import errno
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn  # type: ignore
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sprintest import constants
from sprintest.context import DaemonContext
from sprintest.discovery import find_target_pkg
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
    is_daemon_alive,
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
            "status": "ready",
            "ready_time": time.time(),
        }
        if state.transport_mode == "unix":
            config.update({"type": "unix", "socket_path": context.socket_path})
        else:
            p = context.port if context.port is not None else constants.DEFAULT_PORT
            config.update({"type": "tcp", "port": p})

        write_status(config, path=context.status_path)
        logger.info("Sprintest Daemon is ready to accept connections.")
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

        gen = test_service.run_tests_stream(run_request.args, run_request.target_pkg)

        # Peek at the first chunk to check for busy error
        try:
            first_chunk = await gen.__anext__()
        except StopAsyncIteration:

            async def empty_gen() -> AsyncGenerator[str, None]:
                if False:
                    yield ""

            return StreamingResponse(empty_gen(), media_type="text/plain")

        if first_chunk == "error:busy":
            raise HTTPException(
                status_code=429, detail="Another test is already running"
            )

        async def event_generator() -> AsyncGenerator[str, None]:
            yield first_chunk
            async for chunk in gen:
                yield chunk

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
    """Acquire the daemon lock file.

    Cross-process atomicity is guaranteed by os.O_EXCL.
    The internal_lock is used to prevent multi-threaded races within the same process
    while performing file operations.
    """
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)

    while True:
        # 1. Try to create the lock file atomically
        try:
            with internal_lock:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write(str(os.getpid()))

            logger.debug(
                f"Created resource at absolute path: {os.path.abspath(lock_path)}"
            )
            return True
        except FileExistsError:
            logger.debug(f"Lock file {lock_path} already exists, checking if stale")
            # File already exists, continue to check if it's stale
        except OSError as e:
            logger.error(f"Failed to create lock file at {lock_path}: {e}")
            return False

        # 2. Inspect the existing lock file (outside internal_lock)
        try:
            content = ""
            # If the file was JUST created by another process, it might be empty for a moment.
            # We retry a few times to read the content.
            for _ in range(3):
                try:
                    with open(lock_path) as f:
                        content = f.read().strip()
                    if content:
                        break
                except FileNotFoundError:
                    logger.debug(
                        f"Lock file {lock_path} disappeared during read (race condition), retrying"
                    )
                    break
                time.sleep(0.01)  # Wait outside the lock

            if not content:
                # File is empty and hasn't been filled in time, or was deleted.
                # Try to remove it and retry the whole process.
                abs_path = os.path.abspath(lock_path)
                with internal_lock:
                    try:
                        if os.path.exists(lock_path):
                            os.remove(lock_path)
                            logger.debug(f"Removing empty resource at path: {abs_path}")
                    except OSError as e:
                        logger.warning(
                            f"Failed to remove empty lock file at {abs_path}: {e}"
                        )
                continue

            pid = int(content)
            if not is_daemon_alive(pid):
                # PID is not running, lock is stale.
                abs_path = os.path.abspath(lock_path)
                with internal_lock:
                    try:
                        # Double check if it's still the same stale PID before removing?
                        # For simplicity, we just try to remove it so we can retry.
                        os.remove(lock_path)
                        logger.debug(f"Removing stale resource at path: {abs_path}")
                    except OSError as e:
                        logger.warning(
                            f"Failed to remove stale lock file at {abs_path}: {e}"
                        )
                continue

            # PID exists and is active, acquisition failed.
            return False
        except (OSError, ValueError) as e:
            # If we hit an error while reading or parsing, the file might be in flux.
            # Log it and retry after a short delay.
            logger.debug(f"Transient error while checking lock file: {e}")
            time.sleep(0.01)
            continue
    return False


