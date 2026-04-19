import json
import os
import re
import sys

import requests  # type: ignore

from sprintest import __version__

PORT = os.environ.get("SPRINTEST_PORT", "8000")
DAEMON_URL = f"http://localhost:{PORT}/v1/test/run"
STREAM_URL = f"http://localhost:{PORT}/v1/test/run/stream"


def main() -> None:

    args = sys.argv[1:]

    if not args:
        print("Usage: sprintest [status|stop|--no-stream|--version] [pytest_args...]")
        sys.exit(0)

    if args[0] == "--version":
        print(f"sprintest {__version__}")
        sys.exit(0)

    if args[0] == "status":
        try:
            response = requests.get(f"http://localhost:{PORT}/v1/status", timeout=5)
            response.raise_for_status()
            try:
                data = response.json()
                print(f"Sprintest Daemon ({data['version']}): {data['status']}")
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Error: Invalid response from daemon: {e}")
                sys.exit(1)
        except Exception as e:
            print(f"Error: Could not reach daemon: {e}")
            sys.exit(1)
        sys.exit(0)

    if args[0] == "stop":
        try:
            response = requests.post(f"http://localhost:{PORT}/v1/stop", timeout=5)
            response.raise_for_status()
            try:
                print(response.json()["message"])
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Error: Invalid response from daemon: {e}")
                sys.exit(1)
        except Exception as e:
            print(f"Error: Could not stop daemon: {e}")
            sys.exit(1)
        sys.exit(0)

    use_stream = True
    if args[0] == "--no-stream":
        use_stream = False
        args = args[1:]
    elif args[0] == "--stream":
        use_stream = True
        args = args[1:]

    target_pkg = os.environ.get("SPRINTEST_TARGET_PKG")
    payload = {"args": args, "target_pkg": target_pkg}

    try:
        if use_stream:
            response = requests.post(
                STREAM_URL, json=payload, timeout=None, stream=True
            )
            response.raise_for_status()
            exit_code = 0
            for chunk in response.iter_content(chunk_size=None, decode_unicode=True):
                if chunk:
                    if "[DONE]" in chunk:
                        match = re.search(r"exit_code=(\d+)", chunk)
                        if match:
                            exit_code = int(match.group(1))
                    print(chunk, end="", flush=True)
            sys.exit(exit_code)
        else:
            response = requests.post(DAEMON_URL, json=payload, timeout=None)
            response.raise_for_status()
            try:
                data = response.json()
                print(data["output"])
                sys.exit(data["exit_code"])
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Error: Invalid response from daemon: {e}")
                sys.exit(1)
    except requests.exceptions.ConnectionError:
        print(
            f"Error: Sprintest Daemon not found on port {PORT}. Please start it with 'sprintest-daemon'."
        )
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")
        sys.exit(1)
