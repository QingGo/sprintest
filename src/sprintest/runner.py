import io
import logging
import re
import sys
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout

import pytest

logger = logging.getLogger(__name__)


def clean_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[mK]")
    return ansi_escape.sub("", text)


class TestRunner:
    """Core engine for running pytest within the daemon process."""

    __test__ = False

    def prepare_pytest_args(
        self, args: list[str], enable_color: bool = False
    ) -> list[str]:
        """Force color and add common warning filters."""
        pytest_args: list[str] = args.copy()

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

    @contextmanager
    def sandbox_pytest(self) -> Generator[None, None, None]:
        """Protect the daemon process from pytest's environment mutations."""
        # Snapshot standard streams
        orig_stdout = sys.stdout
        orig_stderr = sys.stderr

        # Snapshot logging handlers
        # We take a shallow copy of the list so we can restore the exact references
        root_logger = logging.getLogger()
        orig_handlers = root_logger.handlers[:]

        try:
            yield
        finally:
            # Restore standard streams
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr

            # Restore logging handlers
            # Remove any handlers added by pytest or the tests
            for handler in root_logger.handlers[:]:
                if handler not in orig_handlers:
                    root_logger.removeHandler(handler)

            # Re-add any original handlers that might have been removed
            for handler in orig_handlers:
                if handler not in root_logger.handlers:
                    root_logger.addHandler(handler)

    def run_tests(
        self, args: list[str], target_pkg: str | None, enable_color: bool = False
    ) -> tuple[int, str, int]:
        """Run tests and return (exit_code, output, nuked_count)."""
        pytest_args = self.prepare_pytest_args(args, enable_color=enable_color)
        logger.debug(f"Running pytest with args: {pytest_args}")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        try:
            with (
                self.sandbox_pytest(),
                redirect_stdout(stdout_buf),
                redirect_stderr(stderr_buf),
            ):
                exit_code = pytest.main(pytest_args)
            output = stdout_buf.getvalue() + stderr_buf.getvalue()
        except (OSError, RuntimeError) as e:
            logger.error(f"Error during test execution: {e}")
            output = f"Error: {e}\n"
            exit_code = 1

        return int(exit_code), clean_ansi(output), 0