def check_unix_socket_support(socket_path: str) -> bool:
    """Check if the filesystem at socket_path supports Unix Domain Sockets."""
    if not hasattr(socket, "AF_UNIX"):
        return False

    # Use a random suffix to avoid collisions during the check
    test_path = f"{socket_path}.test.{os.getpid()}.{time.time()}"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.bind(test_path)
        try:
            os.remove(test_path)
        except OSError as e:
            logger.debug(f"Failed to remove test socket {test_path}: {e}")
        return True
    except (OSError, AttributeError) as e:
        # If it's a permission error or something other than 'Operation not supported',
        # we might still want to try using UDS later.
        if getattr(e, "errno", None) == errno.EOPNOTSUPP:
            logger.debug(
                f"Unix socket support explicitly not supported at {test_path}: {e}"
            )
            return False
        # For other errors, assume it might work at the actual path
        return True


def force_remove_socket(path: str) -> None:
    """Try very hard to remove a socket file, even if it's in a broken state."""
    try:
        if os.path.exists(path) or os.path.lexists(path):
            os.remove(path)
    except OSError as e:
        logger.warning(f"Standard os.remove failed for {path}: {e}")
        # Try calling system 'rm' as a fallback, which sometimes handles
        # virtiofs/9p 'ghost' files better than Python's os.remove.
        # Use shutil.which for cross-platform lookup (Windows has no /bin/rm).
        rm_path = shutil.which("rm")
        if rm_path:
            try:
                subprocess.run(  # noqa: S603
                    [rm_path, "-f", path], check=False, capture_output=True
                )
            except (OSError, subprocess.SubprocessError) as e_rm:
                logger.warning(f"Failed to run '{rm_path} -f {path}': {e_rm}")
        else:
            logger.debug(
                "'rm' not available on this platform, skipping aggressive socket cleanup"
            )

        if os.path.exists(path) or os.path.lexists(path):
            # Still exists? Try renaming it out of the way as a last resort
            try:
                stale_path = f"{path}.stale.{int(time.time())}"
                os.rename(path, stale_path)
                logger.warning(f"Renamed broken socket to {stale_path} (original path still occupied)")
            except OSError as e_rename:
                logger.warning(f"Failed to rename broken socket {path}: {e_rename}")


def handle_exit(sig: int, frame: Any, state: DaemonState) -> None:
    """Signal handler: mark shutdown and exit via SystemExit."""
    logger.info(f"Daemon received signal {sig}, initiating graceful shutdown...")
    state.shutdown_event.set()
    sys.exit(0)


