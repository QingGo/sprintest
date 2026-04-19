import asyncio
import glob
import importlib
import io
import os
import re
import shutil
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout

import pytest
import uvicorn  # type: ignore
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sprintest import __version__


class TestRunRequest(BaseModel):
    args: list[str]
    target_pkg: str | None = None


class TestRunResponse(BaseModel):
    exit_code: int
    output: str
    nuked_modules_count: int


def clean_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[mK]")
    return ansi_escape.sub("", text)


def prepare_pytest_args(args: list[str], enable_color: bool = False) -> list[str]:
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


def nuke_modules(target_pkg: str | None) -> int:
    # Identify all modules that were loaded from the current working directory
    # but are not part of the virtual environment.
    root = os.path.abspath(os.getcwd())
    venv_dir = os.path.join(root, ".venv")

    modules_to_delete: list[str] = []

    # First pass: collect all modules to delete
    for name, mod in list(sys.modules.items()):
        # skip our own package to avoid nuking the executing code
        if name == "sprintest" or name.startswith("sprintest."):
            continue

        # 1. Path-based detection (most reliable for local code)
        file_path = getattr(mod, "__file__", "")
        if file_path:
            abs_file_path = os.path.abspath(file_path)
            if abs_file_path.startswith(root) and not abs_file_path.startswith(
                venv_dir
            ):
                modules_to_delete.append(name)
                continue

        # 2. Name-based fallback for the target package and tests
        if (
            target_pkg and (name == target_pkg or name.startswith(target_pkg + "."))
        ) or (name == "tests" or name.startswith("tests.")):
            if name not in modules_to_delete:
                modules_to_delete.append(name)

    # Second pass: force clean any modules related to the target package
    if target_pkg:
        for name in list(sys.modules.keys()):
            # Skip sprintest modules
            if name == "sprintest" or name.startswith("sprintest."):
                continue
            if name.startswith(target_pkg):
                if name not in modules_to_delete:
                    modules_to_delete.append(name)

    # Third pass: force clean tests module and any test-related modules
    for name in list(sys.modules.keys()):
        # Skip sprintest modules
        if name == "sprintest" or name.startswith("sprintest."):
            continue
        if name.startswith("tests"):
            if name not in modules_to_delete:
                modules_to_delete.append(name)

    # Fourth pass: explicitly check for any modules that might be related to test files
    for name in list(sys.modules.keys()):
        # Skip sprintest modules
        if name == "sprintest" or name.startswith("sprintest."):
            continue
        mod = sys.modules[name]
        file_path = getattr(mod, "__file__", "")
        if file_path:
            # Check if this is a test file that might have been imported
            if "test_" in file_path:
                if name not in modules_to_delete:
                    modules_to_delete.append(name)

    # Fifth pass: explicitly check for any modules that might be related to the test file
    # This is to handle the case where pytest imports test files differently
    for name in list(sys.modules.keys()):
        # Skip sprintest modules
        if name == "sprintest" or name.startswith("sprintest."):
            continue
        # Check if this module is related to the target package or tests
        if target_pkg and (name == target_pkg or name.startswith(target_pkg + ".")):
            if name not in modules_to_delete:
                modules_to_delete.append(name)

    # Sixth pass: explicitly check for test_nuke module
    for name in list(sys.modules.keys()):
        # Skip sprintest modules
        if name == "sprintest" or name.startswith("sprintest."):
            continue
        # Check if this is the test_nuke module
        if name == "test_nuke":
            if name not in modules_to_delete:
                modules_to_delete.append(name)

    # Seventh pass: explicitly check for any modules that might be related to the target package
    # This is to handle the case where modules are imported differently
    if target_pkg:
        for name in list(sys.modules.keys()):
            # Skip sprintest modules
            if name == "sprintest" or name.startswith("sprintest."):
                continue
            # Check if this module is in the target package directory
            mod = sys.modules[name]
            file_path = getattr(mod, "__file__", "")
            if file_path:
                abs_file_path = os.path.abspath(file_path)
                # Check if the file is in the current working directory and not in the virtual environment
                if abs_file_path.startswith(root) and not abs_file_path.startswith(
                    venv_dir
                ):
                    # Check if the file is in the target package directory
                    if target_pkg in abs_file_path:
                        if name not in modules_to_delete:
                            modules_to_delete.append(name)

    # Remove duplicates
    modules_to_delete = list(set(modules_to_delete))

    # Delete modules
    for name in modules_to_delete:
        if name in sys.modules:
            del sys.modules[name]

    # Invalidate caches to ensure modules are reloaded
    importlib.invalidate_caches()

    # Force reload of any modules that might be cached
    if target_pkg:
        # Clear any __pycache__ directories in the target package
        pycache_dirs = glob.glob(f"{target_pkg}/__pycache__")
        for dir in pycache_dirs:
            shutil.rmtree(dir, ignore_errors=True)
        # Clear any __pycache__ directories in the tests directory
        pycache_dirs = glob.glob("tests/__pycache__")
        for dir in pycache_dirs:
            shutil.rmtree(dir, ignore_errors=True)

    return len(modules_to_delete)


app = FastAPI()


# Global lock to prevent concurrent pytest execution
test_lock = threading.Semaphore(1)

# Global variable to track if a test is running
is_test_running = False

# Lock for protecting access to is_test_running
is_test_running_lock = threading.Lock()

