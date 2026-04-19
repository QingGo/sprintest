import os
import re
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import closing
from typing import Any

import pytest
import requests  # type: ignore


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
            ".venv/bin/python",
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
    max_retries = 15
    for i in range(max_retries):
        try:
            # Check if the port is open and uvicorn is responding
            # We use a GET to a non-existent route or /openapi.json which is fast and side-effect free
            requests.get(f"http://localhost:{port}/openapi.json", timeout=2)
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if i == max_retries - 1:
                proc.terminate()
                # Print daemon output for debugging
                stdout, stderr = proc.communicate()
                print(f"Daemon STDOUT: {stdout.decode() if stdout else ''}")
                print(f"Daemon STDERR: {stderr.decode() if stderr else ''}")
                pytest.fail("Daemon failed to start or respond")
            time.sleep(1)

    yield {"port": port, "url": url, "env": env}

    proc.terminate()
    proc.wait()


def test_basic_run(daemon_process: dict[str, Any]) -> None:
    """Verify simple communication between CLI and Daemon."""
    # Create a dummy test file
    test_content = "def test_pass(): assert 1 + 1 == 2\n"
    with open("tests/tmp_test_basic.py", "w") as f:
        f.write(test_content)

    try:
        env = daemon_process["env"].copy()
        env["SPRINTEST_TARGET_PKG"] = "sprintest"
        res = subprocess.run(
            [
                ".venv/bin/python",
                "-c",
                "from sprintest.cli import main; main()",
                "tests/tmp_test_basic.py",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
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
        res = subprocess.run(
            [
                ".venv/bin/python",
                "-c",
                "from sprintest.cli import main; main()",
                "tests/tmp_test_io.py",
                "-s",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert "HELLO STDOUT" in res.stdout
        assert "HELLO STDERR" in res.stdout
    finally:
        if os.path.exists("tests/tmp_test_io.py"):
            os.remove("tests/tmp_test_io.py")


def test_ansi_purification(daemon_process: dict[str, Any]) -> None:
    """Verify that ANSI escape codes are stripped from the output."""
    # Pytest usually outputs color codes even with --color=yes if we trick it,
    # but our daemon forces --color=no.
    # We can check if typical reset codes like \x1b[0m are absent.
    test_content = "def test_fail(): assert False\n"
    with open("tests/tmp_test_ansi.py", "w") as f:
        f.write(test_content)

    try:
        env = daemon_process["env"].copy()
        env["SPRINTEST_TARGET_PKG"] = "sprintest"
        res = subprocess.run(
            [
                ".venv/bin/python",
                "-c",
                "from sprintest.cli import main; main()",
                "tests/tmp_test_ansi.py",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        # Check for presence of ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[mK]")
        assert not ansi_escape.search(res.stdout), "ANSI escape codes found in output"
    finally:
        if os.path.exists("tests/tmp_test_ansi.py"):
            os.remove("tests/tmp_test_ansi.py")


def test_concurrency_lock(daemon_process: dict[str, Any]) -> None:
    """Verify that while one test is running, subsequent requests are rejected."""
    test_content = "import time\ndef test_sleep():\n    time.sleep(2)\n"
    with open("tests/tmp_test_sleep.py", "w") as f:
        f.write(test_content)

    try:
        env = daemon_process["env"].copy()
        # Set target_pkg explicitly in env for both cliff and daemon to avoid missing pkg errors
        env["SPRINTEST_TARGET_PKG"] = "sprintest"

        # Start first test in background
        proc1 = subprocess.Popen(
            [
                ".venv/bin/python",
                "-c",
                "from sprintest.cli import main; main()",
                "tests/tmp_test_sleep.py",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait longer to ensure the first request has reached the daemon and acquired the lock
        # Pytest startup can be slow
        time.sleep(1.5)

        res2 = subprocess.run(
            [
                ".venv/bin/python",
                "-c",
                "from sprintest.cli import main; main()",
                "tests/tmp_test_sleep.py",
            ],
            env=env,
            capture_output=True,
            text=True,
        )

        # Before asserting, wait for proc1 to finish so we can see its output if it failed
        stdout1, stderr1 = proc1.communicate()

        assert "Error: Daemon is busy" in res2.stdout, (
            f"Second request should be blocked. Out: {res2.stdout}, Err1: {stderr1}, Out1: {stdout1}"
        )
        assert res2.returncode == 1
    finally:
        if os.path.exists("tests/tmp_test_sleep.py"):
            os.remove("tests/tmp_test_sleep.py")


def test_environment_variables(daemon_process: dict[str, Any]) -> None:
    """Verify that environment variables like SPRINTEST_TARGET_PKG are correctly picked up."""
    # We use a custom env where SPRINTEST_TARGET_PKG is set
    env = daemon_process["env"].copy()
    env["SPRINTEST_TARGET_PKG"] = "sprintest"

    test_content = "def test_env(): assert 1 == 1\n"
    with open("tests/tmp_test_env.py", "w") as f:
        f.write(test_content)

    try:
        # Run CLI without --target_pkg argument
        res = subprocess.run(
            [
                ".venv/bin/python",
                "-c",
                "from sprintest.cli import main; main()",
                "tests/tmp_test_env.py",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0
        # Check if the daemon log output (not returned to cli) mentions nuking?
        # Actually, we can check if it passed.
    finally:
        if os.path.exists("tests/tmp_test_env.py"):
            os.remove("tests/tmp_test_env.py")
