import asyncio
import importlib
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
from typing import Any

import psutil
import uvicorn  # type: ignore
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sprintest import constants
from sprintest.discovery import discover_package_path
from sprintest.logger import setup_logger
from sprintest.runner import TestRunner
from sprintest.status import (
    ensure_sprintest_dir,
    get_socket_path,
    remove_socket,
    remove_status,
    write_status,
)

# Setup daemon logger (with file output)
logger = setup_logger("sprintest.daemon", is_daemon=True)


class TestRunRequest(BaseModel):
    args: list[str]
    target_pkg: str | None = None


class TestRunResponse(BaseModel):
    exit_code: int
    output: str
    nuked_modules_count: int


app = FastAPI()

# Global state
test_runner = TestRunner()
test_lock = threading.Lock()
DAEMON_LOCK_FILE = os.path.join(tempfile.gettempdir(), "sprintest_global.lock")
shutdown_event = threading.Event()


def acquire_daemon_lock() -> bool:
    if os.path.exists(DAEMON_LOCK_FILE):
        try:
            with open(DAEMON_LOCK_FILE) as f:
                pid = int(f.read().strip())
            if psutil.pid_exists(pid):
                return False
        except Exception:
            pass
    try:
        with open(DAEMON_LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return False


def release_daemon_lock() -> None:
    try:
        if os.path.exists(DAEMON_LOCK_FILE):
            os.remove(DAEMON_LOCK_FILE)
    except Exception:
        pass


@app.post("/v1/test/run/stream")
async def run_test_stream(request: TestRunRequest) -> StreamingResponse:
    if not test_lock.acquire(blocking=False):
        logger.warning("Rejecting streaming test request: Daemon is busy.")
        raise HTTPException(status_code=503, detail="Daemon is busy.")

    try:
        loop = asyncio.get_event_loop()
        target_pkg = request.target_pkg or os.environ.get(constants.ENV_TARGET_PKG)
        if not target_pkg:
            logger.error("Rejecting streaming test request: target_pkg missing.")
            return StreamingResponse(
                iter([b"Error: target_pkg missing.\n", b"[DONE] exit_code=1\n"]),
                media_type="text/plain",
            )

        logger.info(f"Starting streaming test run for package: {target_pkg}")
        exit_code, output, nuked_count = await loop.run_in_executor(
            None, test_runner.run_tests, request.args, target_pkg, True
        )

        lines = [f"[STARTED] nuked {nuked_count} modules\n"]
        lines.extend(output.splitlines())
        lines.append(f"\n[DONE] exit_code={exit_code}\n")

        return StreamingResponse(
            iter([(line + "\n").encode() for line in lines]),
            media_type="text/plain",
        )
    finally:
        try:
            test_lock.release()
        except RuntimeError:
            pass


@app.post("/v1/test/run")
async def run_test(request: TestRunRequest) -> TestRunResponse:
    if not test_lock.acquire(blocking=False):
        logger.warning("Rejecting test request: Daemon is busy.")
        return TestRunResponse(
            exit_code=1, output="Error: Daemon is busy.", nuked_modules_count=0
        )

    try:
        target_pkg = request.target_pkg or os.environ.get(constants.ENV_TARGET_PKG)
        if not target_pkg:
            logger.error("Rejecting test request: target_pkg missing.")
            return TestRunResponse(
                exit_code=1, output="Error: target_pkg missing.", nuked_modules_count=0
            )

        logger.info(f"Starting test run for package: {target_pkg}")
        loop = asyncio.get_event_loop()
        exit_code, output, nuked_count = await loop.run_in_executor(
            None, test_runner.run_tests, request.args, target_pkg
        )

        return TestRunResponse(
            exit_code=exit_code, output=output, nuked_modules_count=nuked_count
        )
    finally:
        try:
            test_lock.release()
        except RuntimeError:
            pass


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


def run_unix_socket_server(socket_path: str) -> None:
    """Run Unix socket server in a separate thread."""
    try:
        server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_socket.settimeout(1.0)
        server_socket.bind(socket_path)
        server_socket.listen(5)

        while not shutdown_event.is_set():
            try:
                client_socket, _ = server_socket.accept()
            except TimeoutError:
                continue

            try:
                data = b""
                while True:
                    chunk = client_socket.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n\n" in data:
                        break
                if not data:
                    continue

                req = json.loads(data.decode("utf-8"))
                cmd = req.get("command")
                if cmd == "run_test":
                    args = req.get("args", [])
                    t_pkg = req.get("target_pkg") or os.environ.get(
                        constants.ENV_TARGET_PKG
                    )
                    if not t_pkg:
                        resp = {
                            "exit_code": 1,
                            "output": "target_pkg missing",
                            "nuked_modules_count": 0,
                        }
                    else:
                        with test_lock:
                            logger.info(f"Unix Socket: Running tests for {t_pkg}")
                            exit_code, output, nuked_count = test_runner.run_tests(
                                args, t_pkg
                            )
                            resp = {
                                "exit_code": exit_code,
                                "output": output,
                                "nuked_modules_count": nuked_count,
                            }
                elif cmd == "status":
                    resp = {"status": "running", "version": constants.VERSION}
                elif cmd == "stop":
                    resp = {"message": "Shutting down..."}
                    logger.info("Unix Socket: Stop request received.")

                    def do_shutdown_unix_sock() -> None:
                        time.sleep(0.5)
                        os.kill(os.getpid(), signal.SIGTERM)

                    threading.Thread(target=do_shutdown_unix_sock, daemon=True).start()
                else:
                    resp = {"error": "Unknown command"}
                client_socket.sendall(json.dumps(resp).encode("utf-8") + b"\n\n")
            except Exception as e:
                logger.error(f"Unix socket handling error: {e}")
            finally:
                client_socket.close()
    except Exception as e:
        if not shutdown_event.is_set():
            logger.error(f"Unix socket server failed: {e}")
    finally:
        remove_socket()


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


def setup_servers() -> None:
    """Set up Unix and TCP servers."""
    use_unix = (
        hasattr(socket, "AF_UNIX") and os.environ.get(constants.ENV_FORCE_TCP) != "1"
    )
    if use_unix:
        ensure_sprintest_dir()
        socket_path = get_socket_path()
        remove_socket()

        threading.Thread(
            target=run_unix_socket_server, args=(socket_path,), daemon=True
        ).start()

        write_status(
            {
                "pid": os.getpid(),
                "socket_path": socket_path,
                "version": constants.VERSION,
                "type": "unix",
            }
        )
        logger.info(f"Daemon started with Unix socket: {socket_path}")
    else:
        port = int(os.environ.get(constants.ENV_PORT, constants.DEFAULT_PORT))
        write_status(
            {
                "pid": os.getpid(),
                "port": port,
                "version": constants.VERSION,
                "type": "tcp",
            }
        )
        logger.info(f"Daemon started with TCP port: {port}")


def run() -> None:
    # Set up signal handlers
    signal.signal(signal.SIGTERM, handle_exit)
    signal.signal(signal.SIGINT, handle_exit)

    if not acquire_daemon_lock():
        logger.error(
            "Another instance of Sprintest Daemon is already running. Exiting."
        )
        sys.exit(1)

    try:
        pre_load_package()
        setup_servers()

        if os.environ.get(constants.ENV_SKIP_UVICORN) != "1":
            port = int(os.environ.get(constants.ENV_PORT, constants.DEFAULT_PORT))
            logger.info(f"Starting Uvicorn on port {port}")
            uvicorn.run(
                app, host=constants.DEFAULT_HOST, port=port, log_level="warning"
            )
    finally:
        release_daemon_lock()


if __name__ == "__main__":
    run()
