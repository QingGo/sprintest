import importlib
import io
import os
import re
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout

import pytest
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel


class TestRunRequest(BaseModel):
    args: list[str]
    target_pkg: str | None = None


class TestRunResponse(BaseModel):
    exit_code: int
    output: str
    nuked_modules_count: int


def nuke_modules(target_pkg: str | None) -> int:
    # Identify all modules that were loaded from the current working directory
    # but are not part of the virtual environment.
    root = os.path.abspath(os.getcwd())
    venv_dir = os.path.join(root, ".venv")

    modules_to_delete = []
    for name, mod in list(sys.modules.items()):
        # skip our own package to avoid nuking the executing code
        if name == "sprintest" or name.startswith("sprintest."):
            continue

        # 1. Path-based detection (most reliable for local code)
        file_path = getattr(mod, "__file__", "")
        if file_path:
            abs_file_path = os.path.abspath(file_path)
            if abs_file_path.startswith(root) and not abs_file_path.startswith(venv_dir):
                modules_to_delete.append(name)
                continue

        # 2. Name-based fallback for the target package and tests
        if (
            target_pkg and (name == target_pkg or name.startswith(target_pkg + "."))
        ) or (name == "tests" or name.startswith("tests.")):
            if name not in modules_to_delete:
                modules_to_delete.append(name)

    for name in modules_to_delete:
        if name in sys.modules:
            del sys.modules[name]

    # Invalidate import caches to force re-reading from disk
    importlib.invalidate_caches()
    return len(modules_to_delete)


app = FastAPI()


# Global lock to prevent concurrent pytest execution
test_lock = threading.Lock()


@app.post("/v1/test/run")
def run_test(request: TestRunRequest) -> TestRunResponse:
    # Attempt to acquire the lock without blocking
    if not test_lock.acquire(blocking=False):
        return TestRunResponse(
            exit_code=1,
            output="Error: Daemon is busy. Please try again later.",
            nuked_modules_count=0,
        )

    try:
        try:
            # Priority: Request > SPRINTEST_TARGET_PKG
            target_pkg = request.target_pkg or os.environ.get("SPRINTEST_TARGET_PKG")
            if not target_pkg:
                return TestRunResponse(
                    exit_code=1,
                    output="Error: target_pkg missing. Set SPRINTEST_TARGET_PKG environment variable or provide it in the request.",
                    nuked_modules_count=0,
                )

            nuked_count = nuke_modules(target_pkg)

            # 1. Process pytest arguments
            pytest_args = request.args.copy()

            # Force --color=no
            color_found = False
            for i, arg in enumerate(pytest_args):
                if arg.startswith("--color="):
                    pytest_args[i] = "--color=no"
                    color_found = True
                    break
            if not color_found:
                pytest_args.append("--color=no")

            # Suppress the anyio assert rewrite warning
            pytest_args.extend(["-W", "ignore::pytest.PytestAssertRewriteWarning"])

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()

            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exit_code = pytest.main(pytest_args)

            # 2. Extract and purify output
            raw_output = stdout_buf.getvalue() + stderr_buf.getvalue()

            # Final safety net: Strip ANSI escape codes using regex
            ansi_escape = re.compile(r"\x1b\[[0-9;]*[mK]")
            clean_output = ansi_escape.sub("", raw_output)

            return TestRunResponse(
                exit_code=exit_code,
                output=clean_output,
                nuked_modules_count=nuked_count,
            )
        except Exception as e:
            return TestRunResponse(
                exit_code=1, output=f"Error: {e}", nuked_modules_count=0
            )
    finally:
        # Always release the lock, even if an exception occurs
        test_lock.release()


def run():
    port_str = os.environ.get("SPRINTEST_PORT", "8000")
    port = int(port_str)
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    uvicorn.run(app, host="0.0.0.0", port=port)
