import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import closing
from typing import Any

import pytest
import requests  # type: ignore

from sprintest import constants
from sprintest.status import get_socket_path, remove_socket


def find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def daemon_process() -> Generator[dict[str, Any], None, None]:
    port = find_free_port()
    env = os.environ.copy()
    env["SPRINTEST_PORT"] = str(port)
    env["PYTHONPATH"] = os.path.join(os.getcwd(), "src")

    # Start the daemon
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "sprintest.daemon:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
        ],
        env=env,
    )

    # Wait for the daemon to be ready
    url = f"http://localhost:{port}/v1/test/run"
    max_retries = 30
    daemon_ready = False
    for _ in range(max_retries):
        try:
            requests.get(f"http://localhost:{port}/v1/status", timeout=0.5)
            daemon_ready = True
            break
        except Exception:
            time.sleep(0.1)

    if not daemon_ready:
        proc.terminate()
        pytest.fail("Daemon failed to start in integration tests")

    yield {"port": port, "url": url, "env": env}

    proc.terminate()
    proc.wait()


def run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """Helper to run CLI using current sys.executable."""
    # Ensure src is in PYTHONPATH for the subprocess
    new_env = env.copy()
    src_path = os.path.join(os.getcwd(), "src")
    if "PYTHONPATH" in new_env:
        if src_path not in new_env["PYTHONPATH"]:
            new_env["PYTHONPATH"] = f"{src_path}:{new_env['PYTHONPATH']}"
    else:
        new_env["PYTHONPATH"] = src_path

    return subprocess.run(
        [sys.executable, "-c", "from sprintest.cli import main; main()"] + args,
        env=new_env,
        capture_output=True,
        text=True,
    )


def test_basic_run(daemon_process: dict[str, Any]) -> None:
    """Verify simple communication between CLI and Daemon."""
    test_content = "def test_pass(): assert 1 + 1 == 2\n"
    with open("tests/tmp_test_basic.py", "w") as f:
        f.write(test_content)

    try:
        env = daemon_process["env"].copy()
        env["SPRINTEST_TARGET_PKG"] = "sprintest"
        res = run_cli(["--no-stream", "tests/tmp_test_basic.py"], env=env)
        assert res.returncode == 0
        assert "passed" in res.stdout
    finally:
        if os.path.exists("tests/tmp_test_basic.py"):
            os.remove("tests/tmp_test_basic.py")


def test_output_hijacking(daemon_process: dict[str, Any]) -> None:
    """Ensure both stdout and stderr from tests are captured and returned."""
    test_content = """
import sys
def test_io():
    print("HELLO STDOUT")
    print("HELLO STDERR", file=sys.stderr)
    assert True
"""
    with open("tests/tmp_test_io.py", "w") as f:
        f.write(test_content)

    try:
        env = daemon_process["env"].copy()
        env["SPRINTEST_TARGET_PKG"] = "sprintest"
        res = run_cli(["--no-stream", "tests/tmp_test_io.py", "-s"], env=env)
        assert "HELLO STDOUT" in res.stdout
        assert "HELLO STDERR" in res.stdout
    finally:
        if os.path.exists("tests/tmp_test_io.py"):
            os.remove("tests/tmp_test_io.py")


def test_concurrency_lock(daemon_process: dict[str, Any]) -> None:
    """Verify that while one test is running, subsequent requests are rejected."""
    test_content = "import time\ndef test_sleep():\n    time.sleep(1)\n"
    with open("tests/tmp_test_sleep.py", "w") as f:
        f.write(test_content)

    try:
        import threading

        results = {}

        def send_first_request() -> None:
            try:
                response = requests.post(
                    f"http://localhost:{daemon_process['port']}/v1/test/run",
                    json={
                        "args": ["tests/tmp_test_sleep.py"],
                        "target_pkg": "sprintest",
                    },
                    timeout=10,
                )
                results["res1"] = response.json()
            except Exception as e:
                results["res1_error"] = str(e)

        thread1 = threading.Thread(target=send_first_request)
        thread1.start()

        time.sleep(0.3)

        response2 = requests.post(
            f"http://localhost:{daemon_process['port']}/v1/test/run",
            json={"args": ["tests/tmp_test_sleep.py"], "target_pkg": "sprintest"},
            timeout=5,
        )
        res2_data = response2.json()

        thread1.join()
        assert "Error: Daemon is busy" in res2_data["output"]
    finally:
        if os.path.exists("tests/tmp_test_sleep.py"):
            os.remove("tests/tmp_test_sleep.py")


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix Sockets not supported")
def test_unix_socket_integration() -> None:
    """Verify communication via Unix Sockets."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(os.getcwd(), "src")
    env["SPRINTEST_FORCE_TCP"] = "0"

    # Clean up lock file if it exists to avoid "Another instance running" error
    from sprintest.daemon import DAEMON_LOCK_FILE

    if os.path.exists(DAEMON_LOCK_FILE):
        try:
            os.remove(DAEMON_LOCK_FILE)
        except OSError:
            pass

    # Start daemon via main to trigger Unix socket setup
    proc = subprocess.Popen(
        [sys.executable, "-m", "sprintest.daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Wait for socket
        socket_path = get_socket_path()
        for _ in range(30):
            if os.path.exists(socket_path):
                break
            time.sleep(0.1)

        assert os.path.exists(socket_path), "Unix socket was not created"

        # Run a test via CLI using Unix socket (auto-detected)
        test_content = "def test_unix(): assert True\n"
        with open("tests/tmp_test_unix.py", "w") as f:
            f.write(test_content)

        try:
            env["SPRINTEST_TARGET_PKG"] = "sprintest"
            res = run_cli(["--no-stream", "tests/tmp_test_unix.py"], env=env)
            assert res.returncode == 0
            assert "passed" in res.stdout
        finally:
            if os.path.exists("tests/tmp_test_unix.py"):
                os.remove("tests/tmp_test_unix.py")
    finally:
        proc.terminate()
        proc.wait()
        remove_socket()


def test_graceful_stop(daemon_process: dict[str, Any]) -> None:
    """Verify daemon stops gracefully via stop endpoint."""
    port = daemon_process["port"]
    requests.post(f"http://localhost:{port}/v1/stop")

    # Wait for process to exit
    for _ in range(20):
        try:
            requests.get(f"http://localhost:{port}/v1/status", timeout=0.1)
            time.sleep(0.1)
        except Exception:
            return  # Success

    pytest.fail("Daemon did not stop gracefully")
