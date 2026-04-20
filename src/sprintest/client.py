import os
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

import httpx

from sprintest import constants
from sprintest.logger import logger
from sprintest.status import read_status


class RequestsShim:
    def __init__(self, response: httpx.Response):
        self.response = response

    def iter_content(
        self, chunk_size: int | None = None, decode_unicode: bool = False
    ) -> Generator[str | bytes, None, None]:
        if decode_unicode:
            yield from self.response.iter_text()
        else:
            yield from self.response.iter_bytes()

    def json(self) -> Any:
        return self.response.json()

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self.response.json().get(key, default)
        except Exception:
            return default


class DaemonClient:
    def __init__(self, port: str = "8000"):
        self.default_port = port

    def _check_health(self) -> bool:
        """Check if the daemon is responding to health checks."""
        try:
            with self._get_client() as client:
                resp = client.get("/v1/status", timeout=0.1)
                return resp.status_code == 200
        except Exception:
            return False

    def start_daemon(self) -> bool:
        """Start the daemon process if not running."""
        status = read_status()
        if status:
            return True

        logger.info("Starting Sprintest Daemon...")
        subprocess.Popen(
            [sys.executable, "-m", "sprintest.daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        for _ in range(constants.DAEMON_START_RETRIES):
            time.sleep(constants.DAEMON_START_WAIT)
            if read_status():
                if self._check_health():
                    logger.info("Daemon started successfully!")
                    return True

        logger.error("Daemon failed to start or respond within timeout.")
        return False

    def _create_client(self, status: dict[str, Any]) -> httpx.Client:
        """Create an httpx.Client from status data."""
        if status.get("type") == "unix":
            transport = httpx.HTTPTransport(uds=status["socket_path"])
            return httpx.Client(
                transport=transport,
                base_url="http://127.0.0.1",
                timeout=None,
                trust_env=False,
            )
        else:
            port = status.get("port", self.default_port)
            return httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=None, trust_env=False
            )

    @contextmanager
    def _get_client(self) -> Generator[httpx.Client, None, None]:
        """Create an httpx.Client configured for the current daemon state."""
        # 1. Environment variable port override
        env_port = os.environ.get(constants.ENV_PORT)
        if env_port:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{env_port}", timeout=None, trust_env=False
            ) as client:
                yield client
                return

        # 2. Try existing status
        status = read_status()
        if status:
            daemon_type = status.get("type", "unknown")
            daemon_id = status.get("pid", "unknown")
            logger.debug(f"Found existing {daemon_type} daemon (PID: {daemon_id})")

            client = self._create_client(status)
            try:
                # Quick health check
                resp = client.get("/v1/status", timeout=0.2)
                if resp.status_code == 200:
                    with client:
                        yield client
                        return
            except Exception as e:
                logger.debug(f"Existing daemon is not responsive: {e}")
            client.close()
        else:
            logger.debug("No active daemon status found.")

        # 3. Start daemon if missing or dead
        if self.start_daemon():
            status = read_status()
            if status:
                with self._create_client(status) as client:
                    yield client
                    return

        # 4. Final fallback
        logger.debug(f"Using final fallback to TCP port {self.default_port}")
        with httpx.Client(
            base_url=f"http://127.0.0.1:{self.default_port}",
            timeout=None,
            trust_env=False,
        ) as client:
            yield client

    def send_request(
        self, command: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a request to the daemon."""
        with self._get_client() as client:
            if command == "status":
                resp = client.get("/v1/status", timeout=constants.HTTP_TIMEOUT)
            elif command == "stop":
                resp = client.post("/v1/stop", timeout=constants.HTTP_TIMEOUT)
            elif command == "run_test":
                resp = client.post("/v1/test/run", json=payload)
            else:
                raise ValueError(f"Unknown command: {command}")

            resp.raise_for_status()
            return cast("dict[str, Any]", resp.json())

    def stream_test_run(self, payload: dict[str, Any]) -> Any:
        """Stream test results from the daemon."""
        self.start_daemon()
        status = read_status()
        if status and status.get("type") == "unix":
            transport = httpx.HTTPTransport(uds=status["socket_path"])
            client = httpx.Client(
                transport=transport,
                base_url="http://127.0.0.1",
                timeout=None,
                trust_env=False,
            )
        else:
            port = (status or {}).get(
                "port", os.environ.get(constants.ENV_PORT, self.default_port)
            )
            client = httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=None, trust_env=False
            )

        resp = client.post("/v1/test/run/stream", json=payload)
        return RequestsShim(resp)
