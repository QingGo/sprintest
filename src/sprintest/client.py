import logging
import os
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from threading import Event, Lock, Thread
from typing import Any, cast

import httpx

from sprintest import constants
from sprintest.paths import ensure_sprintest_dir
from sprintest.status import (
    is_daemon_alive,
    read_lock_pid,
    read_status,
    remove_socket,
    remove_status,
)

logger = logging.getLogger(__name__)


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
        except (ValueError, TypeError, KeyError) as e:
            logger.debug(f"Failed to parse JSON response for key '{key}': {e}")
            return default


class DaemonClient:
    MONITOR_INTERVAL = 1.0

    def __init__(self, port: str = "8000"):
        self.default_port = port
        self.internal_lock = Lock()

    def _check_health(self) -> bool:
        """Check if the daemon is responding to health checks."""
        status = read_status()
        if not status:
            return False

        try:
            with self._create_client(status) as client:
                resp = client.get("/v1/status")
                return resp.status_code == 200
        except (httpx.HTTPError, OSError) as e:
            logger.debug(f"Could not connect to daemon: {e}")
            return False

    def start_daemon(self) -> bool:
        """Ensure the daemon is running, starting it if necessary.

        Uses the lock file as a "daemon is starting" sentinel so that a
        second concurrent call never spawns a duplicate process.
        """
        with self.internal_lock:
            # Already fully ready? Check if it responds to health check.
            status = read_status()
            if status and status.get("status") == "ready":
                if self._check_health():
                    return True
                logger.debug("Existing daemon 'ready' but not responding. Re-starting...")

            # Lock file present with a live PID means a daemon is in the middle
            # of starting (it has acquired the lock but lifespan hasn't run yet).
            # Skip spawning and just wait for status.json to appear.
            alive_pid = read_lock_pid()
            spawned_pid = None
            if not alive_pid:
                # Clean up any stale artifacts (socket, status) from a dead or
                # zombie daemon before spawning a new one.
                remove_socket()
                remove_status()

                logger.info("Starting Sprintest Daemon...")
                ensure_sprintest_dir()
                log_path = os.path.join(constants.SPRINTEST_DIR, constants.LOG_FILE)
                log_file = open(log_path, "a")

                popen_kwargs: dict[str, Any] = {}
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                else:
                    popen_kwargs["start_new_session"] = True

                proc = subprocess.Popen(
                    [sys.executable, "-m", "sprintest.daemon"],
                    stdout=log_file,
                    stderr=log_file,
                    **popen_kwargs,
                )
                spawned_pid = proc.pid
            else:
                logger.debug(
                    f"Daemon (PID {alive_pid}) is starting, waiting for it to be ready..."
                )

        # Wait for daemon to be ready
        last_feedback = 0.0
        consecutive_missing_status = 0
        
        # We need the PID to monitor survival. 
        # If we spawned it, we use the captured PID. 
        # Otherwise, we use whatever was in the lock file.
        monitored_pid = spawned_pid or alive_pid or read_lock_pid()

        for _i in range(constants.DAEMON_START_RETRIES * 10): # Allow more retries if process is alive
            # 1. Check if the process is still alive
            if monitored_pid and not is_daemon_alive(monitored_pid):
                # Process died! Check why.
                status = read_status()
                if not status:
                    logger.error(f"Daemon process (PID {monitored_pid}) died before writing status. Check .sprintest/daemon.log")
                else:
                    logger.error(f"Daemon process (PID {monitored_pid}) died during '{status.get('status')}' phase.")
                return False

            # 2. Check status file
            status = read_status()
            if status:
                consecutive_missing_status = 0
                state = status.get("status")
                if state == "ready":
                    if self._check_health():
                        logger.info("Daemon started successfully!")
                        return True
                elif state == "loading":
                    now = time.time()
                    if now - last_feedback > 10.0:
                        logger.info("Daemon is pre-loading packages, please wait...")
                        last_feedback = now
                    # As long as it's loading and alive, we continue the loop
                else:
                    if self._check_health():
                        now = time.time()
                        if now - last_feedback > 10.0:
                            logger.info("Daemon is busy, please wait...")
                            last_feedback = now
                        return True
            else:
                consecutive_missing_status += 1
                if consecutive_missing_status > constants.DAEMON_START_RETRIES:
                    logger.error("Daemon process is alive but failed to initialize status file (potential deadlock).")
                    return False

            time.sleep(constants.DAEMON_START_WAIT)

        logger.error("Daemon failed to become ready within maximum timeout.")
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
        """
        # 1. Environment variable port override
        env_port = os.environ.get(constants.ENV_PORT)
        if env_port:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{env_port}", timeout=None, trust_env=False
            ) as client:
                yield client
                return

        # 2. Try existing status — only use if ready and responding
        status = read_status()
        if status and status.get("status") == "ready":
            if self._check_health():
                daemon_type = status.get("type", "unknown")
                daemon_id = status.get("pid", "unknown")
                logger.debug(f"Found existing {daemon_type} daemon (PID: {daemon_id})")
                with self._create_client(status) as client:
                    yield client
                    return
            else:
                logger.debug("Existing daemon found but not responding. Re-starting...")
        elif status:
            logger.debug(f"Existing daemon found in state: {status.get('status')}")

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

    @contextmanager
    def stream_test_run(
        self, payload: dict[str, Any]
    ) -> Generator[RequestsShim, None, None]:
        """Stream test results from the daemon."""
        if not self.start_daemon():
            raise RuntimeError("Failed to start or connect to Sprintest Daemon.")

        status = read_status()
        if not status:
            raise RuntimeError("Daemon is reported as ready but status.json is missing.")

        daemon_pid = status.get("pid")
        client = self._create_client(status)
        stop_monitor = Event()

        if daemon_pid:
            def monitor_daemon() -> None:
                while not stop_monitor.wait(self.MONITOR_INTERVAL):
                    if not is_daemon_alive(daemon_pid):
                        logger.warning(
                            f"Daemon (PID {daemon_pid}) died during streaming, "
                            "aborting connection."
                        )
                        try:
                            client.close()
                        except (httpx.HTTPError, OSError) as _e:
                            logger.debug(f"Error closing client after daemon death: {_e}")
                        break
            Thread(target=monitor_daemon, daemon=True).start()

        try:
            with client.stream("POST", "/v1/test/run/stream", json=payload) as resp:
                resp.raise_for_status()
                yield RequestsShim(resp)
        finally:
            stop_monitor.set()
            client.close()
