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


def test_status_trusted_without_health_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify _get_client() trusts status.json without a health-check round-trip.

    Under the new design, status.json is written only after uvicorn's lifespan
    runs (i.e., the socket is bound and requests can be accepted).  Therefore
    the client must NOT perform a separate health check; it should use the
    connection details from status.json directly.
    """
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

        # _get_client() should yield a client pointed at port 8890
        # without calling start_daemon() at all.
        called: list[bool] = []

        def mock_start() -> bool:
            called.append(True)
            return False

        monkeypatch.setattr(client, "start_daemon", mock_start)

        try:
            with client._get_client() as c:
                assert "8890" in str(c.base_url)
        except Exception:
            # Connection will be refused (no server listening) — that's expected;
            # the important assertion is that start_daemon was NOT called.
            pass

        assert len(called) == 0, (
            "start_daemon should not be called when status.json is present"
        )
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

    from threading import Lock

    from sprintest.daemon import acquire_daemon_lock
    from sprintest.paths import get_lock_path

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
