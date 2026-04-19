import os
import sys

import requests  # type: ignore

PORT = os.environ.get("SPRINTEST_PORT", "8000")
DAEMON_URL = f"http://localhost:{PORT}/v1/test/run"


def main() -> None:
    args = sys.argv[1:]
    target_pkg = os.environ.get("SPRINTEST_TARGET_PKG")

    payload = {"args": args, "target_pkg": target_pkg}
    try:
        response = requests.post(DAEMON_URL, json=payload, timeout=30)
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
