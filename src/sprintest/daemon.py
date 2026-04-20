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
test_service = TestService()
shutdown_event = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager for FastAPI.

    status.json is written here, AFTER uvicorn has bound its socket and is
    ready to serve requests.  This guarantees that any client polling
    read_status() can immediately connect without a separate health-check.

    Note: uvicorn.run() with a string app reference reimports this module,
    so we cannot rely on a module-level _server_config set by setup_servers().
    Instead we reconstruct the config from well-known paths and env vars.
    """
    use_unix = (
        hasattr(socket, "AF_UNIX") and os.environ.get(constants.ENV_FORCE_TCP) != "1"
    )
    if use_unix:
        config: dict[str, Any] = {
            "pid": os.getpid(),
            "socket_path": get_socket_path(),
            "version": constants.VERSION,
            "type": "unix",
            "start_time": time.time(),
        }
    else:
        config = {
            "pid": os.getpid(),
            "port": int(os.environ.get(constants.ENV_PORT, constants.DEFAULT_PORT)),
            "version": constants.VERSION,
            "type": "tcp",
            "start_time": time.time(),
        }
    write_status(config)
    logger.debug("status.json written — daemon is ready to accept connections.")
    pre_load_package()
    yield


app = FastAPI(lifespan=lifespan)


_lock_internal = threading.Lock()


def acquire_daemon_lock() -> bool:
    """Acquire the daemon lock file."""
    with _lock_internal:
        ensure_sprintest_dir()
        lock_path = get_lock_path()

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
                    return acquire_daemon_lock()

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
                    return acquire_daemon_lock()
                return False
            except Exception:
                return False
        except Exception:
            return False


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
    """Signal handler: mark shutdown and exit via SystemExit.

    Cleanup (lock / socket / status) is handled exclusively by the
    finally block in run(), which catches SystemExit just like any
    other exception.  Do NOT perform cleanup here to avoid double-
    removal races.
    """
    logger.info(f"Daemon received signal {sig}, initiating graceful shutdown...")
    shutdown_event.set()
    sys.exit(0)


def pre_load_package() -> None:
    """Pre-load the target package if specified or auto-discoverable."""
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())

    target_pkg = os.environ.get(constants.ENV_TARGET_PKG)
    if not target_pkg:
        target_pkg = find_target_pkg()
        if target_pkg:
            logger.info(f"Auto-detected target package for pre-load: {target_pkg}")

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
    """Determine server configuration.

    Does NOT write status.json here — that is deferred to lifespan() so
    that the file only appears once uvicorn is truly ready to serve.
    """
    ensure_sprintest_dir()
    use_unix = (
        hasattr(socket, "AF_UNIX") and os.environ.get(constants.ENV_FORCE_TCP) != "1"
    )
    if use_unix:
        socket_path = get_socket_path()

        remove_socket()
        logger.info(f"Daemon configured with Unix socket: {socket_path}")
        return socket_path, None
    else:
        port = int(os.environ.get(constants.ENV_PORT, constants.DEFAULT_PORT))
        logger.info(f"Daemon configured with TCP port: {port}")
        return None, port


def run() -> None:
    # Cache the original paths immediately upon startup.
    # Because pytest.main() runs inside this process, tests might
    # modify os.getcwd() or os.environ["SPRINTEST_DIR"].
    # Using local variables guarantees we clean up the actual files
    # we created, regardless of any later environment drift.
    orig_lock_path = get_lock_path()
    orig_socket_path = get_socket_path()
    orig_status_path = get_status_path()

    if not acquire_daemon_lock():
        logger.error(
            "Another instance of Sprintest Daemon is already running. Exiting."
        )
        sys.exit(1)

    # Everything after lock acquisition is wrapped in try/finally so that
    # cleanup runs unconditionally — whether we exit normally, via an
    # unhandled exception, or via SystemExit raised by handle_exit.
    try:
        # Register signal handlers INSIDE the try block.  If a signal
        # arrives before this point the Python default handler runs
        # (KeyboardInterrupt / termination), which also propagates through
        # the finally clause below — safe either way.
        try:
            signal.signal(signal.SIGTERM, handle_exit)
            signal.signal(signal.SIGINT, handle_exit)
        except ValueError:
            # Signals can only be registered from the main thread;
            # ignore in test environments that run in worker threads.
            pass

        socket_path, port = setup_servers()

        if os.environ.get(constants.ENV_SKIP_UVICORN) != "1":
            if socket_path:
                logger.info(f"Starting Uvicorn on Unix socket: {socket_path}")
                # Pre-bind the socket ourselves so uvicorn receives an already-bound
                # file descriptor via `fd=` instead of a path via `uds=`. This
                # bypasses uvicorn's internal os.chmod() call (only triggered when
                # `uds=` is used) which fails with EINVAL on some Linux filesystems
                # (e.g., overlayfs inside Docker containers).
                #
                # We intentionally skip chmod here. Default socket permissions
                # (0o755 with typical umask) allow only the owner to connect,
                # which is correct for a local dev tool where the CLI and daemon
                # always run as the same user. The 0o660 that uvicorn sets is
                # designed for production WSGI setups (e.g., nginx connecting via
                # a group) — that model does not apply to sprintest.
                uds_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                uds_sock.bind(socket_path)
                logger.debug(
                    f"Unix socket bound at {socket_path} (fd={uds_sock.fileno()})"
                )
                uvicorn.run(
                    "sprintest.daemon:app",
                    fd=uds_sock.fileno(),
                    log_level="warning",
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
        # Single, authoritative cleanup point.  Runs for all exit paths:
        #   - Normal uvicorn shutdown (SIGTERM handled by uvicorn itself)
        #   - SystemExit raised by handle_exit (SIGTERM/SIGINT before uvicorn starts)
        #   - Unhandled exceptions during setup
        try:
            logger.info("Daemon exiting: releasing lock, socket, and status files.")
        except Exception:
            pass
        finally:
            remove_socket(orig_socket_path)
            remove_status(orig_status_path)
            abs_path = os.path.abspath(orig_lock_path)
            try:
                if os.path.exists(orig_lock_path):
                    os.remove(orig_lock_path)
                    logger.debug(f"Removing resource at absolute path: {abs_path}")
            except OSError as e:
                logger.error(f"Failed to remove lock file at {abs_path}: {e}")


if __name__ == "__main__":
    run()
