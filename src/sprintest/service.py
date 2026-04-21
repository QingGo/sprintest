import asyncio
import logging
import re
import threading
from collections.abc import AsyncGenerator
from typing import Any

from sprintest.context import DaemonContext
from sprintest.nuke import NukeStrategy
from sprintest.runner import TestRunner

logger = logging.getLogger(__name__)


class TestService:
    """Business logic for running tests and managing hot-reloading."""

    __test__ = False

    def __init__(self, context: DaemonContext) -> None:
        self.context = context
        self.test_runner = TestRunner()
        self.nuke_strategy = NukeStrategy(context.ignore_patterns)
        self.test_lock = threading.Lock()
        self.first_run = True

    async def run_tests_stream(
        self,
        args: list[str],
        target_pkg: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Execute a test run and yield output chunks in real-time."""
        target_pkg = target_pkg or self.context.target_pkg

        if not self.test_lock.acquire(blocking=False):
            yield "error:busy"
            return

        try:
            logger.info(f"Starting test run for package: {target_pkg or 'auto'}")

            # Coordination: Should we nuke?
            if self.first_run:
                nuked_count = 0
                self.first_run = False
                logger.info("First test run, skipping module nuke.")
            else:
                nuked_count = self.nuke_strategy.nuke(target_pkg)
                logger.info(f"Hot-reloading: nuked {nuked_count} modules.")

            yield f"[STARTED] nuked {nuked_count} modules\n"

            queue: asyncio.Queue[str] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def output_callback(chunk: str) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, chunk)

            # Execution (Run in executor to avoid blocking event loop)
            future = loop.run_in_executor(
                None,
                self.test_runner.run_tests,
                args,
                target_pkg,
                True,
                output_callback,
            )

            while not future.done() or not queue.empty():
                try:
                    # Wait for a chunk with a small timeout to check future.done()
                    chunk = await asyncio.wait_for(queue.get(), timeout=0.1)
                    yield chunk
                except asyncio.TimeoutError:
                    continue

            exit_code, _, _ = await future

            # Post-run: Nuke tests to ensure fresh state for next collection
            self.nuke_strategy.nuke_tests()

            yield f"\n[DONE] exit_code={exit_code}\n"
        finally:
            self.test_lock.release()

    async def run_tests(
        self,
        args: list[str],
        target_pkg: str | None = None,
    ) -> dict[str, Any]:
        """Execute a test run and return results (non-streaming)."""
        output_acc = []
        nuked_count = 0
        exit_code = 0
        is_busy = False

        async for chunk in self.run_tests_stream(args, target_pkg):
            if chunk == "error:busy":
                is_busy = True
                break

            if chunk.startswith("[STARTED]"):
                match = re.search(r"nuked (\d+) modules", chunk)
                if match:
                    nuked_count = int(match.group(1))
                continue

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
            "nuked_modules_count": nuked_count,
        }
