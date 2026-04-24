"""Test runner that delegates pytest execution to an isolated worker sub-process.

The worker is a long-lived child process that pre-loads the target package
(including heavy deps such as PyTorch) and stays resident between test runs.
This gives us the same "hot" speed as the original in-process approach,
but with complete isolation — if the worker OOMs or hangs, the daemon can
simply kill and re-spawn it without any impact on its own stability.

**Communication protocol** — JSON Lines over stdin / stdout:

Daemon → Worker (stdin):
    {"type": "run_test", "id": "<uuid>", "args": [...],
     "target_pkg": "pkg", "nuke": true}
    {"type": "shutdown"}

Worker → Daemon (stdout):
    {"type": "ready", "pid": 12345}
    {"type": "output", "id": "<uuid>", "data": "collected 5 items\\n"}
    {"type": "done",   "id": "<uuid>", "exit_code": 0}
    {"type": "error",  "id": "<uuid>", "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sprintest.context import DaemonContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utility — kept for backward-compatibility with existing imports
# ---------------------------------------------------------------------------

def clean_ansi(text: str) -> str:
    """Strip ANSI escape codes from *text*."""
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[mK]")
    return ansi_escape.sub("", text)


def prepare_pytest_args(args: list[str], enable_color: bool = False) -> list[str]:
    """Force color and add common warning filters (standalone utility)."""
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


# ---------------------------------------------------------------------------
# Constants for stuck detection
# ---------------------------------------------------------------------------

_SILENT_WARN_SEC = 30      # yield a warning after N seconds without output
_STUCK_CYCLE_SEC = 5.0     # polling interval for stuck detection
_WORKER_START_TIMEOUT = 60.0  # max seconds to wait for worker "ready"


# ---------------------------------------------------------------------------
# Worker sub-process manager
# ---------------------------------------------------------------------------

class TestRunner:
    """Manages a persistent worker sub-process that runs pytest.

    The worker is started lazily on the first call to :meth:`run_tests`.
    Subsequent calls reuse the same worker (and its pre-loaded packages).
    """

    __test__: bool = False

    def __init__(self, context: DaemonContext | None = None) -> None:
        # context is the DaemonContext — we only need a few fields
        if context is not None:
            self._target_pkg: str | None = context.target_pkg
            self._target_pkg_path: str | None = context.target_pkg_path
            self._cwd: str = context.cwd
            self._ignore_patterns: list[str] = context.ignore_patterns
        else:
            self._target_pkg = None
            self._target_pkg_path = None
            self._cwd = os.getcwd()
            self._ignore_patterns = []

        self._proc: asyncio.subprocess.Process | None = None
        self._start_lock: asyncio.Lock = asyncio.Lock()

    def prepare_pytest_args(
        self, args: list[str], enable_color: bool = False
    ) -> list[str]:
        """Delegate to standalone utility (backward-compat)."""
        return prepare_pytest_args(args, enable_color=enable_color)

    # -- public API -----------------------------------------------------------

    async def start(self) -> None:
        """Ensure the worker sub-process is running (idempotent)."""
        if self._proc is not None and self._proc.returncode is None:
            return
        async with self._start_lock:
            # Double-check after acquiring the lock
            if self._proc is not None and self._proc.returncode is None:
                return
            await self._spawn()

    async def stop(self) -> None:
        """Gracefully shut down the worker sub-process."""
        if self._proc is None or self._proc.returncode is not None:
            return
        proc = self._proc  # type-checker narrowing
        stdin = proc.stdin
        if stdin is None:
            return
        try:
            cmd = json.dumps({"type": "shutdown"}) + "\n"
            stdin.write(cmd.encode())
            await stdin.drain()
        except (BrokenPipeError, OSError):
            pass
        try:
            _ = await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            self._kill()

    async def restart(self) -> None:
        """Kill the current worker (if alive) and spawn a new one."""
        await self.stop()
        self._proc = None
        await self.start()

    async def run_tests(
        self,
        args: list[str],
        target_pkg: str | None,
        *,
        nuke: bool = True,
    ) -> AsyncGenerator[str, None]:
        """Run pytest in the worker and yield output chunks as they arrive.

        Yields:
            Output text chunks, including ``[STARTED]`` and ``[DONE]`` markers.
        """
        await self.start()
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            yield "\n❌ Worker process not available\n"
            yield "\n[DONE] exit_code=1\n"
            return
        stdin = proc.stdin
        stdout = proc.stdout

        run_id = str(uuid.uuid4())
        tgt = target_pkg or self._target_pkg

        # Send command to worker
        cmd = {
            "type": "run_test",
            "id": run_id,
            "args": args,
            "target_pkg": tgt,
            "nuke": nuke,
        }
        try:
            stdin.write((json.dumps(cmd) + "\n").encode())
            await stdin.drain()
        except (BrokenPipeError, OSError) as exc:
            yield f"\n❌ Worker communication error: {exc}\n"
            yield "\n[DONE] exit_code=1\n"
            return

        yield f"[STARTED] nuked {'yes' if nuke else 'no'}\n"

        last_output_time = time.monotonic()
        stuck_warning_shown = False
        exit_code = 1

        while True:
            try:
                line = await asyncio.wait_for(
                    stdout.readline(), timeout=_STUCK_CYCLE_SEC
                )
            except asyncio.TimeoutError:
                # ---- stuck detection ----------------------------------------
                silent = time.monotonic() - last_output_time

                if not stuck_warning_shown and silent >= _SILENT_WARN_SEC:
                    yield (
                        f"\n⚠️  Test appears to be stuck... "
                        f"(no output for {int(silent)}s)\n"
                    )
                    stuck_warning_shown = True

                if proc.returncode is not None:
                    yield (
                        f"\n❌ Worker process died unexpectedly "
                        f"(exit code {proc.returncode})\n"
                    )
                    yield "\n[DONE] exit_code=1\n"
                    return
                continue

            # EOF — worker closed stdout unexpectedly
            if not line:
                logger.warning("Worker stdout closed unexpectedly")
                yield "\n❌ Worker process disconnected unexpectedly\n"
                yield "\n[DONE] exit_code=1\n"
                return

            # Parse JSON line
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                logger.warning("Ignoring non-JSON from worker: %r", line[:200])
                continue

            if msg.get("id") != run_id:
                continue

            msg_type = msg.get("type")

            if msg_type == "output":
                data: str = msg.get("data", "")
                if data:
                    last_output_time = time.monotonic()
                    stuck_warning_shown = False
                    yield data
            elif msg_type == "done":
                exit_code = int(msg.get("exit_code", 1))
                break
            elif msg_type == "error":
                yield f"\n[WORKER ERROR] {msg.get('message', 'unknown')}\n"
                exit_code = 1
                break

        yield f"\n[DONE] exit_code={exit_code}\n"

    # -- internal helpers -----------------------------------------------------

    async def _spawn(self) -> None:
        """Spawn the worker sub-process."""
        # Build environment for the worker
        env = os.environ.copy()
        env["SPRINTEST_CWD"] = self._cwd
        env["SPRINTEST_IGNORE_PATTERNS"] = json.dumps(self._ignore_patterns)
        if self._target_pkg:
            env["SPRINTEST_TARGET_PKG"] = self._target_pkg
        if self._target_pkg_path:
            env["SPRINTEST_TARGET_PKG_PATH"] = self._target_pkg_path

        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "sprintest.worker_main",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # worker stderr → daemon log
            env=env,
            cwd=self._cwd,
        )

        logger.info("Worker sub-process started (PID %d)", self._proc.pid)

        stdout = self._proc.stdout
        if stdout is None:
            logger.error("Worker stdout pipe not available")
            self._kill()
            raise RuntimeError("Worker stdout pipe not available")

        # Wait for the "ready" signal
        try:
            line = await asyncio.wait_for(
                stdout.readline(), timeout=_WORKER_START_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error("Worker failed to send 'ready' within %ds", _WORKER_START_TIMEOUT)
            self._kill()
            raise RuntimeError("Worker process failed to start") from None

        if not line:
            logger.error("Worker died immediately after starting")
            self._kill()
            raise RuntimeError("Worker process died immediately after starting")

        try:
            msg = json.loads(line.decode())
        except json.JSONDecodeError as exc:
            logger.error("Unexpected worker output: %r", line[:200])
            self._kill()
            raise RuntimeError(f"Worker sent invalid startup message: {exc}") from None

        if msg.get("type") != "ready":
            logger.error("Expected 'ready' from worker, got: %r", msg)
            self._kill()
            raise RuntimeError(f"Unexpected worker startup message: {msg}")

        logger.info("Worker is ready (PID %d)", msg.get("pid", self._proc.pid))

    def _kill(self) -> None:
        """Force-kill the worker."""
        if self._proc is None:
            return
        try:
            self._proc.kill()
        except OSError:
            pass
        self._proc = None
