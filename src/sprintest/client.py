import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

import requests

from sprintest import constants
from sprintest.logger import logger
from sprintest.status import read_status


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

    def send_request(
        self, command: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a request to the daemon, handling both Unix and TCP."""
        env_port = os.environ.get(constants.ENV_PORT)
        if env_port:
            try:
                return self._send_tcp_request(env_port, command, payload or {})
            except Exception:
                pass

        status = read_status()
        if status:
            if status.get("type") == "unix":
                try:
                    return self._send_unix_request(
                        status["socket_path"], {"command": command, **(payload or {})}
                    )
                except Exception:
                    pass

            port = status.get("port", self.default_port)
            try:
                return self._send_tcp_request(port, command, payload or {})
            except Exception:
                pass

        if self.start_daemon():
            status = read_status()
            if status:
                if status.get("type") == "unix":
                    return self._send_unix_request(
                        status["socket_path"], {"command": command, **(payload or {})}
                    )
                else:
                    return self._send_tcp_request(
                        status.get("port", self.default_port), command, payload or {}
                    )
            else:
                p = os.environ.get(constants.ENV_PORT)
                if p:
                    return self._send_tcp_request(p, command, payload or {})

        return self._send_tcp_request(self.default_port, command, payload or {})

    def _send_unix_request(self, socket_path: str, request_data: dict) -> dict:
        """Send a request via Unix socket."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client_socket:
            client_socket.connect(socket_path)
            request_json = json.dumps(request_data).encode("utf-8") + b"\n\n"
            client_socket.sendall(request_json)

            response_data = b""
            while True:
                chunk = client_socket.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                if b"\n\n" in response_data:
                    break

            if not response_data:
                raise Exception("No response from daemon")

            return json.loads(response_data.decode("utf-8"))  # type: ignore

    def _send_tcp_request(self, port: int | str, command: str, payload: dict) -> dict:
        """Send a request via TCP/HTTP."""
        base_url = f"http://localhost:{port}/v1"

        if command == "status":
            resp = requests.get(f"{base_url}/status", timeout=constants.HTTP_TIMEOUT)
        elif command == "stop":
            resp = requests.post(f"{base_url}/stop", timeout=constants.HTTP_TIMEOUT)
        elif command == "run_test":
            resp = requests.post(f"{base_url}/test/run", json=payload, timeout=None)
        else:
            raise ValueError(f"Unknown command: {command}")

        resp.raise_for_status()
        return resp.json()  # type: ignore

    def stream_test_run(self, payload: dict[str, Any]) -> Any:
        """Stream test results from the daemon."""
        port = os.environ.get(constants.ENV_PORT)
        if not port:
            status = read_status()
            if status and status.get("type") == "tcp":
                port = status.get("port")
            else:
                port = self.default_port

        url = f"http://localhost:{port}/v1/test/run/stream"
        return requests.post(url, json=payload, timeout=None, stream=True)
