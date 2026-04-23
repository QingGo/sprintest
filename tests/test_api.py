from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sprintest.context import DaemonContext
from sprintest.daemon import create_app
from sprintest.service import TestService
from sprintest.state import DaemonState


@pytest.fixture
def context() -> DaemonContext:
    return DaemonContext(
        lock_path="mock_lock",
        socket_path=None,
        status_path="mock_status",
        cwd=".",
        port=8000,
        target_pkg="sprintest",
        target_pkg_path=None,
        version="0.1.0",
        skip_uvicorn=False,
        ignore_patterns=[],
    )


@pytest.fixture
def app(context: DaemonContext) -> Any:
    state = DaemonState()
    return create_app(context, state)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


def test_api_run_test_success(client: TestClient, app: Any) -> None:
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


def test_api_run_test_busy(client: TestClient, app: Any) -> None:
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

def test_run_preloads_before_app(tmp_path: Any) -> None:
    """Verify run() calls write_status -> pre_load_package -> create_app in order.

    This validates the Fix 1 behavioral contract: pre-load happens BEFORE
    socket creation (create_app), and status.json is written before pre-load
    to signal the client to wait.
    """
    import threading
    from threading import Thread
    from unittest.mock import MagicMock, patch

    from sprintest.daemon import run

    call_order: list[str] = []
    exited = threading.Event()

    def fake_uvicorn_run(*args: Any, **kwargs: Any) -> None:
        exited.set()

    with (
        patch.dict(
            "os.environ",
            {
                "SPRINTEST_DIR": str(tmp_path),
                "SPRINTEST_FORCE_TCP": "1",
                "SPRINTEST_PORT": "19999",
                "SPRINTEST_TARGET_PKG": "sprintest",
            },
        ),
        patch("sprintest.daemon.acquire_daemon_lock", return_value=True),
        patch("sprintest.daemon.discover_package_path", return_value=str(tmp_path)),
        patch(
            "sprintest.daemon.write_status",
            side_effect=lambda d, path=None: call_order.append("write_status"),
        ),
        patch(
            "sprintest.daemon.pre_load_package",
            side_effect=lambda ctx: call_order.append("pre_load_package"),
        ),
        patch(
            "sprintest.daemon.create_app",
            side_effect=lambda ctx, state: call_order.append("create_app"),
        ),
        patch("uvicorn.run", side_effect=fake_uvicorn_run),
    ):
        thread = Thread(target=run, daemon=True)
        thread.start()
        assert exited.wait(timeout=2.0), "uvicorn.run was not called within timeout"
        thread.join(timeout=0.5)

    status_idx = call_order.index("write_status")
    preload_idx = call_order.index("pre_load_package")
    app_idx = call_order.index("create_app")
    assert status_idx < preload_idx, (
        f"write_status ({status_idx}) should be before "
        f"pre_load_package ({preload_idx})"
    )
    assert preload_idx < app_idx, (
        f"pre_load_package ({preload_idx}) should be before "
        f"create_app ({app_idx})"
    )


def test_pre_load_package_imports_package(tmp_path: Any) -> None:
    """Verify pre_load_package() calls importlib.import_module.

    This validates that the daemon actually imports the target
    package during the pre-load phase.
    """
    from unittest.mock import patch

    from sprintest.context import DaemonContext
    from sprintest.daemon import pre_load_package

    context = DaemonContext(
        lock_path=str(tmp_path / "lock"),
        socket_path=None,
        status_path=str(tmp_path / "status.json"),
        cwd=".",
        port=19999,
        target_pkg="sprintest",
        target_pkg_path=str(tmp_path),
        version="test",
        skip_uvicorn=True,
        ignore_patterns=[],
    )

    with patch("sprintest.daemon.importlib.import_module") as mock_import:
        pre_load_package(context)
        mock_import.assert_called_once_with("sprintest")
