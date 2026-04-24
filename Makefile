# Sprintest Development Makefile
# Note: If the package is already installed in .venv, use the venv's stest directly.
# Use `uv pip install -e .` to install the package in editable mode first.

.PHONY: test format typecheck check clean stop benchmark help dev-reset test-stest-full test-daemon-full

# Default target: show help
help:
	@echo "Sprintest Development Commands:"
	@echo "  make test             - Run tests via stest (warm start if daemon is running)"
	@echo "  make format             - Run ruff linting (src and tests)"
	@echo "  make typecheck        - Run mypy static type analysis"
	@echo "  make check            - Full verification: format -> typecheck -> test"
	@echo "  make stop             - Gracefully stop the daemon via 'stest stop'"
	@echo "  make clean            - Force kill daemon and remove .sprintest directory"
	@echo "  make dev-reset        - Hard reset environment (clean) then run tests"
	@echo "  make benchmark        - Run performance benchmarks"
	@echo ""
	@echo "Full Scenario Tests (from docs/note.md):"
	@echo "  make test-stest-full  - Test stest auto-start, warm-start, and cleanup"
	@echo "  make test-daemon-full - Test manual daemon startup, test submission, and cleanup"

UV_RUN := uv run --no-build --no-sync

test:
	$(UV_RUN) stest tests

format:
	uv run ruff check . --fix

typecheck:
	uv run mypy src

check: format typecheck test

stop:
	$(UV_RUN) stest stop

clean:
	@echo "Cleaning up daemon processes and .sprintest directory..."
	-pkill -f "sprintest.daemon|stest-daemon" 2>/dev/null || true
	rm -rf .sprintest

dev-reset: clean test

# Scenario: # 测试 stest
test-stest-full: clean
	@echo "\n>>> [点 1] 验证正常测试 (Cold Start)..."
	$(UV_RUN) stest tests
	@echo "检查环境状态 [点 1]:"
	@ls .sprintest
	@ps -ef | grep -E "[s]printest.daemon|[s]test-daemon"
	
	@echo "\n>>> [点 2] 验证重复执行 (Warm Start / No multiple daemons)..."
	$(UV_RUN) stest tests
	@echo "检查环境状态 [点 2] (应保持单进程):"
	@ls .sprintest
	@ps -ef | grep -E "[s]printest.daemon|[s]test-daemon"
	
	@echo "\n>>> [点 3] 验证 kill -9 后恢复能力..."
	@echo "执行 pkill -9 模拟强制杀死..."
	-pkill -9 -f sprintest.daemon 2>/dev/null || true
	$(UV_RUN) stest tests
	@echo "检查环境状态 [点 3] (应已恢复并启动新进程):"
	@ls .sprintest
	@ps -ef | grep -E "[s]printest.daemon|[s]test-daemon"
	
	@echo "\n>>> [点 4] 验证 stest stop 与清理..."
	$(UV_RUN) stest stop
	@sleep 2 && echo "等待 2s 以确保 Daemon 优雅退出..."
	@echo "检查进程状态..."
	@pgrep -f "sprintest.daemon|stest-daemon" >/dev/null && echo "[FAIL] Daemon 进程仍存在" || echo "[SUCCESS] 进程已正常退出"
	@echo "检查文件清理..."
	@ls -A .sprintest 2>/dev/null | grep -v "daemon.log" || echo "[SUCCESS] .sprintest 目录已清理 (仅保留日志)"

# Scenario: # 测试 stest-daemon
test-daemon-full: clean
	@echo "\n>>> [点 1] 验证 stest-daemon 手动启动..."
	mkdir -p .sprintest
	uv run --no-build --no-sync stest-daemon > .sprintest/daemon.log 2>&1 & sleep 3
	@echo "检查环境状态 [点 1]:"
	@ls .sprintest
	@ps -ef | grep -E "[s]printest.daemon|[s]test-daemon"
	
	@echo "\n>>> [点 2] 验证 stest tests 提交请求 (应复用已有进程)..."
	$(UV_RUN) stest tests
	@echo "检查环境状态 [点 2] (PID 应与点 1 一致):"
	@ls .sprintest
	@ps -ef | grep -E "[s]printest.daemon|[s]test-daemon"
	
	@echo "\n>>> [点 3] 验证 stest stop 与清理..."
	$(UV_RUN) stest stop
	@sleep 2 && echo "等待 2s 以确保 Daemon 优雅退出..."
	@echo "检查进程状态..."
	@pgrep -f "sprintest.daemon|stest-daemon" >/dev/null && echo "[FAIL] Daemon 进程仍存在" || echo "[SUCCESS] 进程已正常退出"
	@echo "检查文件清理..."
	@ls -A .sprintest 2>/dev/null | grep -v "daemon.log" || echo "[SUCCESS] .sprintest 目录已清理 (仅保留日志)"

benchmark:
	chmod +x scripts/benchmark.sh
	./scripts/benchmark.sh
