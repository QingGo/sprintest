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
            status = read_status()
            if status:
                logger.info("Daemon started successfully!")
                return True
        return False

    @contextmanager
    def _get_client(self) -> Generator[httpx.Client, None, None]:
        """Create an httpx.Client configured for the current daemon state."""
        status = read_status()

        # Priority 1: Environment variable port
        env_port = os.environ.get(constants.ENV_PORT)
        if env_port:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{env_port}", timeout=None, trust_env=False
            ) as client:
                yield client
                return

        # Priority 2: Status file (Unix or TCP)
        if status:
            if status.get("type") == "unix":
                transport = httpx.HTTPTransport(uds=status["socket_path"])
                with httpx.Client(
                    transport=transport,
                    base_url="http://127.0.0.1",
                    timeout=None,
                    trust_env=False,
                ) as client:
                    yield client
                    return
            else:
                port = status.get("port", self.default_port)
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=None, trust_env=False
                ) as client:
                    yield client
                    return

        # Priority 3: Start daemon if missing
        if self.start_daemon():
            status = read_status()
            if status:
                if status.get("type") == "unix":
                    transport = httpx.HTTPTransport(uds=status["socket_path"])
                    with httpx.Client(
                        transport=transport,
                        base_url="http://127.0.0.1",
                        timeout=None,
                        trust_env=False,
                    ) as client:
                        yield client
                        return
                else:
                    port = status.get("port", self.default_port)
                    with httpx.Client(
                        base_url=f"http://127.0.0.1:{port}",
                        timeout=None,
                        trust_env=False,
                    ) as client:
                        yield client
                        return

        # Final fallback
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
