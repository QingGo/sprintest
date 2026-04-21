import glob
import importlib
import io
import logging
import os
import re
import shutil
import sys
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import Any

import pytest

logger = logging.getLogger(__name__)


def clean_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[mK]")
    return ansi_escape.sub("", text)


class NukeStrategy:
    """Strategy for unloading modules during hot-reload."""

    def __init__(self, ignore_patterns: list[str] | None = None) -> None:
        self.root = os.path.abspath(os.getcwd())
        self.venv_dir = os.path.join(self.root, ".venv")
        self.ignore_patterns = self._get_default_ignore_patterns()
        if ignore_patterns:
            self.ignore_patterns.extend(ignore_patterns)

    def _get_default_ignore_patterns(self) -> list[str]:
        """Return the default ignore patterns."""
        return [
            "sprintest",
            "sprintest.*",
            "__main__",
            "sys",
            "builtins",
        ]

    def should_nuke(self, name: str, mod: Any, target_pkg: str | None) -> bool:
        """Determine if a module should be nuked."""
        # Check ignore patterns
        for pattern in self.ignore_patterns:
            if re.match(pattern.replace(".", "\\.").replace("*", ".*"), name):
                return False

        # Target package or tests should be nuked
        if (
            target_pkg and (name == target_pkg or name.startswith(target_pkg + "."))
        ) or (name == "tests" or name.startswith("tests.")):
            return True

        # Modules within project root (but not venv) should be nuked
        file_path = getattr(mod, "__file__", "")
        if file_path:
            abs_file_path = os.path.abspath(file_path)
            if abs_file_path.startswith(self.root) and not abs_file_path.startswith(
                self.venv_dir
            ):
                return True

        # Test files or modules containing 'test_'
        if file_path and ("test_" in file_path or "test_nuke" == name):
            return True

        return False

    def nuke(self, target_pkg: str | None) -> int:
        """Identify and remove modules to allow hot-reloading."""
        modules_to_delete: list[str] = []

        for name, mod in list(sys.modules.items()):
            if self.should_nuke(name, mod, target_pkg):
                modules_to_delete.append(name)

        modules_to_delete = list(set(modules_to_delete))

        if modules_to_delete:
            logger.debug(
                f"Nuking {len(modules_to_delete)} modules: {', '.join(sorted(modules_to_delete))}"
            )

        for name in modules_to_delete:
            if name in sys.modules:
                del sys.modules[name]

        importlib.invalidate_caches()

        # Cleanup __pycache__
        if target_pkg:
            pycache_dirs = glob.glob(f"{target_pkg}/**/__pycache__", recursive=True)
            for d in pycache_dirs:
                shutil.rmtree(d, ignore_errors=True)

        pycache_dirs = glob.glob("tests/**/__pycache__", recursive=True)
        for d in pycache_dirs:
            shutil.rmtree(d, ignore_errors=True)

        return len(modules_to_delete)

    def nuke_tests(self) -> None:
        """Specifically nuke test modules to ensure fresh collection."""
        self.nuke(None)


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
        except Exception as e:
            logger.error(f"Error during test execution: {e}")
            output = f"Error: {e}\n"
            exit_code = 1

        return int(exit_code), clean_ansi(output), 0
