import os
import subprocess
import sys
import threading
from typing import Any

import httpx
import pytest


def run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """Helper to run CLI using current sys.executable."""
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


@pytest.fixture
def daemon_config(request: Any) -> dict[str, Any]:
    return {
        "type": getattr(request, "param", "tcp"),
        "extra_env": {"SPRINTEST_TARGET_PKG": "sprintest"},
        "cwd": os.getcwd(),
    }


def test_basic_run(daemon_service: dict[str, Any], tmp_path: Any) -> None:
    """Verify simple communication between CLI and Daemon."""
    test_file = tmp_path / "test_basic.py"
    test_file.write_text("def test_pass(): assert 1 + 1 == 2\n")

    res = run_cli(["--no-stream", str(test_file)], env=daemon_service["env"])
    assert res.returncode == 0
    assert "passed" in res.stdout


def test_output_hijacking(daemon_service: dict[str, Any], tmp_path: Any) -> None:
    """Ensure both stdout and stderr from tests are captured and returned."""
    test_file = tmp_path / "test_io.py"
    test_file.write_text("""
import sys
def test_io():
    print("HELLO STDOUT")
    print("HELLO STDERR", file=sys.stderr)
    assert True
""")

    res = run_cli(["--no-stream", str(test_file), "-s"], env=daemon_service["env"])
    assert "HELLO STDOUT" in res.stdout
    assert "HELLO STDERR" in res.stdout


def test_concurrency_lock(daemon_service: dict[str, Any], tmp_path: Any) -> None:
    """Verify that while one test is running, subsequent requests are rejected."""
    test_file = tmp_path / "test_sleep.py"
    test_file.write_text("import time\ndef test_sleep():\n    time.sleep(1)\n")

    results = {}

    def send_first_request() -> None:
        try:
            response = httpx.post(
                f"http://127.0.0.1:{daemon_service['port']}/v1/test/run",
                json={
                    "args": [str(test_file)],
                    "target_pkg": "sprintest",
                },
                timeout=10,
                trust_env=False,
            )
            results["res1"] = response.json()
        except Exception as e:
            results["res1_error"] = str(e)

    thread1 = threading.Thread(target=send_first_request)
    thread1.start()

    import time

    time.sleep(0.3)

    response2 = httpx.post(
        f"http://127.0.0.1:{daemon_service['port']}/v1/test/run",
        json={"args": [str(test_file)], "target_pkg": "sprintest"},
        timeout=5,
        trust_env=False,
    )

    thread1.join()
    assert response2.status_code == 429
    assert "Another test is already running" in response2.json()["detail"]


@pytest.mark.parametrize("daemon_config", ["unix"], indirect=True)
def test_unix_socket_integration(daemon_service: dict[str, Any], tmp_path: Any) -> None:
    """Verify communication via Unix Sockets."""
    test_file = tmp_path / "test_unix.py"
    test_file.write_text("def test_unix(): assert True\n")

    res = run_cli(["--no-stream", str(test_file)], env=daemon_service["env"])
    assert res.returncode == 0
    assert "passed" in res.stdout


@pytest.mark.parametrize("daemon_service", ["tcp"], indirect=True)
def test_graceful_stop(daemon_service: dict[str, Any]) -> None:
    """Verify daemon stops gracefully via stop endpoint."""
    port = daemon_service["port"]
    proc = daemon_service["proc"]

    httpx.post(f"http://127.0.0.1:{port}/v1/stop", trust_env=False)

    # Wait for process to exit
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pytest.fail("Daemon did not stop gracefully")
