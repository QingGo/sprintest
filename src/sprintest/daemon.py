import importlib
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import psutil
import uvicorn  # type: ignore
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sprintest import constants
from sprintest.discovery import discover_package_path
from sprintest.logger import setup_logger
from sprintest.paths import ensure_sprintest_dir, get_socket_path, get_sprintest_dir
from sprintest.service import TestService
from sprintest.status import (
    remove_socket,
    remove_status,
    write_status,
)


def get_lock_path() -> str:
    """Get the current daemon lock file path."""
    return os.environ.get(
        "SPRINTEST_LOCK_FILE", os.path.join(get_sprintest_dir(), "daemon.lock")
    )


logger = setup_logger("sprintest.daemon", is_daemon=True)


class TestRunRequest(BaseModel):
    args: list[str]
    target_pkg: str | None = None


class TestRunResponse(BaseModel):
    exit_code: int
    output: str
    nuked_modules_count: int


# Global state
test_service = TestService()
shutdown_event = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager for FastAPI."""
    pre_load_package()
    yield


app = FastAPI(lifespan=lifespan)


_lock_internal = threading.Lock()


def acquire_daemon_lock() -> bool:
    with _lock_internal:
        ensure_sprintest_dir()
        lock_path = get_lock_path()

        # Try to create the file atomically
        try:
            # Open with O_EXCL to ensure atomic creation
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            # File exists, check if it's stale
            try:
                # Give the other process a tiny bit of time to write its PID
                # if it just created the file
                content = ""
                for _ in range(3):
                    with open(lock_path) as f:
                        content = f.read().strip()
                    if content:
                        break
                    time.sleep(0.01)

                if not content:
                    # Still empty? Probably stale or crashed during creation
                    try:
                        os.remove(lock_path)
                    except OSError:
                        pass
                    return acquire_daemon_lock()

                pid = int(content)
                if not psutil.pid_exists(pid):
                    # Stale lock, remove and retry
                    try:
                        os.remove(lock_path)
                    except OSError:
                        pass
                    return acquire_daemon_lock()
                return False
            except (OSError, ValueError):
                # Error reading or corrupted content, try to remove and retry
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
                return acquire_daemon_lock()
        except Exception:
            return False


def release_daemon_lock() -> None:
    lock_path = get_lock_path()
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception:
        pass


@app.post("/v1/test/run")
async def run_test(request: TestRunRequest) -> TestRunResponse:
    """Execute a test run and return results."""
    result = await test_service.run_tests(request.args, request.target_pkg)

    if result.get("error") == "busy":
        raise HTTPException(status_code=429, detail="Another test is already running")

    return TestRunResponse(
        exit_code=result["exit_code"],
        output=result["output"],
        nuked_modules_count=result["nuked_modules_count"],
    )


@app.post("/v1/test/run/stream")
async def run_test_stream(request: TestRunRequest) -> StreamingResponse:
    """Execute a test run and stream results back to the client."""
    result = await test_service.run_tests(request.args, request.target_pkg)

    if result.get("error") == "busy":
        raise HTTPException(status_code=429, detail="Another test is already running")

    async def event_generator() -> AsyncGenerator[str, None]:
        yield f"[STARTED] nuked {result['nuked_modules_count']} modules\n"
        yield result["output"]
        yield f"\n[DONE] exit_code={result['exit_code']}\n"

    return StreamingResponse(event_generator(), media_type="text/plain")


@app.get("/v1/status")
def status() -> dict[str, str]:
    return {"status": "running", "version": constants.VERSION}


@app.post("/v1/stop")
def stop() -> dict[str, str]:
    logger.info("Stop request received, initiating shutdown...")

    def shutdown() -> None:
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=shutdown, daemon=True).start()
    return {"message": "Sprintest Daemon is shutting down..."}


def handle_exit(sig: int, frame: Any) -> None:
    """Graceful shutdown handler."""
    logger.info(f"Daemon received signal {sig}, shutting down...")
    shutdown_event.set()
    remove_socket()
    remove_status()
    release_daemon_lock()
    sys.exit(0)


def pre_load_package() -> None:
    """Pre-load the target package if specified."""
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())

    target_pkg = os.environ.get(constants.ENV_TARGET_PKG)
    if target_pkg:
        pkg_name = target_pkg.replace("-", "_")
        target_pkg_path = os.environ.get(
            constants.ENV_TARGET_PKG_PATH
        ) or discover_package_path(target_pkg)

        if target_pkg_path:
            if target_pkg_path not in sys.path:
                sys.path.insert(0, target_pkg_path)
            try:
                importlib.import_module(pkg_name)
                logger.info(
                    f"Successfully pre-loaded package: {pkg_name} from {target_pkg_path}"
                )
            except ImportError as e:
                logger.warning(f"Failed to pre-load {pkg_name}: {e}")


def setup_servers() -> tuple[str | None, int | None]:
    """Determine server configuration and write status."""
    ensure_sprintest_dir()
    use_unix = (
        hasattr(socket, "AF_UNIX") and os.environ.get(constants.ENV_FORCE_TCP) != "1"
    )
    if use_unix:
        socket_path = get_socket_path()
        remove_socket()

        write_status(
            {
                "pid": os.getpid(),
                "socket_path": socket_path,
                "version": constants.VERSION,
                "type": "unix",
                "start_time": time.time(),
            }
        )
        logger.info(f"Daemon configured with Unix socket: {socket_path}")
        return socket_path, None
    else:
        port = int(os.environ.get(constants.ENV_PORT, constants.DEFAULT_PORT))
        write_status(
            {
                "pid": os.getpid(),
                "port": port,
                "version": constants.VERSION,
                "type": "tcp",
                "start_time": time.time(),
            }
        )
        logger.info(f"Daemon configured with TCP port: {port}")
        return None, port


def run() -> None:
    # Set up signal handlers
    try:
        signal.signal(signal.SIGTERM, handle_exit)
        signal.signal(signal.SIGINT, handle_exit)
    except ValueError:
        # Signals only work in the main thread; ignore in other environments (like some tests)
        pass

    if not acquire_daemon_lock():
        logger.error(
            "Another instance of Sprintest Daemon is already running. Exiting."
        )
        sys.exit(1)

    try:
        socket_path, port = setup_servers()

        if os.environ.get(constants.ENV_SKIP_UVICORN) != "1":
            if socket_path:
                logger.info(f"Starting Uvicorn on Unix socket: {socket_path}")
                uvicorn.run(
                    "sprintest.daemon:app", uds=socket_path, log_level="warning"
                )
            else:
                p = port or int(
                    os.environ.get(constants.ENV_PORT, constants.DEFAULT_PORT)
                )
                logger.info(f"Starting Uvicorn on port {p}")
                uvicorn.run(
                    "sprintest.daemon:app",
                    host=constants.DEFAULT_HOST,
                    port=p,
                    log_level="warning",
                )
    finally:
        release_daemon_lock()
        remove_socket()
        remove_status()


if __name__ == "__main__":
    run()
