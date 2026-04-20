import os
import subprocess
import sys
from typing import Any

import pytest


def run_cli_main(
    args: list[str], env: dict[str, str], cwd: str
) -> subprocess.CompletedProcess:
    """Helper to run CLI using current sys.executable."""
    return subprocess.run(
        [sys.executable, "-c", "from sprintest.cli import main; main()"] + args,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def daemon_config(tmp_path: Any) -> dict[str, Any]:
    return {
        "type": "tcp",
        "extra_env": {"SPRINTEST_TARGET_PKG": "nuke_test_pkg"},
        "cwd": str(tmp_path),
    }


def test_nuke_engine_hot_reload(daemon_service: dict[str, Any], tmp_path: Any) -> None:
    """
    Integration test to verify that the Nuke Engine correctly handles
    hot-reloading even across multiple file reverts.
    """
    # Setup: Create package and test file in the tmp_path (which is the daemon's CWD)
    test_pkg = tmp_path / "nuke_test_pkg"
    test_pkg.mkdir()
    (test_pkg / "__init__.py").write_text("")

    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    test_file = test_dir / "test_nuke.py"
    module_file = test_pkg / "module.py"

    def set_version(v: int) -> None:
        module_file.write_text(f"VERSION = {v}\n")

    def set_test(v: int) -> None:
        test_file.write_text(
            f"from nuke_test_pkg.module import VERSION\ndef test_v(): assert VERSION == {v}\n"
        )

    env = daemon_service["env"]

    try:
        # Step 1: VERSION = 1, Test assert 1 -> Should Pass
        set_version(1)
        set_test(1)
        res = run_cli_main([str(test_file)], env=env, cwd=str(tmp_path))
        assert res.returncode == 0, (
            f"Initial run should pass. Output: {res.stdout}\nError: {res.stderr}"
        )

        # Step 2: VERSION = 2, Test assert 1 -> Should Fail
        set_version(2)
        res = run_cli_main([str(test_file)], env=env, cwd=str(tmp_path))
        assert res.returncode != 0, (
            f"Run after change to 2 should fail. Output: {res.stdout}"
        )

        # Step 3: VERSION = 1, Test assert 1 -> Should Pass again
        set_version(1)
        res = run_cli_main([str(test_file)], env=env, cwd=str(tmp_path))
        assert res.returncode == 0, (
            f"Run after revert to 1 should pass. Output: {res.stdout}\nError: {res.stderr}"
        )

    finally:
        pass
