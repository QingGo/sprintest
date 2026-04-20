# Sprintest

[![PyPI version](https://img.shields.io/pypi/v/sprintest.svg)](https://pypi.org/project/sprintest/)

[简体中文] | [English](README.md)

Sprintest 是一个专门为重型 AI 项目设计的 C/S（客户端-服务端）架构测试运行器。它通过将大型模型和数据集保留在内存中，解决了由于加载缓慢导致的测试启动延迟问题。

## 核心优势

- **智能预加载**：将重量级依赖（如 PyTorch、Transformers 或大型数据集）加载到 daemon 进程中，支持自动搜索包路径或手动指定路径，将测试启动时间从分钟级缩短到秒级。
- **强力热重载**：自动检测并清理当前目录中修改过的模块，确保测试在最新代码上运行，而无需重启 daemon。
- **灵活配置**：支持通过环境变量自定义端口、目标包名和包路径，适应不同项目结构和命名约定。
- **Agent 友好**：专为 AI 编程助手设计——提供快速的反馈循环、纯净的输出（无 ANSI 字符）以及稳定的通信。

## 安装

```bash
pip install sprintest
```



## 快速开始

1. **运行测试**：
   在项目根目录下直接运行：
   ```bash
   sprintest tests/your_test_file.py
   ```
   
   系统会自动检测并启动 Sprintest Daemon（如果尚未运行）。

2. **手动管理 Daemon**（可选）：
   - 查看状态：`sprintest status`
   - 停止 Daemon：`sprintest stop`
   - 手动启动：`sprintest-daemon`

## 配置项

您可以通过环境变量自定义 Sprintest：

- `SPRINTEST_TARGET_PKG`：设置需要热重载的包名（例如您的项目主包名）。这可以确保您的源码变动被正确检测。
- `SPRINTEST_TARGET_PKG_PATH`：设置目标包的具体路径（可选）。当自动搜索无法找到包时，可以使用此选项直接指定路径。
- `SPRINTEST_PORT`：当 Unix socket 不可用时的 TCP 端口（默认：`8000`）。

示例：
```bash
export SPRINTEST_PORT=8001
export SPRINTEST_TARGET_PKG=my_project
sprintest-daemon
```

指定路径的示例：
```bash
export SPRINTEST_TARGET_PKG=engram-peft
export SPRINTEST_TARGET_PKG_PATH=/path/to/engram-peft/src
sprintest-daemon
```

## 回归测试

为了确保稳定性，Sprintest 包含集成测试。使用标准的 pytest 运行：

```bash
uv run pytest tests
```
