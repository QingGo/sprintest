# sprintest AI 助手开发指南 (AGENTS.md)

## 1. 项目背景与角色定位
- **项目定位**：本项目 (`sprintest`) 是一个专为重型 AI 项目设计的 C/S 架构测试运行器。它通过在后台守护进程 (Daemon) 中预加载大模型、库（如 PyTorch, Transformers）等重型依赖，消除测试运行的冷启动开销，支持秒级的热重载 (Hot-reload) 测试体验。
- **技术栈**：Python, FastAPI, Uvicorn, Pytest, HTTPX, Psutil, Ruff, Mypy。
- **你的角色**：你是一位精通 Python 后端架构、系统底层机制及测试基础设施的资深研发工程师。在理解需求、修改代码时，请严格遵守本指南的所有规范。

---

## 2. 核心工作流：Think Before You Code
在进行任何实质性的代码修改前，你**必须**在回复中先输出你的思考过程：
1. **分析**：简述你对当前问题的理解，涉及 Daemon 还是 Client，是否涉及模块卸载逻辑。
2. **规划**：列出你打算修改的文件清单和逻辑步骤。规划完成后，**第一步先更新短期记忆**，再执行代码修改。
3. **求证与确认机制**：如果你的规划涉及修改超过 3 个文件，或者更改核心通信协议、锁机制（见第12节核心资产清单），你**必须主动停止**，先询问人类是否同意，得到明确确认后再开始编写代码。


---

## 4. 目录结构与包管理
- **包管理**：本项目严格由 `uv` 进行环境和包管理。执行任何 Python 相关命令时，请优先使用 `uv run`。可以通过 `source .venv/bin/activate` 激活虚拟环境。
- **代码组织**：
  - 核心源代码：`src/sprintest/`
  - 运行状态目录：`.sprintest/` (包含 `daemon.lock`, `daemon.sock`, `status.json`, `daemon.log`)
  - 测试代码：`tests/`
  - 脚本工具：`scripts/`
  - 示例代码：`examples/`

---

## 5. 测试与验证规范 (Testing Constraints)

**5.1 验证手段**
- **集成测试**：使用 `stest tests` 运行完整测试套件，验证 C/S 协同是否正常。
- **静态检测**：修改后必须运行 `ruff check` 和 `mypy` 确保代码质量。

**5.2 强制验证命令**
完成代码修改后，你必须主动运行以下命令验证：
```bash
# 运行单元与集成测试
uv run stest tests
# 运行静态检测
uv run ruff check src tests && uv run mypy src
```

**5.3 环境清理与回归验证**
- **强制清理**：测试用例运行结束后，**必须**清理所有临时目录（如 `.sprintest_test`）、锁文件、Socket 文件及衍生的子进程。
- **Bug 修复标准流程**：
  1. 先写一个**失败的测试用例**复现 Bug；
  2. 再修改代码使测试通过；
  3. 运行完整测试套件确保无回归。

---

## 6. 代码风格与质量规约 (Coding Standards)

- **错误处理原则 (CRITICAL)**：**绝对禁止**使用 `except xxx: pass` 静默错误。如果必须捕获并忽略某个异常，也**至少应该打日志** (logger.debug 或 logger.warning)，并说明忽略的原因。
- **配置与环境隔离 (Architecture)**：
  - 对于 Daemon 进程，所有从环境变量读取的配置必须尽早注入到 `src/sprintest/context.py` 中的 `DaemonContext`（应为不可变 dataclass）。
  - **严禁**在初始化之后的业务逻辑中再次通过 `os.environ.get` 读取配置，以防测试运行时的环境变量污染。
  - 运行时可变状态（如 `is_busy`, `shutdown_event`）必须集中在 `src/sprintest/state.py` 的 `DaemonState` 中管理。
  - **死锁预防**：在操作 `DaemonState` 中的锁或 Event 时，必须遵循统一的加锁顺序，严禁在持有锁的情况下执行可能阻塞的 I/O 或等待操作。
- **强制类型提示 (Type Hints)**：所有新函数和类的方法必须包含完整的 Python 类型提示。务必确保通过 `mypy` 检查。
- **文档注释 (Docstrings)**：使用 Google 风格的 Docstring 描述类和复杂函数。
- **精确修改模式**：修改长文件时，严禁输出整个文件。必须使用精确的搜索/替换块，严禁在块内使用 `// ... existing code ...`。

---

## 7. 文档同步规范
以下情况必须同步更新文档：
1. 修改了任何 `stest` 命令行参数或行为 → 更新 `README.md` 和 `README_zh.md`
2. 变更了 Daemon 的状态文件结构 (`status.json`) 或锁机制 → 更新内部技术文档 (如 `docs/note.md`)

---

## 8. 调试与排错工作流
- **日志优先**：Daemon 的运行日志位于 `.sprintest/daemon.log`。定位问题时，请优先查看该文件。
- **清理机制**：如果 Daemon 启动失败，应检查 `.sprintest/` 下是否有残留的 `daemon.lock` 或 `daemon.sock`。

---

## 9. Git 提交规范
采用 Conventional Commits 规范：
- `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `perf:`, `chore:`。

---

## 10. 结构化状态与记忆管理 (Structured Memory)
维护根目录 `.agent_memory.md`，记录：
- **短期记忆**：当前会话任务、即时踩坑、未闭环问题。
- **长期记忆**：核心架构决策（如 Nuke 策略的选择）、人类最终决策、通用踩坑。

---

## 11. 死循环熔断机制 (Circuit Breaker)
连续 **3 次** 修复同一个 Bug 失败后，立即停止编码，输出《排错总结》，并更新记忆文件，等待人类指令。

---

## 12. 核心资产与破坏性变更管理

**🛑 核心资产清单（修改前必须人类确认）**
1. `src/sprintest/daemon.py` 中的锁竞争与 Socket 绑定逻辑。
2. `src/sprintest/runner.py` 中的 `pytest` 调用与 `NukeStrategy` 集成。
3. `src/sprintest/client.py` 中的请求重试与超时控制。
4. `pyproject.toml` 中的核心依赖版本。

---

## 13. 文件读取与 API 溯源策略
- **渐进式读取**：避免一次性读取超过 500 行的长文件，优先使用 `grep` 或 `head`/`tail` 定位。
- **溯源**：在涉及 `uvicorn`, `fastapi`, `pytest` 的底层 API 时，若不确定行为，应查阅本地 `site-packages` 中的源码或官方文档。

---

## 14. 人机协同执行规范
如果需要执行涉及系统信号（如 `SIGKILL`）、复杂网络配置或需要长时间观察性能的基准测试，请使用标准模板请求人类协助。

---

> **🛑 绝对红线 (CRITICAL RULES)**
> 1. **严禁静默异常**：任何 `try-except` 块必须有日志输出或合理的重抛逻辑。
> 2. **环境隔离与清理**：Daemon 配置必须通过 Context 隔离，严禁直接读污染的环境变量；测试产生的临时目录 (如 `.sprintest_test`) 必须在测试结束后自动清理。
> 3. **稳定性优先**：Daemon 是本项目的核心，任何可能导致 Daemon 挂起、死锁或产生僵尸进程的修改都必须经过极其严谨的论证。