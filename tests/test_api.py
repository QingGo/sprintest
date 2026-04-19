from unittest.mock import patch

from fastapi.testclient import TestClient

from sprintest.daemon import app, test_lock


def test_api_run_test_success() -> None:
    client = TestClient(app)
    payload = {"args": ["tests/test_foo.py"], "target_pkg": "sprintest"}

    with (
        patch("sprintest.daemon.pytest.main") as mock_pytest,
        patch("sprintest.daemon.nuke_modules") as mock_nuke,
    ):
        mock_pytest.return_value = 0
        mock_nuke.return_value = 5

        response = client.post("/v1/test/run", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0
        assert data["nuked_modules_count"] == 5
        mock_pytest.assert_called_once()
        mock_nuke.assert_called_once_with("sprintest")


def test_api_run_test_busy() -> None:
    client = TestClient(app)
    payload = {"args": ["tests/test_foo.py"], "target_pkg": "sprintest"}

    # Manually acquire the lock to simulate a busy daemon
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
    # No target_pkg in payload and we assume it's NOT in env for this test
    payload = {"args": ["tests/test_foo.py"]}

    with patch.dict("os.environ", {}, clear=True):
        response = client.post("/v1/test/run", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 1
        assert "target_pkg missing" in data["output"]
