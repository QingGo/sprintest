import os
import sys

import requests  # type: ignore

from sprintest import __version__

PORT = os.environ.get("SPRINTEST_PORT", "8000")
DAEMON_URL = f"http://localhost:{PORT}/v1/test/run"


def main() -> None:

    args = sys.argv[1:]

    # CLI command handling
    if not args:
        print("Usage: sprintest [status|stop|--version] [pytest_args...]")
        sys.exit(0)

    if args[0] == "--version":
        print(f"sprintest {__version__}")
        sys.exit(0)

    if args[0] == "status":
        try:
            response = requests.get(f"http://localhost:{PORT}/v1/status", timeout=5)
            response.raise_for_status()
            data = response.json()
            print(f"Sprintest Daemon ({data['version']}): {data['status']}")
        except Exception as e:
            print(f"Error: Could not reach daemon: {e}")
            sys.exit(1)
        sys.exit(0)

    if args[0] == "stop":
        try:
            response = requests.post(f"http://localhost:{PORT}/v1/stop", timeout=5)
            response.raise_for_status()
            print(response.json()["message"])
        except Exception as e:
            print(f"Error: Could not stop daemon: {e}")
            sys.exit(1)
        sys.exit(0)

    # Default: Run tests
    target_pkg = os.environ.get("SPRINTEST_TARGET_PKG")
    payload = {"args": args, "target_pkg": target_pkg}
    try:
        response = requests.post(
            DAEMON_URL, json=payload, timeout=None
        )  # No timeout for tests
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        print(
            f"Error: Sprintest Daemon not found on port {PORT}. Please start it with 'sprintest-daemon'."
        )
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")
        sys.exit(1)

    data = response.json()
    print(data["output"])
    sys.exit(data["exit_code"])
