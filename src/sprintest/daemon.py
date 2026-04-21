import importlib
import logging
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
from sprintest.state import DaemonState
from sprintest.status import (
    remove_socket,
    remove_status,
    write_status,
)

try:
    import tomllib  # type: ignore
except ImportError:
    import tomli as tomllib  # type: ignore

logger = logging.getLogger(__name__)


class TestRunRequest(BaseModel):
    args: list[str]
    target_pkg: str | None = None


class TestRunResponse(BaseModel):
    exit_code: int
    output: str
    nuked_modules_count: int


def load_config_patterns(cwd: str) -> list[str]:
    """Load ignore patterns from pyproject.toml."""
    patterns: list[str] = []
    path = os.path.join(cwd, "pyproject.toml")
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
                user_patterns = (
                    data.get("tool", {}).get("sprintest", {}).get("ignore", [])
                )
                if isinstance(user_patterns, list):
                    patterns.extend(user_patterns)
        except (OSError, tomllib.TOMLDecodeError) as e:
            logger.warning(f"Failed to load ignore patterns from {path}: {e}")
    return patterns


def create_app(context: DaemonContext, state: DaemonState) -> FastAPI:
    """App factory to create and configure the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
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
    app.state.context = context
    app.state.state = state
    app.state.test_service = TestService(context)

    @app.post("/v1/test/run")
    async def run_test(
        run_request: TestRunRequest, request: Request
    ) -> TestRunResponse:
        """Execute a test run and return results."""
        test_service: TestService = request.app.state.test_service
        result = await test_service.run_tests(run_request.args, run_request.target_pkg)

        if result.get("error") == "busy":
            raise HTTPException(
                status_code=429, detail="Another test is already running"
            )

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
            raise HTTPException(
                status_code=429, detail="Another test is already running"
            )

        async def event_generator() -> AsyncGenerator[str, None]:
            yield f"[STARTED] nuked {result['nuked_modules_count']} modules\n"
            yield result["output"]
            yield f"\n[DONE] exit_code={result['exit_code']}\n"

        return StreamingResponse(event_generator(), media_type="text/plain")

    @app.get("/v1/status")
    def status() -> dict[str, str]:
        return {"status": "running", "version": context.version}

    @app.post("/v1/stop")
    def stop() -> dict[str, str]:
        logger.info("Stop request received, initiating shutdown...")

        def shutdown() -> None:
            time.sleep(0.5)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=shutdown, daemon=True).start()
        return {"message": "Sprintest Daemon is shutting down..."}

    return app


def acquire_daemon_lock(lock_path: str, internal_lock: threading.Lock) -> bool:
    """Acquire the daemon lock file."""
    while True:
        with internal_lock:
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
                            logger.debug(
                                f"Removing resource at absolute path: {abs_path}"
                            )
                        except OSError as e:
                            logger.error(
                                f"Failed to remove empty lock file at {abs_path}: {e}"
                            )
                        continue

                    pid = int(content)
                    if not psutil.pid_exists(pid):
                        abs_path = os.path.abspath(lock_path)
                        try:
                            os.remove(lock_path)
                            logger.debug(
                                f"Removing resource at absolute path: {abs_path}"
                            )
                        except OSError as e:
                            logger.error(
                                f"Failed to remove stale lock file at {abs_path}: {e}"
                            )
                        continue
                    return False
                except (OSError, ValueError):
                    return False
            except OSError:
                return False
    return False


def handle_exit(sig: int, frame: Any, state: DaemonState) -> None:
    """Signal handler: mark shutdown and exit via SystemExit."""
    logger.info(f"Daemon received signal {sig}, initiating graceful shutdown...")
    state.shutdown_event.set()
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
    # Setup root logger for the daemon
    setup_logger("sprintest", is_daemon=True)

    # Initialize the immutable context at the very start
    ensure_sprintest_dir()
    use_unix = (
        hasattr(socket, "AF_UNIX") and os.environ.get(constants.ENV_FORCE_TCP) != "1"
    )

    cwd = os.getcwd()
    target_pkg = os.environ.get(constants.ENV_TARGET_PKG) or find_target_pkg()
    ignore_patterns = load_config_patterns(cwd)

    context = DaemonContext(
        lock_path=get_lock_path(),
        socket_path=get_socket_path() if use_unix else None,
        status_path=get_status_path(),
        cwd=cwd,
        port=int(os.environ.get(constants.ENV_PORT, constants.DEFAULT_PORT)),
        target_pkg=target_pkg,
        target_pkg_path=os.environ.get(constants.ENV_TARGET_PKG_PATH),
        version=constants.VERSION,
        skip_uvicorn=os.environ.get(constants.ENV_SKIP_UVICORN) == "1",
        ignore_patterns=ignore_patterns,
    )

    state = DaemonState()

    if not acquire_daemon_lock(context.lock_path, state.internal_lock):
        logger.error(
            "Another instance of Sprintest Daemon is already running. Exiting."
        )
        sys.exit(1)

    try:
        app = create_app(context, state)

        try:
            try:

                def exit_wrapper(sig: int, frame: Any) -> None:
                    handle_exit(sig, frame, state)

                signal.signal(signal.SIGTERM, exit_wrapper)
                signal.signal(signal.SIGINT, exit_wrapper)
            except ValueError as e:
                logger.debug(
                    f"Signal handling setup failed (expected if not in main thread): {e}"
                )

            if context.skip_uvicorn:
                logger.info("Skipping Uvicorn startup as requested (test mode).")
                while not state.shutdown_event.is_set():
                    time.sleep(0.1)
                return

            if context.socket_path:
                # Remove stale socket if it exists
                try:
                    os.remove(context.socket_path)
                except OSError as e:
                    logger.debug(
                        f"Note: Could not remove stale socket {context.socket_path}: {e}"
                    )

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
    except Exception:
        logger.exception("Daemon startup failed unexpectedly")
        sys.exit(1)


if __name__ == "__main__":
    run()
