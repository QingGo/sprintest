from unittest.mock import call, patch

import pytest
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
        # First run skips nuke_modules, so nuked_modules_count is 0
        assert data["nuked_modules_count"] == 0
        mock_pytest.assert_called_once()
        # nuke_modules should not be called on first run
        mock_nuke.assert_not_called()


def test_api_run_test_second_run() -> None:
    """Test that nuke_modules is called on second run"""
    client = TestClient(app)
    payload = {"args": ["tests/test_foo.py"], "target_pkg": "sprintest"}

    with (
        patch("sprintest.daemon.pytest.main") as mock_pytest,
        patch("sprintest.daemon.nuke_modules") as mock_nuke,
    ):
        mock_pytest.return_value = 0
        mock_nuke.return_value = 5

        # Second run should call nuke_modules
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


def test_pre_load_logic() -> None:
    """Test the pre-load logic in run() function"""
    import importlib
    from unittest.mock import patch

    # Test with SPRINTEST_TARGET_PKG set
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
    ):
        from sprintest.daemon import run

        # Run the function
        run()

        # Verify importlib.import_module was called with the target package
        sprintest_calls = [
            c
            for c in mock_import.call_args_list
            if len(c[0]) > 0 and c[0][0] == "sprintest"
        ]
        assert len(sprintest_calls) > 0

    # Test with SPRINTEST_TARGET_PKG not set
    with (
        patch.dict(
            "os.environ",
            {
                "SPRINTEST_PORT": "8000",
                "SPRINTEST_FORCE_TCP": "1",
                "SPRINTEST_SKIP_UVICORN": "1",
            },
            clear=True,
        ),
        patch("importlib.import_module") as mock_import,
    ):
        from sprintest.daemon import run

        # Run the function
        run()

        # Verify importlib.import_module was not called with the target package
        sprintest_calls = [
            c
            for c in mock_import.call_args_list
            if len(c[0]) > 0 and c[0][0] == "sprintest"
        ]
        assert len(sprintest_calls) == 0
