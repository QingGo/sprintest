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
from sprintest.paths import ensure_sprintest_dir
from sprintest.status import read_lock_pid, read_status


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
        status = read_status()
        if not status:
            return False

        try:
            with self._create_client(status) as client:
                resp = client.get("/v1/status")
                return resp.status_code == 200
        except Exception as e:
            logger.debug(f"Could not connect to daemon: {e}")
            return False

    def start_daemon(self) -> bool:
        """Ensure the daemon is running, starting it if necessary.

        Uses the lock file as a "daemon is starting" sentinel so that a
        second concurrent call never spawns a duplicate process.
        """
        # Already fully ready?
        status = read_status()
        if status:
            return True

        # Lock file present with a live PID means a daemon is in the middle
        # of starting (it has acquired the lock but lifespan hasn't run yet).
        # Skip spawning and just wait for status.json to appear.
        alive_pid = read_lock_pid()
        if not alive_pid:
            logger.info("Starting Sprintest Daemon...")
            ensure_sprintest_dir()
            log_path = os.path.join(constants.SPRINTEST_DIR, constants.LOG_FILE)
            log_file = open(log_path, "a")
            subprocess.Popen(
                [sys.executable, "-m", "sprintest.daemon"],
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
        else:
            logger.debug(
                f"Daemon (PID {alive_pid}) is starting, waiting for it to be ready..."
            )

        # status.json is written inside the daemon's FastAPI lifespan, which
        # runs only after uvicorn has bound its socket and is ready to serve.
        # Polling read_status() is therefore sufficient — no separate health
        # check round-trip is needed.
        for _ in range(constants.DAEMON_START_RETRIES):
            time.sleep(constants.DAEMON_START_WAIT)
            if read_status():
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
    def _get_existing_client(self) -> Generator[httpx.Client, None, None]:
        """Connect to an already-running daemon WITHOUT auto-starting one.

        Raises RuntimeError if no live daemon is found.  Use this for
        commands like ``stop`` and ``status`` that must not accidentally
        spin up a fresh daemon process.
        """
        # 1. Environment variable port override
        env_port = os.environ.get(constants.ENV_PORT)
        if env_port:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{env_port}", timeout=None, trust_env=False
            ) as client:
                yield client
                return

        # 2. Read existing status (read_status() already removes stale files)
        status = read_status()
        if not status:
            raise RuntimeError("No running daemon found.")

        daemon_type = status.get("type", "unknown")
        daemon_id = status.get("pid", "unknown")
        logger.debug(f"Found existing {daemon_type} daemon (PID: {daemon_id})")
        with self._create_client(status) as client:
            yield client

    @contextmanager
    def _get_client(self) -> Generator[httpx.Client, None, None]:
        """Create an httpx.Client configured for the current daemon state.

        Starts a new daemon automatically if none is running.
        status.json is written only after uvicorn is ready (in lifespan),
        so a non-None read_status() result means the daemon is connectable.
        """
        # 1. Environment variable port override
        env_port = os.environ.get(constants.ENV_PORT)
        if env_port:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{env_port}", timeout=None, trust_env=False
            ) as client:
                yield client
                return

        # 2. Try existing status — if it exists, uvicorn is ready
        status = read_status()
        if status:
            daemon_type = status.get("type", "unknown")
            daemon_id = status.get("pid", "unknown")
            logger.debug(f"Found existing {daemon_type} daemon (PID: {daemon_id})")
            with self._create_client(status) as client:
                yield client
                return
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
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        no_auto_start: bool = False,
    ) -> dict[str, Any]:
        """Send a request to the daemon.

        Args:
            command: One of ``status``, ``stop``, ``run_test``.
            payload: Optional JSON body.
            no_auto_start: If True, use :meth:`_get_existing_client` and
                never start a new daemon.  Raises ``RuntimeError`` if no
                daemon is currently running.
        """
        ctx = self._get_existing_client() if no_auto_start else self._get_client()
        with ctx as client:
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
