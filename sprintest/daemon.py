from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn
import sys
import io
import os
from contextlib import redirect_stdout, redirect_stderr
import pytest

class TestRunRequest(BaseModel):
    args: list[str]
    target_pkg: str

class TestRunResponse(BaseModel):
    exit_code: int
    output: str
    nuked_modules_count: int

import importlib

def nuke_modules(target_pkg: str) -> int:
    # Identify all modules that were loaded from the current working directory
    # but are not part of the virtual environment.
    root = os.getcwd()
    venv_dir = os.path.join(root, ".venv")
    
    modules_to_delete = []
    for name, mod in list(sys.modules.items()):
        # 1. Path-based detection (most reliable for local code)
        file_path = getattr(mod, "__file__", "")
        if file_path and file_path.startswith(root) and not file_path.startswith(venv_dir):
            modules_to_delete.append(name)
        # 2. Name-based fallback for the target package and tests
        elif (target_pkg and (name == target_pkg or name.startswith(target_pkg + "."))) or \
             (name == "tests" or name.startswith("tests.")):
            if name not in modules_to_delete:
                modules_to_delete.append(name)

    for name in modules_to_delete:
        if name in sys.modules:
            del sys.modules[name]
    
    # Invalidate import caches to force re-reading from disk
    importlib.invalidate_caches()
    return len(modules_to_delete)

app = FastAPI()

@app.post("/v1/test/run")
async def run_test(request: TestRunRequest) -> TestRunResponse:
    nuked_count = nuke_modules(request.target_pkg)
    
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        exit_code = pytest.main(request.args)
    
    output = stdout_buf.getvalue() + stderr_buf.getvalue()
    
    return TestRunResponse(
        exit_code=exit_code,
        output=output,
        nuked_modules_count=nuked_count
    )

def run():
    port = int(os.environ.get("STEST_PORT", "8000"))
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    uvicorn.run(app, host="0.0.0.0", port=port)
