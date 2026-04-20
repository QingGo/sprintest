import glob
import importlib
import io
import os
import re
import shutil
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout

import pytest

from sprintest.logger import logger


def clean_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[mK]")
    return ansi_escape.sub("", text)


class TestRunner:
    __test__ = False

    def __init__(self) -> None:
        self.first_test_run = True
        self.first_test_run_lock = threading.Lock()

    def prepare_pytest_args(
        self, args: list[str], enable_color: bool = False
    ) -> list[str]:
        """Force --color=no (or yes if enable_color=True) and add common warning filters to pytest arguments."""
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

    def nuke_modules(self, target_pkg: str | None) -> int:
        """Identify and remove modules to allow hot-reloading."""
        root = os.path.abspath(os.getcwd())
        venv_dir = os.path.join(root, ".venv")

        modules_to_delete: list[str] = []

        for name, mod in list(sys.modules.items()):
            if name == "sprintest" or name.startswith("sprintest."):
                continue

            file_path = getattr(mod, "__file__", "")
            if file_path:
                abs_file_path = os.path.abspath(file_path)
                if abs_file_path.startswith(root) and not abs_file_path.startswith(
                    venv_dir
                ):
                    modules_to_delete.append(name)
                    continue

            if (
                target_pkg and (name == target_pkg or name.startswith(target_pkg + "."))
            ) or (name == "tests" or name.startswith("tests.")):
                if name not in modules_to_delete:
                    modules_to_delete.append(name)

        for name in list(sys.modules.keys()):
            if name == "sprintest" or name.startswith("sprintest."):
                continue

            if (target_pkg and name.startswith(target_pkg)) or name.startswith("tests"):
                if name not in modules_to_delete:
                    modules_to_delete.append(name)
                    continue

            m = sys.modules.get(name)
            if m is not None:
                file_path = getattr(m, "__file__", "")
                if file_path and ("test_" in file_path or "test_nuke" == name):
                    if name not in modules_to_delete:
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

        if target_pkg:
            pycache_dirs = glob.glob(f"{target_pkg}/__pycache__")
            for d in pycache_dirs:
                shutil.rmtree(d, ignore_errors=True)
            pycache_dirs = glob.glob("tests/__pycache__")
            for d in pycache_dirs:
                shutil.rmtree(d, ignore_errors=True)

        return len(modules_to_delete)

    def run_tests(
        self, args: list[str], target_pkg: str | None, enable_color: bool = False
    ) -> tuple[int, str, int]:
        """Run tests and return (exit_code, output, nuked_count)."""
        with self.first_test_run_lock:
            if self.first_test_run:
                nuked_count = 0
                self.first_test_run = False
                logger.info("First test run, skipping module nuke.")
            else:
                nuked_count = self.nuke_modules(target_pkg)
                logger.info(f"Hot-reloading: nuked {nuked_count} modules.")

        pytest_args = self.prepare_pytest_args(args, enable_color=enable_color)
        logger.debug(f"Running pytest with args: {pytest_args}")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exit_code = pytest.main(pytest_args)
            output = stdout_buf.getvalue() + stderr_buf.getvalue()
        except Exception as e:
            logger.error(f"Error during test execution: {e}")
            output = f"Error: {e}\n"
            exit_code = 1

        # Post-run cleanup to ensure fresh state for next time
        self.nuke_modules(target_pkg)

        return int(exit_code), clean_ansi(output), nuked_count
