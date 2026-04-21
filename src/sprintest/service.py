import asyncio
import logging
import threading
from typing import Any

from sprintest.context import DaemonContext
from sprintest.runner import NukeStrategy, TestRunner

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

    async def run_tests(
        self,
        args: list[str],
        target_pkg: str | None = None,
        use_stream: bool = False,
    ) -> dict[str, Any]:
        """Execute a test run with hot-reloading."""
        target_pkg = target_pkg or self.context.target_pkg

        if not self.test_lock.acquire(blocking=False):
            return {
                "exit_code": 1,
                "output": "Another test is already running.",
                "nuked_modules_count": 0,
                "error": "busy",
            }

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

            # Execution (Run in executor to avoid blocking event loop)
            loop = asyncio.get_event_loop()
            exit_code, output, _ = await loop.run_in_executor(
                None, self.test_runner.run_tests, args, target_pkg, True
            )

            # Post-run: Nuke tests to ensure fresh state for next collection
            self.nuke_strategy.nuke_tests()

            return {
                "exit_code": exit_code,
                "output": output,
                "nuked_modules_count": nuked_count,
            }
        finally:
            self.test_lock.release()
