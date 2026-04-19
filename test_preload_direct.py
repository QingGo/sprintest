#!/usr/bin/env python3
import importlib
import os
import sys

# Add the current directory to sys.path
sys.path.insert(0, os.getcwd())


# Test the preload logic directly
def test_preload_logic():
    print("Testing preload logic directly...")

    # Test with engram-peft
    target_pkg = "engram-peft"
    pkg_name = target_pkg.replace("-", "_")

    print(f"Target package: {target_pkg}")
    print(f"Package name (converted): {pkg_name}")

    # Try to import directly
    try:
        importlib.import_module(pkg_name)
        print(f"✅ Successfully imported {pkg_name} directly")
        return True
    except ImportError as e:
        print(f"❌ Failed to import {pkg_name} directly: {e}")

    # Try to find the package directory
    print("\nTrying to find the package directory...")
    current_dir = os.getcwd()
    found = False

    for _ in range(5):  # Limit search to 5 levels up
        print(f"Checking directory: {current_dir}")

        # Try both hyphenated and underscore versions
        for dir_name in [target_pkg, pkg_name]:
            pkg_dir = os.path.join(current_dir, dir_name)
            if os.path.isdir(pkg_dir):
                print(f"Found package directory: {pkg_dir}")
                # Check if this directory has a src subdirectory
                src_in_pkg = os.path.join(pkg_dir, "src")
                if os.path.isdir(src_in_pkg):
                    # If it does, add the src directory to sys.path
                    print(f"Found src directory in package: {src_in_pkg}")
                    if src_in_pkg not in sys.path:
                        sys.path.insert(0, src_in_pkg)
                        print(f"Added {src_in_pkg} to sys.path")
                else:
                    # If it doesn't, add the package directory itself to sys.path
                    if pkg_dir not in sys.path:
                        sys.path.insert(0, pkg_dir)
                        print(f"Added {pkg_dir} to sys.path")
                found = True
                break

        # Look for src directory
        if not found:
            src_dir = os.path.join(current_dir, "src")
            if os.path.isdir(src_dir):
                print(f"Found src directory: {src_dir}")
                # Try both hyphenated and underscore versions in src
                for src_pkg_name in [target_pkg, pkg_name]:
                    pkg_in_src = os.path.join(src_dir, src_pkg_name)
                    if os.path.isdir(pkg_in_src):
                        print(f"Found package in src: {pkg_in_src}")
                        if src_dir not in sys.path:
                            sys.path.insert(0, src_dir)
                            print(f"Added {src_dir} to sys.path")
                        found = True
                        break

        if found:
            break

        # Move up one directory
        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:  # Reached root
            break
        current_dir = parent_dir

    # Try to import again
    if found:
        try:
            importlib.import_module(pkg_name)
            print(f"✅ Successfully imported {pkg_name} after adding path")
            return True
        except ImportError as e:
            print(f"❌ Still failed to import {pkg_name}: {e}")
    else:
        print("❌ Failed to find package directory")

    return False


if __name__ == "__main__":
    # Change to the sprintest directory
    os.chdir("/Users/zeng/code/sprintest")
    # Run the test
    success = test_preload_logic()
    sys.exit(0 if success else 1)
