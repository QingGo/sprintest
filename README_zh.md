# Sprintest

[![PyPI version](https://img.shields.io/pypi/v/sprintest.svg)](https://pypi.org/project/sprintest/)

[简体中文] | [English](README.md)

Sprintest 是一个专门为重型 AI 项目设计的 C/S（客户端-服务端）架构测试运行器。它通过将大型模型和数据集保留在内存中，解决了由于加载缓慢导致的测试启动延迟问题。

## 核心优势

- **预加载**：将重量级依赖（如 PyTorch、Transformers 或大型数据集）加载到 daemon 进程中，将测试启动时间从分钟级缩短到秒级。
- **强力热重载**：自动检测并清理当前目录中修改过的模块，确保测试在最新代码上运行，而无需重启 daemon。
- **Agent 友好**：专为 AI 编程助手设计——提供快速的反馈循环、纯净的输出（无 ANSI 字符）以及稳定的通信。

## 安装

```bash
pip install sprintest
```



## 快速开始

1. **启动服务端**：
   在项目根目录下运行：
   ```bash
   sprintest-daemon
   ```

2. **运行测试**：
   在另一个终端运行：
   ```bash
   sprintest tests/your_test_file.py
   ```

## 配置项

您可以通过环境变量自定义 Sprintest：

- `SPRINTEST_PORT`：设置服务端和客户端通信的端口（默认：`8000`）。
- `SPRINTEST_TARGET_PKG`：设置需要热重载的包名（例如您的项目主包名）。这可以确保您的源码变动被正确检测。

示例：
```bash
export SPRINTEST_PORT=8001
export SPRINTEST_TARGET_PKG=my_project
sprintest-daemon
```

## 回归测试

为了确保稳定性，Sprintest 包含集成测试。使用标准的 pytest 运行：

```bash
uv run pytest tests
```
