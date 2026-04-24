import os
import subprocess
import sys
import time
from typing import Any

import httpx
import pytest

from sprintest import constants
from sprintest.paths import (
    get_lock_path,
    get_socket_path,
    get_sprintest_dir,
    get_status_path,
)


def test_daemon_path_drift_resilience(tmp_path: Any) -> None:
    """
    Verify that the daemon cleans up its original resource files even if
    a running test modifies os.environ["SPRINTEST_DIR"] and os.chdir()
    inside the daemon process.
    """
    # 1. Setup original paths
    original_dir = tmp_path / "original_sprintest"
    original_dir.mkdir()

    env = os.environ.copy()
    env["SPRINTEST_DIR"] = str(original_dir)
    env["SPRINTEST_FORCE_TCP"] = "1"
    # Remove SPRINTEST_PORT if inherited, so daemon auto-finds a free port
    env.pop("SPRINTEST_PORT", None)

    # Start the daemon
    proc = subprocess.Popen(
        [sys.executable, "-m", "sprintest.daemon"],
        env=env,
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        import json

        # Wait for daemon to reach 'ready' status (not just file existence).
        # 'ready' is written by the lifespan after uvicorn binds the port,
        # so we know the server is accepting connections.
        status_file = original_dir / "status.json"
        start_time = time.time()
        port: int | None = None
        while True:
            if status_file.exists():
                status = json.loads(status_file.read_text())
                if status.get("status") == "ready":
                    port = status["port"]
                    break
            if time.time() - start_time > 15:
                stdout, stderr = proc.communicate()
                details = ""
                if status_file.exists():
                    details = f"Last status: {status_file.read_text()}"
                pytest.fail(
                    f"Daemon failed to become ready. Stderr: {stderr}\n{details}"
                )
            time.sleep(0.1)

        # 2. Run a "nasty" test that tries to derail the daemon
        nasty_test = tmp_path / "test_nasty.py"
        drift_dir = tmp_path / "drifted_dir"
        drift_dir.mkdir()

        nasty_test.write_text(f"""
import os
def test_drift():
    os.environ["SPRINTEST_DIR"] = "{str(drift_dir)}"
    os.chdir("{str(tmp_path)}")
    print("Environment drifted!")
""")

        # Execute the nasty test via the daemon
        httpx.post(
            f"http://127.0.0.1:{port}/v1/test/run",
            json={"args": [str(nasty_test)], "target_pkg": "sprintest"},
            timeout=10,
        )

        # 3. Stop the daemon
        httpx.post(f"http://127.0.0.1:{port}/v1/stop", timeout=5)
        proc.wait(timeout=5)

        # 4. Assertions: Original files must be gone, drift_dir must be empty
        assert not (original_dir / "daemon.lock").exists(), "Lock file not cleaned!"
        assert not (original_dir / "status.json").exists(), "Status file not cleaned!"

        # Drift dir should NOT have any sprintest files (proves daemon didn't use the drifted env)
        assert not (drift_dir / "daemon.lock").exists()
        assert not (drift_dir / "status.json").exists()

    finally:
        if proc.poll() is None:
            proc.kill()


def test_logging_hijacking_resilience(monkeypatch: Any) -> None:
    """
    Verify that the daemon's logging remains functional even if pytest
    temporarily hijacks the root logger or standard streams.
    """
    import logging

    import pytest as pytest_mod

    from sprintest.runner import TestRunner

    runner = TestRunner()
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]

    # Nasty handler that would normally cause crashes
    class NastyHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise ValueError("I/O operation on closed file")

    nasty_handler = NastyHandler()

    # Mock pytest.main to simulate a test suite that adds a nasty handler
    def mock_pytest_main(args: list[str]) -> int:
        root_logger.addHandler(nasty_handler)
        return 0

    monkeypatch.setattr(pytest_mod, "main", mock_pytest_main)

    try:
        # TestRunner should isolate this
        runner.run_tests(["--version"], None)

        # Verify handlers were restored (nasty_handler should be gone)
        assert nasty_handler not in root_logger.handlers
        assert root_logger.handlers == original_handlers
    finally:
        if nasty_handler in root_logger.handlers:
            root_logger.removeHandler(nasty_handler)
