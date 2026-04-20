import os
import re
import sys

from sprintest import constants
from sprintest.client import DaemonClient
from sprintest.discovery import find_target_pkg
from sprintest.logger import logger


def handle_status(client: DaemonClient) -> None:
    try:
        resp = client.send_request("status", no_auto_start=True)
        logger.info(f"Sprintest Daemon ({resp['version']}): {resp['status']}")
    except RuntimeError:
        logger.info("No Sprintest Daemon is currently running.")
    except Exception as e:
        logger.error(f"Could not reach daemon: {e}")
        sys.exit(1)


def handle_stop(client: DaemonClient) -> None:
    try:
        resp = client.send_request("stop", no_auto_start=True)
        logger.info(resp.get("message", "Stopping..."))
    except RuntimeError:
        logger.info("No Sprintest Daemon is currently running.")
    except Exception as e:
        logger.error(f"Could not stop daemon: {e}")
        sys.exit(1)


def handle_run(client: DaemonClient, args: list[str], use_stream: bool) -> None:
    target_pkg = os.environ.get(constants.ENV_TARGET_PKG)
    if not target_pkg:
        target_pkg = find_target_pkg()
        if not target_pkg:
            logger.error(
                f"{constants.ENV_TARGET_PKG} is required or must be discoverable"
            )
            sys.exit(1)
        logger.info(f"Auto-detected target package: {target_pkg}")

    payload = {"args": args, "target_pkg": target_pkg}

    try:
        if use_stream:
            resp = client.stream_test_run(payload)
            if hasattr(resp, "iter_content"):
                exit_code = 0
                for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
                    if chunk:
                        if "[DONE]" in chunk:
                            match = re.search(r"exit_code=(\d+)", chunk)
                            if match:
                                exit_code = int(match.group(1))
                        print(chunk, end="", flush=True)
                sys.exit(exit_code)
            else:
                # Fallback if stream_test_run returned a non-stream response (e.g. unix fallback)
                print(resp.get("output", ""))
                sys.exit(resp.get("exit_code", 0))
        else:
            resp = client.send_request("run_test", payload)
            print(resp.get("output", ""))
            sys.exit(resp.get("exit_code", 0))
    except Exception as e:
        logger.error(f"Test run failed: {e}")
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: sprintest [status|stop|--no-stream|--version] [pytest_args...]")
        sys.exit(0)

    if args[0] == "--version":
        print(f"sprintest {constants.VERSION}")
        sys.exit(0)

    client = DaemonClient(
        port=os.environ.get(constants.ENV_PORT, str(constants.DEFAULT_PORT))
    )

    if args[0] == "status":
        handle_status(client)
        return
    if args[0] == "stop":
        handle_stop(client)
        return

    use_stream = True
    if args[0] == "--no-stream":
        use_stream = False
        args = args[1:]
    elif args[0] == "--stream":
        use_stream = True
        args = args[1:]

    handle_run(client, args, use_stream)


if __name__ == "__main__":
    main()
