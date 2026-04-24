"""Worker sub-process for running pytest in complete isolation.

This process is spawned by the Sprintest daemon.  It:

1. Pre-loads the target package (incl. heavy deps such as torch).
2. Listens on **stdin** for JSON-Line commands (``run_test``, ``shutdown``).
3. For each ``run_test`` command, nukes the relevant modules (if requested),
   runs ``pytest.main()`` in-process, and streams captured output as JSON
   Lines back to the daemon via **stdout**.
4. Calls ``torch.cuda.empty_cache()`` after each run to prevent GPU memory
   accumulation.

Because the target package stays resident in the worker's ``sys.modules``
between runs, subsequent test runs are just as fast as the original in-process
approach — but when the worker OOMs or hangs, the daemon can simply kill and
re-spawn it without affecting its own stability.

Protocol (JSON Lines on stdout)
-------------------------------
Worker → Daemon (stdout):

.. code-block:: json

    {"type": "ready", "pid": 12345}
    {"type": "output", "id": "<run_id>", "data": "test output ..."}
    {"type": "done",   "id": "<run_id>", "exit_code": 0}
    {"type": "error",  "id": "<run_id>", "message": "..."}

Daemon → Worker (stdin):

.. code-block:: json

    {"type": "run_test", "id": "<run_id>", "args": ["..."],
     "target_pkg": "pkg", "nuke": true}
    {"type": "shutdown"}
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, cast

import pytest

from sprintest.nuke import NukeStrategy

try:
    import torch
except ImportError:
    torch = None  # type: ignore[assignment]

logging.basicConfig(
    level=logging.WARNING,
    stream=sys.stderr,
    format="%(asctime)s [WORKER] [%(levelname)s] %(message)s",
)
logger = logging.getLogger("worker")


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

original_stdout = sys.stdout


def send_message(msg: dict[str, Any]) -> None:
    """Write a JSON-Line message to the daemon via the original stdout."""
    line = json.dumps(msg, ensure_ascii=False, default=str)
    original_stdout.write(line + "\n")
    original_stdout.flush()


class _CaptureIO(io.TextIOBase):
    """Captures pytest output and forwards it as JSON-Line messages."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    def write(self, s: str) -> int:
        if s:
            send_message({"type": "output", "id": self.run_id, "data": s})
        return len(s)

    def flush(self) -> None:
        original_stdout.flush()

    def writable(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------

def _setup_environment() -> None:
    """Configure ``sys.path`` based on daemon-provided env vars."""
    cwd = os.environ.get("SPRINTEST_CWD", os.getcwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    pkg_path = os.environ.get("SPRINTEST_TARGET_PKG_PATH")
    if pkg_path and pkg_path not in sys.path:
        sys.path.insert(0, pkg_path)


def _preload_package() -> None:
    """Import the target package so it stays resident in the worker."""
    target_pkg = os.environ.get("SPRINTEST_TARGET_PKG")
    if not target_pkg:
        return

    pkg_name = target_pkg.replace("-", "_")
    try:
        __import__(pkg_name)
        logger.info("Pre-loaded package: %s", pkg_name)
    except ImportError as e:
        logger.warning("Failed to pre-load %s: %s", pkg_name, e)


def _load_ignore_patterns() -> list[str]:
    raw = os.environ.get("SPRINTEST_IGNORE_PATTERNS", "[]")
    try:
        return cast("list[str]", json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------

def _prepare_pytest_args(args: list[str], enable_color: bool = False) -> list[str]:
    """Prepare pytest arguments (mirrors runner.prepare_pytest_args)."""
    pytest_args = args.copy()
    color_found = False
    for i, arg in enumerate(pytest_args):
        if arg.startswith("--color="):
            pytest_args[i] = "--color=yes" if enable_color else "--color=no"
            color_found = True
            break
    if not color_found:
        pytest_args.append("--color=yes" if enable_color else "--color=no")
    pytest_args.extend(["-W", "ignore::pytest.PytestAssertRewriteWarning"])
    return pytest_args


def _run_test(run_id: str, args: list[str], target_pkg: str | None, nuke: bool) -> None:
    """Execute a single test run inside the worker."""
    nuke_strategy = NukeStrategy(_load_ignore_patterns())

    if nuke and target_pkg:
        nuked = nuke_strategy.nuke(target_pkg)
        logger.info("Nuked %d modules before test run", nuked)
    else:
        nuke_strategy.nuke_tests()

    pytest_args = _prepare_pytest_args(args, enable_color=False)
    capture = _CaptureIO(run_id)

    exit_code = 1
    try:
        with redirect_stdout(capture), redirect_stderr(capture):  # type: ignore[arg-type]
            exit_code = pytest.main(pytest_args)
    except Exception as exc:  # noqa: BLE001
        send_message({"type": "output", "id": run_id, "data": f"\n[WORKER ERROR] {exc}\n"})
        exit_code = 1

    # ---- CUDA memory cleanup ------------------------------------------------
    if torch is not None:
        try:
            torch.cuda.empty_cache()
            logger.debug("torch.cuda.empty_cache() called after test run")
        except Exception:  # noqa: BLE001
            logger.warning("torch.cuda.empty_cache() failed", exc_info=True)

    send_message({"type": "done", "id": run_id, "exit_code": int(exit_code)})


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    """Worker entry point — preload, signal ready, then loop over stdin commands."""
    _setup_environment()
    _preload_package()

    send_message({"type": "ready", "pid": os.getpid()})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid JSON from stdin: %r", line[:200])
            continue

        cmd_type = cmd.get("type")

        if cmd_type == "run_test":
            _run_test(
                run_id=cmd.get("id", ""),
                args=cmd.get("args", []),
                target_pkg=cmd.get("target_pkg"),
                nuke=cmd.get("nuke", True),
            )
        elif cmd_type == "shutdown":
            logger.info("Worker received shutdown command, exiting.")
            break
        else:
            logger.warning("Unknown command type: %s", cmd_type)

    sys.exit(0)


if __name__ == "__main__":
    main()
