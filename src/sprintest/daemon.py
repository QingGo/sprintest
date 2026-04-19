import asyncio
import importlib
import io
import os
import re
import sys
import threading
import time
from collections.abc import AsyncGenerator
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

    for name in modules_to_delete:
        if name in sys.modules:
            del sys.modules[name]

    if modules_to_delete:
        importlib.invalidate_caches()
    return len(modules_to_delete)


app = FastAPI()


# Global lock to prevent concurrent pytest execution
test_lock = threading.Lock()


@app.post("/v1/test/run/stream")
async def run_test_stream(request: TestRunRequest) -> StreamingResponse:
    """Stream test execution output in real-time."""
    if not test_lock.acquire(blocking=False):
        error_msg = "Error: Daemon is busy. Please try again later.\n"
        return StreamingResponse(
            iter([error_msg]),
            media_type="text/plain",
            status_code=503,
        )

    async def generate() -> AsyncGenerator[bytes, None]:
        try:
            target_pkg = request.target_pkg or os.environ.get("SPRINTEST_TARGET_PKG")
            if not target_pkg:
                yield b"Error: target_pkg missing. Set SPRINTEST_TARGET_PKG environment variable or provide it in the request.\n"
                yield b"[DONE]\n"
                return

            # Clean modules before starting subprocess
            nuked_count = nuke_modules(target_pkg)
            yield f"[STARTED] nuked {nuked_count} modules\n".encode()

            pytest_args = prepare_pytest_args(request.args, enable_color=True)

            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pytest",
                *pytest_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )

            stdout = process.stdout
            if stdout is not None:
                while True:
                    line = await stdout.readline()
                    if not line:
                        break
                    decoded_line = line.decode("utf-8", errors="replace")
                    clean_line = clean_ansi(decoded_line)
                    yield clean_line.encode()

            exit_code = await process.wait()
            yield f"\n[DONE] exit_code={exit_code}\n".encode()

        except Exception as e:
            yield f"Error: {e}\n".encode()
        finally:
            test_lock.release()

    return StreamingResponse(content=generate(), media_type="text/plain")


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
    uvicorn.run(app, host="0.0.0.0", port=port)
