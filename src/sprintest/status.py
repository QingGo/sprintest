import json
import os
from typing import Any

import psutil

from sprintest.logger import logger
from sprintest.paths import ensure_sprintest_dir, get_socket_path, get_status_path


def write_status(data: dict[str, Any]) -> None:
    """写入状态文件"""
    ensure_sprintest_dir()
    path = get_status_path()
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
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
                remove_status()
                return None

            return data
    except (OSError, json.JSONDecodeError):
        return None


def remove_status() -> None:
    """删除状态文件"""
    path = get_status_path()
    if os.path.exists(path):
        os.remove(path)


def remove_socket() -> None:
    """删除 Unix socket 文件"""
    path = get_socket_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