def run() -> None:
    # Setup root logger for the daemon
    setup_logger("sprintest", is_daemon=True)

    # Initialize paths
    ensure_sprintest_dir()
    socket_path = get_socket_path()
    lock_path = get_lock_path()
    status_path = get_status_path()

    state = DaemonState()

    # 1. Acquire Lock
    # We must acquire the lock BEFORE we try to manipulate the socket or status files.
    if not acquire_daemon_lock(lock_path, state.internal_lock):
        logger.error(
            "Another instance of Sprintest Daemon is already running. Exiting."
        )
        sys.exit(1)

    try:
        # 2. Decide Transport
        use_unix = (
            hasattr(socket, "AF_UNIX") and os.environ.get(constants.ENV_FORCE_TCP) != "1"
        )

        if use_unix:
            # Check if filesystem supports UDS for new files
            if not check_unix_socket_support(socket_path):
                logger.warning(
                    f"Filesystem at {os.path.dirname(socket_path)} does not support Unix sockets properly. Falling back to TCP mode."
                )
                use_unix = False
            else:
                # Try to clear the actual path. If it's a 'ghost' file that can't be removed, fallback.
                force_remove_socket(socket_path)
                if os.path.exists(socket_path) or os.path.lexists(socket_path):
                    logger.warning(
                        f"Found unremovable socket entry at {socket_path}. Falling back to TCP mode."
                    )
                    use_unix = False

        # Record runtime transport mode so the lifespan (and pre-load status write)
        # can produce a correct status.json even after a bind-failure fallback.
        state.transport_mode = "unix" if use_unix else "tcp"

        # Determine port for TCP mode: auto-find a free port when user didn't
        # specify one explicitly.  This avoids port conflicts when running
        # multiple daemon instances (e.g. during tests).
        port: int | None = None
        port_str = os.environ.get(constants.ENV_PORT)
        if port_str:
            port = int(port_str)
        elif state.transport_mode == "tcp":
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", 0))
                port = int(s.getsockname()[1])
            logger.info(f"Auto-selected free TCP port: {port}")

        # 3. Initialize the immutable context with the final transport decision
        cwd = os.getcwd()
        target_pkg = os.environ.get(constants.ENV_TARGET_PKG) or find_target_pkg()
        ignore_patterns = load_config_patterns(cwd)

        context = DaemonContext(
            lock_path=lock_path,
            socket_path=socket_path if use_unix else None,
            status_path=status_path,
            cwd=cwd,
            port=port,
            target_pkg=target_pkg,
            target_pkg_path=os.environ.get(constants.ENV_TARGET_PKG_PATH),
            version=constants.VERSION,
            skip_uvicorn=os.environ.get(constants.ENV_SKIP_UVICORN) == "1",
            ignore_patterns=ignore_patterns,
        )

        config: dict[str, Any] = {
            "pid": os.getpid(),
            "version": context.version,
            "start_time": time.time(),
            "cwd": context.cwd,
            "status": "loading",
        }
        if state.transport_mode == "unix":
            config["type"] = "unix"
            config["socket_path"] = context.socket_path
        else:
            config["type"] = "tcp"
            p = context.port if context.port is not None else constants.DEFAULT_PORT
            config["port"] = p
        write_status(config, path=context.status_path)
        logger.debug("status.json written — daemon is starting up.")

        app = create_app(context, state)

        # 5. Start Uvicorn
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
            logger.info(f"Starting Uvicorn on Unix socket: {context.socket_path}")
            uds_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                uds_sock.bind(context.socket_path)
                logger.debug(
                    f"Unix socket bound at {context.socket_path} (fd={uds_sock.fileno()})"
                )
                uvicorn.run(
                    app,
                    fd=uds_sock.fileno(),
                    log_level="warning",
                )
            except OSError as e:
                if e.errno == errno.EOPNOTSUPP:
                    logger.warning(
                        f"Failed to bind Unix socket at {context.socket_path} even after cleanup: {e}. Last-resort fallback to TCP."
                    )
                    state.transport_mode = "tcp"
                    p = (
                        context.port
                        if context.port is not None
                        else constants.DEFAULT_PORT
                    )
                    uvicorn.run(
                        app,
                        host=constants.DEFAULT_HOST,
                        port=p,
                        log_level="warning",
                    )
                else:
                    raise
        else:
            logger.info(f"Starting Uvicorn on port {context.port}")
            p = context.port if context.port is not None else constants.DEFAULT_PORT
            uvicorn.run(
                app,
                host=constants.DEFAULT_HOST,
                port=p,
                log_level="warning",
            )
    except Exception:
        logger.exception("Daemon startup failed unexpectedly")
        sys.exit(1)
    finally:
        # Cleanup: use the paths from context if available to handle path drift correctly.
        # If context wasn't created, fallback to current environment-based paths.
        logger.info("Daemon exiting: releasing lock, socket, and status files.")
        
        ctx = locals().get("context")
        l_path = ctx.lock_path if ctx else get_lock_path()
        s_path = (ctx.socket_path if ctx else None) or get_socket_path()
        st_path = ctx.status_path if ctx else get_status_path()

        if hasattr(socket, "AF_UNIX"):
            remove_socket(s_path)
        remove_status(st_path)

        abs_lock_path = os.path.abspath(l_path)
        try:
            if os.path.exists(l_path):
                os.remove(l_path)
                logger.debug(f"Removing resource at absolute path: {abs_lock_path}")
        except OSError as e:
            logger.error(f"Failed to remove lock file at {abs_lock_path}: {e}")


if __name__ == "__main__":
    run()
