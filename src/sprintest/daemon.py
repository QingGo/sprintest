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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sprintest import constants
from sprintest.context import DaemonContext
from sprintest.discovery import discover_package_path, find_target_pkg
from sprintest.logger import setup_logger
from sprintest.paths import (
    ensure_sprintest_dir,
    get_lock_path,
    get_socket_path,
    get_status_path,
)
from sprintest.service import TestService
from sprintest.status import (
    remove_socket,
    remove_status,
    write_status,
)

logger = setup_logger("sprintest", is_daemon=True)


class TestRunRequest(BaseModel):
    args: list[str]
    target_pkg: str | None = None


class TestRunResponse(BaseModel):
    exit_code: int
    output: str
    nuked_modules_count: int


# Global state
shutdown_event = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    context: DaemonContext = app.state.context
    config: dict[str, Any] = {
        "pid": os.getpid(),
        "version": context.version,
        "start_time": time.time(),
        "cwd": context.cwd,
    }
    if context.socket_path:
        config.update({"type": "unix", "socket_path": context.socket_path})
    else:
        config.update({"type": "tcp", "port": context.port})

    write_status(config)
    logger.debug("status.json written — daemon is ready to accept connections.")
    pre_load_package(context)
    yield


app = FastAPI(lifespan=lifespan)


_lock_internal = threading.Lock()


def acquire_daemon_lock(lock_path: str) -> bool:
    """Acquire the daemon lock file."""
    with _lock_internal:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)

        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            logger.debug(
                f"Created resource at absolute path: {os.path.abspath(lock_path)}"
            )
            return True
        except FileExistsError:
            try:
                content = ""
                for _ in range(3):
                    with open(lock_path) as f:
                        content = f.read().strip()
                    if content:
                        break
                    time.sleep(0.01)

                if not content:
                    abs_path = os.path.abspath(lock_path)
                    try:
                        os.remove(lock_path)
                        logger.debug(f"Removing resource at absolute path: {abs_path}")
                    except OSError as e:
                        logger.error(
                            f"Failed to remove empty lock file at {abs_path}: {e}"
                        )
                    return acquire_daemon_lock(lock_path)

                pid = int(content)
                if not psutil.pid_exists(pid):
                    abs_path = os.path.abspath(lock_path)
                    try:
                        os.remove(lock_path)
                        logger.debug(f"Removing resource at absolute path: {abs_path}")
                    except OSError as e:
                        logger.error(
                            f"Failed to remove stale lock file at {abs_path}: {e}"
                        )
                    return acquire_daemon_lock(lock_path)
                return False
            except Exception:
                return False
        except Exception:
            return False


@app.post("/v1/test/run")
async def run_test(run_request: TestRunRequest, request: Request) -> TestRunResponse:
    """Execute a test run and return results."""
    test_service: TestService = request.app.state.test_service
    result = await test_service.run_tests(run_request.args, run_request.target_pkg)

    if result.get("error") == "busy":
        raise HTTPException(status_code=429, detail="Another test is already running")

    return TestRunResponse(
        exit_code=result["exit_code"],
        output=result["output"],
        nuked_modules_count=result["nuked_modules_count"],
    )


@app.post("/v1/test/run/stream")
async def run_test_stream(
    run_request: TestRunRequest, request: Request
) -> StreamingResponse:
    """Execute a test run and stream results back to the client."""
    test_service: TestService = request.app.state.test_service
    result = await test_service.run_tests(run_request.args, run_request.target_pkg)

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
    """Signal handler: mark shutdown and exit via SystemExit.

    Cleanup (lock / socket / status) is handled exclusively by the
    finally block in run(), which catches SystemExit just like any
    other exception.  Do NOT perform cleanup here to avoid double-
    removal races.
    """
    logger.info(f"Daemon received signal {sig}, initiating graceful shutdown...")
    shutdown_event.set()
    sys.exit(0)


def pre_load_package(context: DaemonContext) -> None:
    """Pre-load the target package if specified or auto-discoverable."""
    if context.cwd not in sys.path:
        sys.path.insert(0, context.cwd)

    target_pkg = context.target_pkg
    if target_pkg:
        pkg_name = target_pkg.replace("-", "_")
        target_pkg_path = context.target_pkg_path or discover_package_path(target_pkg)

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


def run() -> None:
    # Initialize the immutable context at the very start
    ensure_sprintest_dir()
    use_unix = (
        hasattr(socket, "AF_UNIX") and os.environ.get(constants.ENV_FORCE_TCP) != "1"
    )

    target_pkg = os.environ.get(constants.ENV_TARGET_PKG) or find_target_pkg()

    context = DaemonContext(
        lock_path=get_lock_path(),
        socket_path=get_socket_path() if use_unix else None,
        status_path=get_status_path(),
        cwd=os.getcwd(),
        port=int(os.environ.get(constants.ENV_PORT, constants.DEFAULT_PORT)),
        target_pkg=target_pkg,
        target_pkg_path=os.environ.get(constants.ENV_TARGET_PKG_PATH),
        version=constants.VERSION,
        skip_uvicorn=os.environ.get(constants.ENV_SKIP_UVICORN) == "1",
    )

    if not acquire_daemon_lock(context.lock_path):
        logger.error(
            "Another instance of Sprintest Daemon is already running. Exiting."
        )
        sys.exit(1)

    # Initialize service with context and store in app state
    test_service = TestService(context)
    app.state.context = context
    app.state.test_service = test_service

    try:
        try:
            signal.signal(signal.SIGTERM, handle_exit)
            signal.signal(signal.SIGINT, handle_exit)
        except ValueError:
            pass

        if context.skip_uvicorn:
            logger.info("Skipping Uvicorn startup as requested (test mode).")
            while not shutdown_event.is_set():
                time.sleep(0.1)
            return

        if context.socket_path:
            logger.info(f"Starting Uvicorn on Unix socket: {context.socket_path}")
            uds_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            uds_sock.bind(context.socket_path)
            logger.debug(
                f"Unix socket bound at {context.socket_path} (fd={uds_sock.fileno()})"
            )
            uvicorn.run(
                app,
                fd=uds_sock.fileno(),
                log_level="warning",
            )
        else:
            logger.info(f"Starting Uvicorn on port {context.port}")
            # Ensure port is not None for type safety
            p = context.port if context.port is not None else constants.DEFAULT_PORT
            uvicorn.run(
                app,
                host=constants.DEFAULT_HOST,
                port=p,
                log_level="warning",
            )
    finally:
        # Cleanup using immutable context paths
        logger.info("Daemon exiting: releasing lock, socket, and status files.")
        if context.socket_path:
            remove_socket(context.socket_path)
        remove_status(context.status_path)
        abs_lock_path = os.path.abspath(context.lock_path)
        try:
            if os.path.exists(context.lock_path):
                os.remove(context.lock_path)
                logger.debug(f"Removing resource at absolute path: {abs_lock_path}")
        except OSError as e:
            logger.error(f"Failed to remove lock file at {abs_lock_path}: {e}")


if __name__ == "__main__":
    run()
