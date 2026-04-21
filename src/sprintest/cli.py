import logging
import os
import re
import sys
from typing import cast

import httpx

from sprintest import constants
from sprintest.client import DaemonClient
from sprintest.discovery import find_target_pkg
from sprintest.logger import setup_logger

logger = logging.getLogger(__name__)


def handle_status(client: DaemonClient) -> None:
    try:
        resp = client.send_request("status", no_auto_start=True)
        logger.info(f"Sprintest Daemon ({resp['version']}): {resp['status']}")
    except RuntimeError:
        logger.info("No Sprintest Daemon is currently running.")
    except (httpx.HTTPError, OSError) as e:
        logger.error(f"Could not reach daemon: {e}")
        sys.exit(1)


def handle_stop(client: DaemonClient) -> None:
    try:
        resp = client.send_request("stop", no_auto_start=True)
        logger.info(resp.get("message", "Stopping..."))
    except RuntimeError:
        logger.info("No Sprintest Daemon is currently running.")
    except (httpx.HTTPError, OSError) as e:
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
            with client.stream_test_run(payload) as resp:
                exit_code = 0
                for chunk in resp.iter_content(decode_unicode=True):
                    if chunk:
                        s_chunk = cast("str", chunk)
                        if "[DONE]" in s_chunk:
                            match = re.search(r"exit_code=(\d+)", s_chunk)
                            if match:
                                exit_code = int(match.group(1))
                        print(s_chunk, end="", flush=True)
                sys.exit(exit_code)
        else:
            result = client.send_request("run_test", payload)
            print(result.get("output", ""))
            sys.exit(result.get("exit_code", 0))
    except (httpx.HTTPError, OSError) as e:
        logger.error(f"Test run failed: {e}")
        sys.exit(1)


def main() -> None:
    
    setup_logger("sprintest")

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
