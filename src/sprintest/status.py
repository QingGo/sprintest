import json
import os
from typing import Any

import psutil

from sprintest.logger import setup_logger
from sprintest.paths import (
    ensure_sprintest_dir,
    get_lock_path,
    get_socket_path,
    get_status_path,
)

logger = setup_logger("sprintest.status")


def write_status(data: dict[str, Any]) -> str:
    """写入状态文件"""
    ensure_sprintest_dir()
    path = get_status_path()
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
        logger.debug(f"Created resource at absolute path: {os.path.abspath(path)}")
        return path
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def read_status() -> dict[str, Any] | None:
    """读取状态文件，并验证进程是否存活"""
    path = get_status_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return None

            # 验证进程存活
            pid = data.get("pid")
            if pid and not psutil.pid_exists(pid):
                logger.warning(f"Removing stale status file (PID {pid} is not running)")
                remove_status(path)
                return None

            return data
    except (OSError, json.JSONDecodeError):
        return None


def read_lock_pid() -> int | None:
    """Return the PID from the daemon lock file if the process is alive.

    Returns:
        The living PID, or None if the lock file is absent, stale, or unreadable.
        A non-None return means a daemon process is running or still starting up.
    """
    lock_path = get_lock_path()
    if not os.path.exists(lock_path):
        return None
    try:
        with open(lock_path) as f:
            content = f.read().strip()
        if not content:
            logger.debug(f"Lock file {lock_path} exists but is empty")
            return None

        pid = int(content)
        if psutil.pid_exists(pid):
            return pid
        # Stale lock — process is gone
        logger.debug(f"Lock file {lock_path} is stale (PID {pid} not running)")
        return None
    except (OSError, ValueError) as e:
        logger.debug(f"Failed to read lock file {lock_path}: {e}")
        return None


def remove_status(path: str | None = None) -> None:
    """删除状态文件"""
    path = path or get_status_path()
    abs_path = os.path.abspath(path)
    if os.path.exists(path):
        try:
            os.remove(path)
            logger.debug(f"Removing resource at absolute path: {abs_path}")
        except OSError as e:
            logger.error(f"Failed to remove status file at {abs_path}: {e}")


def remove_socket(path: str | None = None) -> None:
    """删除 Unix socket 文件"""
    path = path or get_socket_path()
    abs_path = os.path.abspath(path)
    if os.path.exists(path):
        try:
            os.remove(path)
            logger.debug(f"Removing resource at absolute path: {abs_path}")
        except OSError as e:
            logger.error(f"Failed to remove socket file at {abs_path}: {e}")
