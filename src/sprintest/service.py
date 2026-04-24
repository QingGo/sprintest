import asyncio
import logging
import re
from collections.abc import AsyncGenerator
from typing import Any

from sprintest.context import DaemonContext
from sprintest.runner import TestRunner

logger = logging.getLogger(__name__)


class TestService:
    """Business logic for running tests via a persistent worker sub-process."""

    __test__: bool = False

    def __init__(self, context: DaemonContext) -> None:
        self.context = context
        self.test_runner = TestRunner(context)
        self._lock: asyncio.Lock = asyncio.Lock()

    async def run_tests_stream(
        self,
        args: list[str],
        target_pkg: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Execute a test run and yield output chunks in real-time.

        Nuking (hot-reload) is handled inside the worker sub-process;
        the daemon no longer runs ``nuke.py`` directly.
        """
        target_pkg = target_pkg or self.context.target_pkg

        if self._lock.locked():
            yield "error:busy"
            return

        async with self._lock:
            async for chunk in self.test_runner.run_tests(
                args, target_pkg, nuke=True
            ):
                yield chunk

    async def run_tests(
        self,
        args: list[str],
        target_pkg: str | None = None,
    ) -> dict[str, Any]:
        """Execute a test run and return results (non-streaming)."""
        output_acc: list[str] = []
        exit_code = 0
        is_busy = False

        async for chunk in self.run_tests_stream(args, target_pkg):
            if chunk == "error:busy":
                is_busy = True
                break

            if chunk.startswith("\n[DONE]"):
                match = re.search(r"exit_code=(\d+)", chunk)
                if match:
                    exit_code = int(match.group(1))
                continue

            output_acc.append(chunk)

        if is_busy:
            return {
                "exit_code": 1,
                "output": "Another test is already running.",
                "nuked_modules_count": 0,
                "error": "busy",
            }

        return {
            "exit_code": exit_code,
            "output": "".join(output_acc),
            "nuked_modules_count": 0,  # nuking happens in-worker, not tracked here
        }
