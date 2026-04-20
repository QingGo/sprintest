import json
import os
import subprocess
import threading
import time
from typing import Any

import httpx
import pytest

from sprintest import constants
from sprintest.client import DaemonClient
from sprintest.paths import get_sprintest_dir, get_status_path
from sprintest.status import read_status, remove_socket, remove_status, write_status


def test_stale_status_recovery(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify client can recover from a status file with a dead PID."""
    remove_status()
    remove_socket()

    # Create a fake status file with a PID that is likely not running
    fake_pid = 999999
    write_status(
        {
            "pid": fake_pid,
            "type": "tcp",
            "port": 8888,
            "version": "0.0.0",
            "start_time": time.time(),
        }
    )

    client = DaemonClient(port="8888")

    # Let's mock start_daemon to just return True and write a valid status
    def mock_start() -> bool:
        write_status(
            {
                "pid": os.getpid(),
                "type": "tcp",
                "port": 8889,
                "version": constants.VERSION,
                "start_time": time.time(),
            }
        )
        return True

    monkeypatch.setattr(client, "start_daemon", mock_start)

    with client._get_client() as c:
        assert str(c.base_url) == "http://127.0.0.1:8889"

    # Cleanup
    remove_status()


def test_unresponsive_daemon_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify client restarts daemon if it's alive but not responding to HTTP."""
    # Start a dummy process that doesn't listen on the port
    proc = subprocess.Popen(["sleep", "10"])
    try:
        write_status(
            {
                "pid": proc.pid,
                "type": "tcp",
                "port": 8890,
                "version": constants.VERSION,
                "start_time": time.time(),
            }
        )

        client = DaemonClient(port="8890")

        # Mock start_daemon to verify it's called
        called: list[bool] = []

        def mock_start() -> bool:
            called.append(True)
            return False  # Stop here

        monkeypatch.setattr(client, "start_daemon", mock_start)

        # Should attempt to start daemon because 8890 is unresponsive
        try:
            with client._get_client():
                pass
        except Exception:
            pass

        assert len(called) > 0
    finally:
        proc.terminate()
        remove_status()


def test_isolation_via_env_var(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that setting SPRINTEST_DIR correctly isolates the state."""
    custom_dir = tmp_path / "custom_sprintest"
    custom_dir.mkdir()

    monkeypatch.setenv("SPRINTEST_DIR", str(custom_dir))

    # This should create the directory and status file in the custom location
    from sprintest.paths import ensure_sprintest_dir

    path = ensure_sprintest_dir()
    assert path == str(custom_dir)
    assert custom_dir.exists()

    write_status({"test": "data", "pid": os.getpid()})
    status_file = custom_dir / constants.STATUS_FILE
    assert status_file.exists()

    # Verify read_status uses the same location
    data = read_status()
    assert data is not None
    assert data["test"] == "data"


def test_concurrency_lock_atomicity(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify lock atomicity by having multiple threads attempt to start daemon simultaneously."""
    monkeypatch.setenv("SPRINTEST_DIR", str(tmp_path))

    from sprintest.daemon import acquire_daemon_lock, release_daemon_lock

    results: list[bool] = []

    def attempt_lock() -> None:
        results.append(acquire_daemon_lock())

    threads = [threading.Thread(target=attempt_lock) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread should have succeeded
    success_count = sum(1 for r in results if r is True)
    assert success_count == 1

    # Cleanup for next test
    release_daemon_lock()
