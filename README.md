# Sprintest

[English] | [简体中文](README_zh.md)

Sprintest is a Client-Server (C/S) architecture test runner designed for heavy AI projects. It addresses the slow startup times of large models and datasets by keeping them preloaded in memory.

## Core Advantages

- **Preloading**: Keep heavy dependencies (like PyTorch, Transformers, or large datasets) loaded in the daemon process, reducing test startup time from minutes to seconds.
- **Strong Hot-Reloading**: Automatically detects and nukes modified modules in the current directory, ensuring tests run against the latest code without restarting the daemon.
- **Agent Friendly**: Designed with AI coding agents in mind—providing fast feedback loops, purified output (ANSI-free), and stable communication.

## Installation

```bash
# Clone the repository
git clone https://github.com/QingGo/sprintest.git
cd sprintest

# Install in editable mode
pip install -e .
```

## Quick Start

1. **Start the Daemon**:
   In your project root, run:
   ```bash
   sprintest-daemon
   ```

2. **Run Tests**:
   In another terminal, run:
   ```bash
   sprintest tests/your_test_file.py
   ```

## Configuration

You can customize Sprintest using environment variables:

- `SPRINTEST_PORT`: Set the port for the daemon and CLI communication (default: `8000`).
- `SPRINTEST_TARGET_PKG`: Set the name of the package to be hot-reloaded (e.g., your project's main package name). This ensures changes in your source code are detected.

Example:
```bash
export SPRINTEST_PORT=8001
export SPRINTEST_TARGET_PKG=my_project
sprintest-daemon
```

## Regression Testing

To ensure stability, Sprintest includes integration tests. Run them using standard pytest:

```bash
pytest tests/test_sprintest_integration.py
```
