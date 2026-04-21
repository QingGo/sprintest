# Sprintest

[![PyPI version](https://img.shields.io/pypi/v/sprintest.svg)](https://pypi.org/project/sprintest/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

[English] | [简体中文](README_zh.md)

**Sprintest** is a high-performance Client-Server (C/S) architecture test runner specifically engineered for heavy AI/ML projects. 

In projects involving large language models (LLMs), deep learning frameworks (PyTorch, TensorFlow), or massive datasets, standard test runners suffer from excruciatingly slow startup times (often 30s to several minutes) because they re-initialize the entire environment for every run. **Sprintest solves this by keeping your heavy dependencies pre-loaded in memory.**

---

## 🚀 Key Highlights

- **⚡ Blazing Fast Feedback**: Reduces test startup time from minutes to milliseconds by keeping heavy frameworks and models pre-loaded in a background daemon.
- **🔄 Intelligent Hot-Reloading**: Features a "Nuke Engine" that surgically unloads only your project's modified modules, ensuring you always test against the latest code without losing the pre-loaded state.
- **🔌 Unified Transport Layer**: Automatically chooses between **Unix Domain Sockets (UDS)** for zero-latency local communication and **TCP** for maximum compatibility.
- **🛠️ Decoupled Architecture**: Built with a robust service layer and atomic state management, ensuring stable communication even during heavy test execution.
- **🤖 Agent-Optimized**: Designed for AI coding agents (like Antigravity or Cursor) with clean, ANSI-free output and reliable status tracking.
- **🎯 Configurable Strategy**: Fine-tune hot-reloading with `ignore_patterns` in your `pyproject.toml` to prevent specific heavy modules from being reloaded.

---

## ⚡ Performance Comparison

For AI/ML projects with heavy dependencies (`torch`, `transformers`, etc.), Sprintest provides a massive speedup by eliminating redundant initialization.

| Run Type | Total Time |
| :--- | :--- |
| **Pytest (Standard)** | ~6.0s |
| **Sprintest (First Run)** | ~7.0s |
| **Sprintest (Warm Start)** | **~2.0s** |

*Numbers above are measured on `examples/test_ai_model.py` and are provided for reference only. In heavier real-world projects, speedups typically reach **5x - 10x**—for example, in my other project [engram-peft](https://github.com/QingGo/engram-peft), unit test total wall time improved by **7.8x** (integration tests not included).*

---

## 🏗️ Architecture

Sprintest uses a decoupled architecture to ensure the daemon remains responsive even when running heavy tests.

### System Architecture
```mermaid
graph TD
    Client["Client CLI"] -->|HTTP over UDS/TCP| App["FastAPI App"]
    subgraph "Daemon Process"
        App --> Service["TestService"]
        Service -->|Atomic Lock| Service
        Service --> Nuke["NukeStrategy"]
        Nuke -->|Module Unloading| PySys[sys.modules]
        Service --> Runner["TestRunner"]
        Runner -->|Pytest execution| Tests["User Tests"]
    end
    App -.-> Status["status.json"]
    Client -.-> Status
```

### Test Execution Flow
```mermaid
sequenceDiagram
    participant C as "Client CLI"
    participant S as "status.json"
    participant D as "Daemon (FastAPI)"
    participant N as "NukeStrategy"
    participant R as "TestRunner"

    C->>S: Read status
    alt Daemon not running
        C->>D: Start Daemon (subprocess)
        D->>D: Acquire daemon.lock
        D->>D: Start Uvicorn
        D->>S: Write status (lifespan)
        C->>S: Poll until ready
    end

    C->>D: POST /v1/test/run/stream
    activate D
    D->>D: Acquire test_lock
    D->>N: nuke(target_pkg)
    N-->>D: Modules unloaded
    D->>R: run_tests(args)
    R->>R: Redirect stdout/stderr
    R->>R: pytest.main()
    R-->>D: Exit code & Output
    D->>N: nuke_tests()
    N-->>D: Test modules cleared
    D-->>C: Stream results
    deactivate D
    C->>C: Print output & exit
```

---

## 📦 Installation

```bash
pip install sprintest
```

---

## 📖 Quick Start

1. **Run a test**:
   Simply run `stest` followed by your test file. If the daemon isn't running, it will start automatically.
   ```bash
   stest tests/test_model_loading.py
   ```

2. **Check Daemon status**:
   ```bash
   stest status
   ```

3. **Stop the Daemon**:
   ```bash
   stest stop
   ```

---

## ⚙️ Configuration

### Environment Variables & Isolation
- `SPRINTEST_TARGET_PKG`: The name of the package you are developing. Sprintest will prioritize hot-reloading this package.
- `SPRINTEST_FORCE_TCP`: Set to `1` to bypass Unix Sockets and force TCP communication.
- `SPRINTEST_PORT`: Customize the TCP port (default: `8000`).
- `SPRINTEST_DIR`: Override the default `.sprintest` directory (useful for multi-project isolation or CI environments).
- `SPRINTEST_LOCK_FILE`: Override the daemon lock file path.
- `SPRINTEST_LOG_LEVEL`: Set log level (DEBUG, INFO, WARNING, ERROR).

### Advanced: `pyproject.toml`
You can prevent specific modules from being "nuked" during hot-reload by adding them to the ignore list:

```toml
[tool.sprintest]
ignore = [
    "torch.*",
    "transformers.*",
    "heavy_module_to_keep"
]
```

---

## 🧪 Testing the Runner

### Standard Unit Tests
```bash
uv run pytest tests
```

### Self-Hosting (Bootstrap) Test
Sprintest can reliably run its own test suite through its own daemon to verify infrastructure stability:
```bash
stest tests
```

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
