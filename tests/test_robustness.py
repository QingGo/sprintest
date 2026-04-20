import json
import os
import subprocess
import time
from typing import Any

import httpx
import pytest

from sprintest import constants
from sprintest.client import DaemonClient
from sprintest.status import get_status_path, remove_socket, remove_status, write_status


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
