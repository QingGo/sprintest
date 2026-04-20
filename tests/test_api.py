from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sprintest.daemon import app, test_lock


def test_api_run_test_success() -> None:
    client = TestClient(app)
    payload = {"args": ["tests/test_foo.py"], "target_pkg": "sprintest"}

    with patch("sprintest.daemon.test_runner.run_tests") as mock_run:
        mock_run.return_value = (0, "passed", 0)

        response = client.post("/v1/test/run", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0
        assert data["nuked_modules_count"] == 0
        mock_run.assert_called_once()


def test_api_run_test_second_run() -> None:
    client = TestClient(app)
    payload = {"args": ["tests/test_foo.py"], "target_pkg": "sprintest"}

    with patch("sprintest.daemon.test_runner.run_tests") as mock_run:
        mock_run.return_value = (0, "passed", 5)

        response = client.post("/v1/test/run", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0
        assert data["nuked_modules_count"] == 5
        mock_run.assert_called_once()


def test_api_run_test_busy() -> None:
    client = TestClient(app)
    payload = {"args": ["tests/test_foo.py"], "target_pkg": "sprintest"}

    test_lock.acquire()
    try:
        response = client.post("/v1/test/run", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 1
        assert "Error: Daemon is busy" in data["output"]
    finally:
        test_lock.release()


def test_api_run_test_missing_pkg() -> None:
    client = TestClient(app)
    payload = {"args": ["tests/test_foo.py"]}

    with patch.dict("os.environ", {}, clear=True):
        response = client.post("/v1/test/run", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 1
        assert "target_pkg missing" in data["output"]


def test_pre_load_logic() -> None:
    """Test the pre-load logic in run() function"""
    from unittest.mock import patch

    with (
        patch.dict(
            "os.environ",
            {
                "SPRINTEST_TARGET_PKG": "sprintest",
                "SPRINTEST_PORT": "8000",
                "SPRINTEST_FORCE_TCP": "1",
                "SPRINTEST_SKIP_UVICORN": "1",
            },
        ),
        patch("importlib.import_module") as mock_import,
        patch("sprintest.daemon.acquire_daemon_lock", return_value=True),
    ):
        from sprintest.daemon import run

        run()

        sprintest_calls = [
            c
            for c in mock_import.call_args_list
            if len(c[0]) > 0 and c[0][0] == "sprintest"
        ]
        assert len(sprintest_calls) > 0
