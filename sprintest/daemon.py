from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn
import sys
import io
from contextlib import redirect_stdout, redirect_stderr
import pytest

class TestRunRequest(BaseModel):
    args: list[str]
    target_pkg: str

class TestRunResponse(BaseModel):
    exit_code: int
    output: str
    nuked_modules_count: int

app = FastAPI()

@app.post("/v1/test/run")
async def run_test(request: TestRunRequest) -> TestRunResponse:
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        exit_code = pytest.main(request.args)
    
    output = stdout_buf.getvalue() + stderr_buf.getvalue()
    
    return TestRunResponse(
        exit_code=exit_code,
        output=output,
        nuked_modules_count=0
    )

def run():
    uvicorn.run(app, host="0.0.0.0", port=8000)
