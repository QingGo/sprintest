from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from sprintest.context import DaemonContext
from sprintest.daemon import create_app
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


def test_run_preloads_before_app(tmp_path: Any) -> None:
    """Verify run() calls write_status -> create_app in order.

    Pre-loading has been moved to the worker sub-process (worker_main.py),
    so the daemon's run() only calls write_status then create_app.
    """
    import threading
    from threading import Thread
    from unittest.mock import patch

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
        patch(
            "sprintest.daemon.write_status",
            side_effect=lambda d, path=None: call_order.append("write_status"),
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
    app_idx = call_order.index("create_app")
    assert status_idx < app_idx, (
        f"write_status ({status_idx}) should be before "
        f"create_app ({app_idx})"
    )

