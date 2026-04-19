import os
import sys

import requests

PORT = os.environ.get("STEST_PORT", "8000")
DAEMON_URL = f"http://localhost:{PORT}/v1/test/run"


def main():
    raw_args = sys.argv[1:]
    args = []
    target_pkg = "my_project"

    i = 0
    while i < len(raw_args):
        if raw_args[i] == "--target_pkg" and i + 1 < len(raw_args):
            target_pkg = raw_args[i + 1]
            i += 2
        else:
            args.append(raw_args[i])
            i += 1

    payload = {"args": args, "target_pkg": target_pkg}

    response = requests.post(DAEMON_URL, json=payload)
    response.raise_for_status()

    data = response.json()
    print(data["output"])
    sys.exit(data["exit_code"])