# Flag to track if this is the first test run (to preserve pre-loaded modules)
first_test_run = True

# Lock for protecting access to first_test_run
first_test_run_lock = threading.Lock()


@app.post("/v1/test/run/stream")
async def run_test_stream(request: TestRunRequest) -> StreamingResponse:
    """Stream test execution output in real-time."""
    global is_test_running

    # Try to acquire the semaphore without blocking using run_in_executor
    loop = asyncio.get_event_loop()
    acquired = await loop.run_in_executor(
        None, lambda: test_lock.acquire(blocking=False)
    )
    await asyncio.sleep(0)  # Yield control to event loop

    if not acquired:
        error_msg = "Error: Daemon is busy. Please try again later.\n"
        return StreamingResponse(
            iter([error_msg]),
            media_type="text/plain",
            status_code=503,
        )

    # Mark test as running
    with is_test_running_lock:
        is_test_running = True

    # Run pytest in executor before returning StreamingResponse
    target_pkg = request.target_pkg or os.environ.get("SPRINTEST_TARGET_PKG")
    if not target_pkg:
        error_msg = "Error: target_pkg missing. Set SPRINTEST_TARGET_PKG environment variable or provide it in the request.\n"
        test_lock.release()
        lines = [
            "Error: target_pkg missing. Set SPRINTEST_TARGET_PKG environment variable or provide it in the request.\n",
            "[DONE] exit_code=1\n",
        ]
        return StreamingResponse(
            iter([(line).encode() for line in lines]),
            media_type="text/plain",
        )

    # Clean modules before running tests, but skip on first run to preserve pre-loaded modules
    global first_test_run
    with first_test_run_lock:
        if first_test_run:
            nuked_count = 0
            print(
                "[INFO] Skipping module cleanup on first run to preserve pre-loaded modules"
            )
            first_test_run = False
        else:
            nuked_count = nuke_modules(target_pkg)

    pytest_args = prepare_pytest_args(request.args, enable_color=True)

    # Run pytest in executor to actually hold the lock during test execution
    def run_pytest() -> tuple[str, int]:
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exit_code = pytest.main(pytest_args)
            return stdout_buf.getvalue() + stderr_buf.getvalue(), exit_code
        except Exception as e:
            return f"Error: {e}\n", 1

    output, exit_code = await loop.run_in_executor(None, run_pytest)
    clean_output = clean_ansi(output)

    # Clean modules after running tests
    nuke_modules(target_pkg)

    # Mark test as finished
    with is_test_running_lock:
        is_test_running = False
    print(f"[DEBUG] Released lock at {time.time():.3f}", flush=True)
    test_lock.release()

    # Stream the output
    lines = [f"[STARTED] nuked {nuked_count} modules\n"]
    lines.extend(clean_output.splitlines())
    lines.append(f"\n[DONE] exit_code={exit_code}\n")

    return StreamingResponse(
        iter([(line + "\n").encode() for line in lines]),
        media_type="text/plain",
    )


@app.post("/v1/test/run")
def run_test(request: TestRunRequest) -> TestRunResponse:
    global is_test_running

    # Attempt to acquire the lock without blocking
    if not test_lock.acquire(blocking=False):
        return TestRunResponse(
            exit_code=1,
            output="Error: Daemon is busy. Please try again later.",
            nuked_modules_count=0,
        )

    # Check if a test is already running
    if is_test_running:
        test_lock.release()
        return TestRunResponse(
            exit_code=1,
            output="Error: Daemon is busy. Please try again later.",
            nuked_modules_count=0,
        )

    # Mark test as running
    is_test_running = True

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

            # Clean modules before running tests, but skip on first run to preserve pre-loaded modules
            global first_test_run
            with first_test_run_lock:
                if first_test_run:
                    nuked_count = 0
                    print(
                        "[INFO] Skipping module cleanup on first run to preserve pre-loaded modules"
                    )
                    first_test_run = False
                else:
                    nuked_count = nuke_modules(target_pkg)

            # 1. Process pytest arguments
            pytest_args = prepare_pytest_args(request.args)

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()

            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exit_code = pytest.main(pytest_args)

            # 2. Extract and purify output
            raw_output = stdout_buf.getvalue() + stderr_buf.getvalue()
            clean_output = clean_ansi(raw_output)

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
        # Mark test as finished
        is_test_running = False
        # Always release the lock, even if an exception occurs
        test_lock.release()


@app.get("/v1/status")
def status() -> dict[str, str]:

    return {"status": "running", "version": __version__}


@app.post("/v1/stop")
def stop() -> dict[str, str]:
    def shutdown() -> None:
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=shutdown, daemon=True).start()
    return {"message": "Sprintest Daemon is shutting down..."}


def run() -> None:
    port_str = os.environ.get("SPRINTEST_PORT", "8000")
    port = int(port_str)
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())

    # Pre-load target package if SPRINTEST_TARGET_PKG is set
    target_pkg = os.environ.get("SPRINTEST_TARGET_PKG")
    if target_pkg:
        try:
            importlib.import_module(target_pkg)
            print(f"[INFO] Pre-loaded target package: {target_pkg}")
        except ImportError as e:
            print(f"[WARNING] Failed to pre-load target package {target_pkg}: {e}")

    uvicorn.run(app, host="0.0.0.0", port=port)
