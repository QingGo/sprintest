# Sprintest

[![PyPI version](https://img.shields.io/pypi/v/sprintest.svg)](https://pypi.org/project/sprintest/)

[English] | [简体中文](README_zh.md)

Sprintest is a Client-Server (C/S) architecture test runner designed for heavy AI projects. It addresses the slow startup times of large models and datasets by keeping them preloaded in memory.

## Core Advantages

- **Intelligent Preloading**: Keep heavy dependencies (like PyTorch, Transformers, or large datasets) loaded in the daemon process, with support for automatic path search or manual path specification, reducing test startup time from minutes to seconds.
- **Strong Hot-Reloading**: Automatically detects and nukes modified modules in the current directory, ensuring tests run against the latest code without restarting the daemon.
- **Flexible Configuration**: Support for customizing port, target package name, and package path through environment variables, adapting to different project structures and naming conventions.
- **Agent Friendly**: Designed with AI coding agents in mind—providing fast feedback loops, purified output (ANSI-free), and stable communication.

## Installation

```bash
pip install sprintest
```

## Quick Start

1. **Run Tests**:
   In your project root, simply run:
   ```bash
   sprintest tests/your_test_file.py
   ```
   
   The system will automatically detect and start the Sprintest Daemon if it's not already running.

2. **Manual Daemon Management** (optional):
   - Check status: `sprintest status`
   - Stop daemon: `sprintest stop`
   - Start manually: `sprintest-daemon`

## Configuration

You can customize Sprintest using environment variables:
- `SPRINTEST_TARGET_PKG`: Set the name of the package to be hot-reloaded (e.g., your project's main package name). This ensures changes in your source code are detected.
- `SPRINTEST_TARGET_PKG_PATH`: Set the specific path to the target package (optional). Use this option to directly specify the path when automatic search cannot find the package.
- `SPRINTEST_PORT`: TCP port used when Unix socket is not available (default: `8000`).

Example:
```bash
export SPRINTEST_PORT=8001
export SPRINTEST_TARGET_PKG=my_project
sprintest-daemon
```

Path specification example:
```bash
export SPRINTEST_TARGET_PKG=engram-peft
export SPRINTEST_TARGET_PKG_PATH=/path/to/engram-peft/src
sprintest-daemon
```

## Regression Testing

To ensure stability, Sprintest includes integration tests. Run them using standard pytest:

```bash
uv run pytest tests
```
