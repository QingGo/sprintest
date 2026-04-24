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

    # Make is_daemon_alive return True so the mock-written status passes
    # read_status() validation (the test process PID is not a sprintest daemon)
    import sprintest.status as sprintest_status
    monkeypatch.setattr(sprintest_status, "is_daemon_alive", lambda pid: True)

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


def test_status_verified_with_health_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify _get_client() verifies status.json with a health-check.

    Under the improved design, status.json is NOT trusted blindly. The client
    checks for status='ready' and performs a health check.
    """
    proc = subprocess.Popen(["sleep", "10"])
    try:
        # 1. Status exists but is not 'ready'
        write_status(
            {
                "pid": proc.pid,
                "type": "tcp",
                "port": 8890,
                "version": constants.VERSION,
                "start_time": time.time(),
                "status": "loading",
            }
        )

        client = DaemonClient(port="8890")
        called_start: list[bool] = []

        def mock_start() -> bool:
            called_start.append(True)
            # Update status to ready for the second read in _get_client
            write_status({
                "pid": proc.pid,
                "type": "tcp",
                "port": 8890,
                "version": constants.VERSION,
                "status": "ready"
            })
            return True

        monkeypatch.setattr(client, "start_daemon", mock_start)
        # Mock health check to fail for the first attempt
        monkeypatch.setattr(client, "_check_health", lambda: False)

        with client._get_client() as c:
            assert "8890" in str(c.base_url)

        assert len(called_start) == 1, "start_daemon should be called if status is not ready or health check fails"
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

    write_status({"test": "data"})
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

    from threading import Lock

    # Make is_daemon_alive return True so that threads recognize the
    # lock-holding process as alive (not a stale lock).
    # acquire_daemon_lock imported is_daemon_alive at module load time,
    # so we patch it in sprintest.daemon's namespace.
    import sprintest.daemon as sprintest_daemon
    from sprintest.daemon import acquire_daemon_lock
    from sprintest.paths import get_lock_path
    monkeypatch.setattr(sprintest_daemon, "is_daemon_alive", lambda pid: True)

    lock = Lock()
    results: list[bool] = []

    def attempt_lock() -> None:
        results.append(acquire_daemon_lock(get_lock_path(), lock))

    threads = [threading.Thread(target=attempt_lock) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread should have succeeded
    success_count = sum(1 for r in results if r is True)
    assert success_count == 1

    # Cleanup for next test
    try:
        os.remove(get_lock_path())
    except OSError:
        pass


def test_stream_monitor_starts_with_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify stream_test_run() starts a monitor thread when daemon PID is available.

    The monitor thread is responsible for detecting daemon death during
    streaming and aborting the connection early.
    """
    from unittest.mock import MagicMock, patch

    client = DaemonClient(port="9999")
    monkeypatch.setattr(client, "start_daemon", lambda: True)

    mock_http = MagicMock(spec=httpx.Client)
    mock_response = MagicMock(spec=httpx.Response)
    mock_http.stream.return_value.__enter__.return_value = mock_response
    monkeypatch.setattr(client, "_create_client", lambda s: mock_http)

    initial_count = threading.active_count()

    with patch("sprintest.client.read_status", return_value={
        "pid": 42, "type": "tcp", "port": "9999", "status": "ready"
    }):
        with client.stream_test_run({"args": ["test.py"]}) as _:
            # A daemon monitor thread should have been started
            assert threading.active_count() == initial_count + 1, (
                "Expected exactly one new thread for PID monitoring"
            )

    # After context manager exit, stop_monitor.set() stops the thread
    time.sleep(0.01)
    assert threading.active_count() == initial_count, (
        "Monitor thread should have exited after stop_monitor.set()"
    )


def test_stream_monitor_detects_daemon_death(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify monitor thread calls client.close() when daemon dies mid-stream.

    Uses a patched MONITOR_INTERVAL (10ms) so the test completes quickly
    instead of waiting for the default 1s interval.
    """
    from unittest.mock import MagicMock, patch

    client = DaemonClient(port="9999")
    monkeypatch.setattr(client, "start_daemon", lambda: True)

    mock_http = MagicMock(spec=httpx.Client)
    mock_response = MagicMock(spec=httpx.Response)
    mock_http.stream.return_value.__enter__.return_value = mock_response
    monkeypatch.setattr(client, "_create_client", lambda s: mock_http)

    with (
        patch.object(DaemonClient, "MONITOR_INTERVAL", 0.01),
        patch("sprintest.client.read_status", return_value={
            "pid": 42, "type": "tcp", "port": "9999", "status": "ready"
        }),
        patch("sprintest.client.is_daemon_alive", return_value=False),
    ):
        with client.stream_test_run({"args": ["test.py"]}) as _:
            pass

        # Give the monitor a moment to detect death and call close()
        time.sleep(0.05)
        mock_http.close.assert_called()
