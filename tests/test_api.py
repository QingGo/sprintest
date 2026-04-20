from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from sprintest.daemon import app, test_service


def test_api_run_test_success() -> None:
    client = TestClient(app)
    payload = {"args": ["tests/test_foo.py"], "target_pkg": "sprintest"}

    with patch.object(test_service, "run_tests", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {
            "exit_code": 0,
            "output": "passed",
            "nuked_modules_count": 0,
        }

        response = client.post("/v1/test/run", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0
        assert data["nuked_modules_count"] == 0
        mock_run.assert_called_once()


def test_api_run_test_busy() -> None:
    client = TestClient(app)
    payload = {"args": ["tests/test_foo.py"], "target_pkg": "sprintest"}

    with patch.object(test_service, "run_tests", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"error": "busy"}

        response = client.post("/v1/test/run", json=payload)
        assert response.status_code == 429
        assert "Another test is already running" in response.json()["detail"]


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
