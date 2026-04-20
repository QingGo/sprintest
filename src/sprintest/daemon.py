import asyncio
import glob
import importlib
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout

import psutil
import pytest
import uvicorn  # type: ignore
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sprintest import __version__
from sprintest.status import (
    ensure_sprintest_dir,
    get_socket_path,
    remove_socket,
    remove_status,
    write_status,
)


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


# Global variables to track state
# test_lock is used to ensure only one test run happens at a time
test_lock = threading.Lock()
first_test_run = True
first_test_run_lock = threading.Lock()


def get_lock_path() -> str:
    return os.path.join(tempfile.gettempdir(), f"sprintest_{os.getpid()}.lock")


# For simplicity in integration tests where we might have one daemon but multiple CLI calls
# we use a fixed lock file based on the port if possible, or just a global one.
DAEMON_LOCK_FILE = os.path.join(tempfile.gettempdir(), "sprintest_global.lock")


def acquire_daemon_lock() -> bool:
    if os.path.exists(DAEMON_LOCK_FILE):
        # Check if the process is still alive
        try:
            with open(DAEMON_LOCK_FILE) as f:
                pid = int(f.read().strip())
            if psutil.pid_exists(pid):
                return False
        except Exception:
            pass
    try:
        with open(DAEMON_LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return False


def release_daemon_lock() -> None:
    try:
        if os.path.exists(DAEMON_LOCK_FILE):
            os.remove(DAEMON_LOCK_FILE)
    except Exception:
        pass


@app.post("/v1/test/run/stream")
async def run_test_stream(request: TestRunRequest) -> StreamingResponse:
    """Stream test execution output in real-time."""
    if not test_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=503, detail="Daemon is busy. Please try again later."
        )

    try:
        loop = asyncio.get_event_loop()

        # Run pytest in executor before returning StreamingResponse
        target_pkg = request.target_pkg or os.environ.get("SPRINTEST_TARGET_PKG")
        if not target_pkg:
            error_msg = "Error: target_pkg missing. Set SPRINTEST_TARGET_PKG environment variable or provide it in the request.\n"
            lines = [
                error_msg,
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

        # Stream the output
        lines = [f"[STARTED] nuked {nuked_count} modules\n"]
        lines.extend(clean_output.splitlines())
        lines.append(f"\n[DONE] exit_code={exit_code}\n")

        return StreamingResponse(
            iter([(line + "\n").encode() for line in lines]),
            media_type="text/plain",
        )
    except HTTPException:
        # Re-raise HTTPException as it's already handled by FastAPI
        raise
    except Exception as e:
        error_msg = f"Error in run_test_stream: {e}\n"
        return StreamingResponse(
            iter([error_msg.encode()]),
            media_type="text/plain",
            status_code=500,
        )
    finally:
        # Release the lock if we own it
        try:
            test_lock.release()
        except RuntimeError:
            # Lock might not be owned by this thread if it was already released
            pass


@app.post("/v1/test/run")
async def run_test(request: TestRunRequest) -> TestRunResponse:
    global first_test_run

    # Attempt to acquire the lock without blocking
    if not test_lock.acquire(blocking=False):
        return TestRunResponse(
            exit_code=1,
            output="Error: Daemon is busy. Please try again later.",
            nuked_modules_count=0,
        )

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

        def execute_pytest() -> int:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                return pytest.main(pytest_args)

        loop = asyncio.get_event_loop()
        exit_code = await loop.run_in_executor(None, execute_pytest)

        # 2. Extract and purify output
        raw_output = stdout_buf.getvalue() + stderr_buf.getvalue()
        clean_output = clean_ansi(raw_output)

        return TestRunResponse(
            exit_code=exit_code,
            output=clean_output,
            nuked_modules_count=nuked_count,
        )
    except Exception as e:
        return TestRunResponse(exit_code=1, output=f"Error: {e}", nuked_modules_count=0)
    finally:
        # Mark test as finished
        try:
            test_lock.release()
        except RuntimeError:
            pass


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
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())

    # Pre-load target package if SPRINTEST_TARGET_PKG is set
    target_pkg = os.environ.get("SPRINTEST_TARGET_PKG")
    if target_pkg:
        # Convert hyphens to underscores for package name (Python convention)
        pkg_name = target_pkg.replace("-", "_")

        # Check if user specified a direct path to the package
        target_pkg_path = os.environ.get("SPRINTEST_TARGET_PKG_PATH")
        if target_pkg_path:
            print(f"[INFO] Using user-specified package path: {target_pkg_path}")
            if target_pkg_path not in sys.path:
                sys.path.insert(0, target_pkg_path)
                print(f"[INFO] Added {target_pkg_path} to sys.path")
            # Try to import the package
            try:
                importlib.import_module(pkg_name)
                print(
                    f"[INFO] Pre-loaded target package: {pkg_name} (from specified path)"
                )
            except ImportError as e:
                print(
                    f"[WARNING] Failed to pre-load target package {pkg_name} from specified path: {e}"
                )
        else:
            # Try to import the package directly
            try:
                importlib.import_module(pkg_name)
                print(
                    f"[INFO] Pre-loaded target package: {pkg_name} (from {target_pkg})"
                )
            except ImportError as e:
                # If import fails, try to find the package directory and add it to sys.path
                print(f"[WARNING] Failed to pre-load target package {pkg_name}: {e}")
                print(
                    f"[INFO] Trying to find and add {target_pkg} directory to sys.path"
                )

                # Look for the package directory in current working directory and parent directories
                current_dir = os.getcwd()
                found = False

                for _ in range(5):  # Limit search to 5 levels up
                    # Try both hyphenated and underscore versions of the directory name
                    for dir_name in [target_pkg, pkg_name]:
                        pkg_dir = os.path.join(current_dir, dir_name)
                        if os.path.isdir(pkg_dir):
                            print(f"[INFO] Found package directory: {pkg_dir}")

                            # Check if this directory has a pyproject.toml or setup.py file
                            pyproject_toml = os.path.join(pkg_dir, "pyproject.toml")
                            setup_py = os.path.join(pkg_dir, "setup.py")

                            # Check if this directory has a src subdirectory
                            src_in_pkg = os.path.join(pkg_dir, "src")
                            if os.path.isdir(src_in_pkg):
                                # If it does, add the src directory to sys.path
                                if src_in_pkg not in sys.path:
                                    sys.path.insert(0, src_in_pkg)
                                    print(f"[INFO] Added {src_in_pkg} to sys.path")
                                # Try to import again
                                try:
                                    importlib.import_module(pkg_name)
                                    print(
                                        f"[INFO] Pre-loaded target package: {pkg_name}"
                                    )
                                    found = True
                                except ImportError as e2:
                                    print(
                                        f"[WARNING] Still failed to pre-load target package {pkg_name}: {e2}"
                                    )
                                break
                            elif os.path.exists(pyproject_toml) or os.path.exists(
                                setup_py
                            ):
                                # If it has pyproject.toml or setup.py, add the directory itself
                                if pkg_dir not in sys.path:
                                    sys.path.insert(0, pkg_dir)
                                    print(f"[INFO] Added {pkg_dir} to sys.path")
                                # Try to import again
                                try:
                                    importlib.import_module(pkg_name)
                                    print(
                                        f"[INFO] Pre-loaded target package: {pkg_name}"
                                    )
                                    found = True
                                except ImportError as e2:
                                    print(
                                        f"[WARNING] Still failed to pre-load target package {pkg_name}: {e2}"
                                    )
                                break
                            else:
                                # Add the package directory itself to sys.path
                                if pkg_dir not in sys.path:
                                    sys.path.insert(0, pkg_dir)
                                    print(f"[INFO] Added {pkg_dir} to sys.path")
                                # Try to import again
                                try:
                                    importlib.import_module(pkg_name)
                                    print(
                                        f"[INFO] Pre-loaded target package: {pkg_name}"
                                    )
                                    found = True
                                except ImportError as e2:
                                    print(
                                        f"[WARNING] Still failed to pre-load target package {pkg_name}: {e2}"
                                    )
                                break

                    # Look for src directory which might contain the package
                    if not found:
                        src_dir = os.path.join(current_dir, "src")
                        if os.path.isdir(src_dir):
                            # Try both hyphenated and underscore versions of the package name in src
                            for src_pkg_name in [target_pkg, pkg_name]:
                                pkg_in_src = os.path.join(src_dir, src_pkg_name)
                                if os.path.isdir(pkg_in_src):
                                    if src_dir not in sys.path:
                                        sys.path.insert(0, src_dir)
                                        print(f"[INFO] Added {src_dir} to sys.path")
                                    # Try to import again
                                    try:
                                        importlib.import_module(pkg_name)
                                        print(
                                            f"[INFO] Pre-loaded target package: {pkg_name}"
                                        )
                                        found = True
                                    except ImportError as e2:
                                        print(
                                            f"[WARNING] Still failed to pre-load target package {pkg_name}: {e2}"
                                        )
                                    break

                    if found:
                        break

                    # Move up one directory
                    parent_dir = os.path.dirname(current_dir)
                    if parent_dir == current_dir:  # Reached root directory
                        break
                    current_dir = parent_dir

                if not found:
                    print(
                        f"[ERROR] Could not find package {target_pkg} in any directory. Consider setting SPRINTEST_TARGET_PKG_PATH."
                    )

    # Try to use Unix socket first
    use_unix = (
        hasattr(socket, "AF_UNIX") and os.environ.get("SPRINTEST_FORCE_TCP") != "1"
    )

    if use_unix:
        try:
            ensure_sprintest_dir()
            socket_path = get_socket_path()
            remove_socket()

            # Create Unix socket server
            server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_socket.bind(socket_path)
            server_socket.listen(1)

            # Write status file
            status = {
                "pid": os.getpid(),
                "socket_path": socket_path,
                "version": __version__,
                "type": "unix",
            }
            write_status(status)

            print(f"[INFO] Sprintest Daemon started with Unix socket: {socket_path}")

            # Handle socket connections
            while True:
                client_socket, client_address = server_socket.accept()
                try:
                    # Receive data from client
                    data = b""
                    while True:
                        chunk = client_socket.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                        if b"\n\n" in data:
                            break

                    if not data:
                        continue

                    # Parse JSON request
                    try:
                        request_data = json.loads(data.decode("utf-8"))
                        command = request_data.get("command")

                        if command == "run_test":
                            # Handle test run request
                            args = request_data.get("args", [])
                            target_pkg = request_data.get(
                                "target_pkg"
                            ) or os.environ.get("SPRINTEST_TARGET_PKG")

                            if not target_pkg:
                                response = {
                                    "exit_code": 1,
                                    "output": "Error: target_pkg missing. Set SPRINTEST_TARGET_PKG environment variable or provide it in the request.",
                                    "nuked_modules_count": 0,
                                }
                            else:
                                # Clean modules before running tests, but skip on first run
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

                                # Run pytest
                                pytest_args = prepare_pytest_args(args)
                                stdout_buf = io.StringIO()
                                stderr_buf = io.StringIO()

                                try:
                                    with (
                                        redirect_stdout(stdout_buf),
                                        redirect_stderr(stderr_buf),
                                    ):
                                        exit_code = pytest.main(pytest_args)
                                    raw_output = (
                                        stdout_buf.getvalue() + stderr_buf.getvalue()
                                    )
                                    clean_output = clean_ansi(raw_output)

                                    # Clean modules after running tests
                                    nuke_modules(target_pkg)

                                    response = {
                                        "exit_code": exit_code,
                                        "output": clean_output,
                                        "nuked_modules_count": nuked_count,
                                    }
                                except Exception as e:
                                    response = {
                                        "exit_code": 1,
                                        "output": f"Error: {e}",
                                        "nuked_modules_count": 0,
                                    }
                        elif command == "status":
                            response = {"status": "running", "version": __version__}
                        elif command == "stop":
                            response = {
                                "message": "Sprintest Daemon is shutting down..."
                            }

                            # Schedule shutdown
                            def shutdown() -> None:
                                time.sleep(0.5)
                                remove_socket()
                                remove_status()
                                os._exit(0)

                            threading.Thread(target=shutdown, daemon=True).start()
                        else:
                            response = {"error": "Unknown command"}
                    except json.JSONDecodeError:
                        response = {"error": "Invalid JSON request"}

                    # Send response
                    response_data = json.dumps(response).encode("utf-8") + b"\n\n"
                    client_socket.sendall(response_data)
                except Exception as e:
                    print(f"[ERROR] Socket handling error: {e}")
                finally:
                    client_socket.close()
        except (AttributeError, OSError) as e:
            print(
                f"[INFO] Unix socket not available or failed: {e}, falling back to TCP"
            )

    # TCP Fallback
    port_str = os.environ.get("SPRINTEST_PORT", "8000")
    port = int(port_str)

    # Write status file for TCP mode
    status = {
        "pid": os.getpid(),
        "port": port,
        "version": __version__,
        "type": "tcp",
    }
    write_status(status)

    print(f"[INFO] Sprintest Daemon started with TCP port: {port}")
    if os.environ.get("SPRINTEST_SKIP_UVICORN") != "1":
        uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    run()
