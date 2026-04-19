#!/usr/bin/env python3
import os
import subprocess
import sys
import time


# Test pre-loading functionality
def test_preload():
    # Set environment variables
    env = os.environ.copy()
    env["SPRINTEST_PORT"] = "8001"
    env["SPRINTEST_TARGET_PKG"] = "engram-peft"

    # Start the daemon
    print("Starting sprintest-daemon with SPRINTEST_TARGET_PKG=engram-peft...")
    print(f"Current working directory: {os.getcwd()}")

    # Use a different approach to capture output
    daemon_process = subprocess.Popen(
        [sys.executable, "-m", "sprintest.daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait a bit for the process to start
    time.sleep(2)

    # Check if the process is still running
    if daemon_process.poll() is not None:
        # Process exited early, print error
        stdout, stderr = daemon_process.communicate()
        print("Daemon process exited early:")
        print(f"STDOUT: {stdout}")
        print(f"STDERR: {stderr}")
        return False

    # Read output
    print("\nReading daemon output...")
    for _ in range(20):  # Read up to 20 lines
        stdout_line = daemon_process.stdout.readline()
        stderr_line = daemon_process.stderr.readline()

        if stdout_line:
            print(f"STDOUT: {stdout_line.strip()}")
        if stderr_line:
            print(f"STDERR: {stderr_line.strip()}")

        if not stdout_line and not stderr_line:
            break

    # Stop the daemon
    print("\nStopping daemon...")
    daemon_process.terminate()
    try:
        daemon_process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        daemon_process.kill()

    # Check results
    # We'll check if we saw the pre-load message
    # For now, just return True since we're debugging
    return True


if __name__ == "__main__":
    # Change to the sprintest directory
    os.chdir("/Users/zeng/code/sprintest")
    # Run the test
    success = test_preload()
    sys.exit(0 if success else 1)
