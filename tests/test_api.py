from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sprintest.context import DaemonContext
from sprintest.daemon import app
from sprintest.service import TestService


@pytest.fixture
def client() -> TestClient:
    context = DaemonContext(
        lock_path="mock_lock",
        socket_path=None,
        status_path="mock_status",
        cwd=".",
        port=8000,
        target_pkg="sprintest",
        target_pkg_path=None,
        version="0.1.0",
        skip_uvicorn=False,
    )
    app.state.context = context
    app.state.test_service = TestService(context)
    return TestClient(app)


def test_api_run_test_success(client: TestClient) -> None:
    payload = {"args": ["tests/test_foo.py"], "target_pkg": "sprintest"}

    test_service = app.state.test_service
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


def test_api_run_test_busy(client: TestClient) -> None:
    payload = {"args": ["tests/test_foo.py"], "target_pkg": "sprintest"}

    test_service = app.state.test_service
    with patch.object(test_service, "run_tests", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"error": "busy"}

        response = client.post("/v1/test/run", json=payload)
        assert response.status_code == 429
        assert "Another test is already running" in response.json()["detail"]


# def test_pre_load_logic(tmp_path: Any) -> None:
#     """Test the pre-load logic in run() function"""
#     from unittest.mock import patch

#     with (
#         patch.dict(
#             "os.environ",
#             {
#                 "SPRINTEST_TARGET_PKG": "sprintest",
#                 "SPRINTEST_PORT": "8000",
#                 "SPRINTEST_FORCE_TCP": "1",
#                 "SPRINTEST_SKIP_UVICORN": "1",
#                 "SPRINTEST_LOCK_FILE": os.path.join(tmp_path, "test_daemon.lock"),
#                 "SPRINTEST_DIR": tempfile.TemporaryDirectory(prefix="sprintest_test_").name,
#             },
#         ),
#         patch("importlib.import_module") as mock_import,
#         patch("sprintest.daemon.acquire_daemon_lock", return_value=True),
#     ):
#         from sprintest.daemon import run, stop

#         run()

#         sprintest_calls = [
#             c
#             for c in mock_import.call_args_list
#             if len(c[0]) > 0 and c[0][0] == "sprintest"
#         ]
#         assert len(sprintest_calls) > 0

#         stop()
