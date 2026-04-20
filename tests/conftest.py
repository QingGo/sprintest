import os
import tempfile

# Isolate the test session from the real project's .sprintest directory
# This is crucial when running "self-hosting" tests (sprintest testing itself)
if "SPRINTEST_DIR" not in os.environ:
    _temp_dir = tempfile.TemporaryDirectory(prefix="sprintest_test_")
    os.environ["SPRINTEST_DIR"] = _temp_dir.name
    # Note: the directory will be cleaned up when the process exits
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import closing
from typing import Any

import httpx
import pytest

from sprintest import constants
from sprintest.paths import get_socket_path, get_sprintest_dir
from sprintest.status import remove_socket


def wait_for_condition(
    condition_func: Callable[[], bool],
    timeout: float = 5.0,
    interval: float = 0.1,
    error_msg: str = "Condition not met within timeout",
) -> None:
    """Wait for a condition to be true with polling."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            if condition_func():
                return
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(error_msg)


def find_free_port() -> int:
    """Find a free TCP port."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


@pytest.fixture
def free_port() -> int:
    return find_free_port()


@pytest.fixture
def daemon_config() -> dict[str, Any]:
    """Default configuration for the daemon service."""
    return {"type": "tcp", "extra_env": {}, "cwd": os.getcwd()}


@pytest.fixture
def daemon_service(
    daemon_config: dict[str, Any],
) -> Generator[dict[str, Any], None, None]:
    """
    Centralized fixture to start and stop the Sprintest Daemon.
    """
    daemon_type = daemon_config.get("type", "tcp")
    extra_env = daemon_config.get("extra_env", {})
    cwd = daemon_config.get("cwd", os.getcwd())

    port = find_free_port()
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(os.getcwd(), "src")
    env.update(extra_env)

    # Force the type
    if daemon_type == "unix":
        env["SPRINTEST_FORCE_TCP"] = "0"
        socket_path = get_socket_path()
        remove_socket()
    else:
        env["SPRINTEST_PORT"] = str(port)
        env["SPRINTEST_FORCE_TCP"] = "1"
        socket_path = None

    # Use a unique lock file for tests to avoid contention
    env["SPRINTEST_LOCK_FILE"] = os.path.join(
        get_sprintest_dir(), f"daemon_{port or 'unix'}.lock"
    )

    # Start the daemon
    proc = subprocess.Popen(
        [sys.executable, "-m", "sprintest.daemon"],
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        if daemon_type == "unix":
            assert socket_path is not None
            wait_for_condition(
                lambda: os.path.exists(socket_path),
                timeout=10.0,
                error_msg=f"Unix socket {socket_path} was not created",
            )
        else:
            url = f"http://127.0.0.1:{port}/v1/status"
            wait_for_condition(
                lambda: httpx.get(url, timeout=0.1, trust_env=False).status_code == 200,
                timeout=10.0,
                error_msg=f"Daemon failed to start on port {port}",
            )

        yield {
            "type": daemon_type,
            "port": port,
            "socket_path": socket_path,
            "env": env,
            "proc": proc,
        }

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()

        if daemon_type == "unix":
            remove_socket()
