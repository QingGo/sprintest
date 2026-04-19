import os
import subprocess
import time


def test_nuke_engine_hot_reload() -> None:
    """
    Integration test to verify that the Nuke Engine correctly handles
    hot-reloading even across multiple file reverts.
    """
    project_root = os.getcwd()
    test_pkg = "nuke_test_pkg"
    test_dir = "tests"
    test_file = f"{test_dir}/test_nuke.py"
    module_file = f"{test_pkg}/module.py"

    # Setup: Create package and test file
    os.makedirs(test_pkg, exist_ok=True)
    os.makedirs(test_dir, exist_ok=True)
    with open(f"{test_pkg}/__init__.py", "w") as f:
        f.write("")

    def set_version(v: int) -> None:
        with open(module_file, "w") as f:
            f.write(f"VERSION = {v}\n")

    def set_test(v: int) -> None:
        with open(test_file, "w") as f:
            f.write(f"from {test_pkg}.module import VERSION\n")
            f.write(f"def test_v(): assert VERSION == {v}\n")

    # Start the daemon on a specific port for testing
    port = 8001
    env = os.environ.copy()
    env["SPRINTEST_PORT"] = str(port)
    env["PYTHONPATH"] = os.path.join(project_root, "src")
    # Use python to run the daemon entry point
    daemon_proc = subprocess.Popen(
        [".venv/bin/python", "-c", "from sprintest.daemon import run; run()"],
        env=env,
        cwd=project_root,
    )

    # Set target_pkg in env for the CLI runs below
    env["SPRINTEST_TARGET_PKG"] = test_pkg

    try:
        # Wait for daemon to start
        time.sleep(3)

        # Step 1: VERSION = 1, Test assert 1 -> Should Pass
        set_version(1)
        set_test(1)
        res = subprocess.run(
            [
                ".venv/bin/python",
                "-c",
                "from sprintest.cli import main; main()",
                test_file,
            ],
            env=env,
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, (
            f"Initial run should pass. Output: {res.stdout}\nError: {res.stderr}"
        )

        # Step 2: VERSION = 2, Test assert 1 -> Should Fail with assert 2 == 1
        set_version(2)
        res = subprocess.run(
            [
                ".venv/bin/python",
                "-c",
                "from sprintest.cli import main; main()",
                test_file,
            ],
            env=env,
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        assert res.returncode != 0, (
            f"Run after change to 2 should fail. Output: {res.stdout}"
        )

        # Step 3: VERSION = 1, Test assert 1 -> Should Pass again (The Revert Scenario)
        set_version(1)
        res = subprocess.run(
            [
                ".venv/bin/python",
                "-c",
                "from sprintest.cli import main; main()",
                test_file,
            ],
            env=env,
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, (
            f"Run after revert to 1 should pass. Output: {res.stdout}\nError: {res.stderr}"
        )

    finally:
        daemon_proc.terminate()
        daemon_proc.wait()
        # Cleanup
        if os.path.exists(test_file):
            os.remove(test_file)
        if os.path.exists(module_file):
            os.remove(module_file)
        if os.path.exists(f"{test_pkg}/__init__.py"):
            os.remove(f"{test_pkg}/__init__.py")


if __name__ == "__main__":
    test_nuke_engine_hot_reload()
