import json
import os
from typing import Any

from sprintest import constants


def get_sprintest_dir() -> str:
    """获取 sprintest 目录路径"""
    return os.path.abspath(constants.SPRINTEST_DIR)


def get_socket_path() -> str:
    """获取 Unix socket 文件路径"""
    sprintest_dir = get_sprintest_dir()
    return os.path.join(sprintest_dir, constants.SOCKET_NAME)


def get_status_path() -> str:
    """获取状态文件路径"""
    return os.path.join(get_sprintest_dir(), constants.STATUS_FILE)


def ensure_sprintest_dir() -> None:
    """确保 .sprintest 目录存在"""
    sprintest_dir = get_sprintest_dir()
    os.makedirs(sprintest_dir, exist_ok=True)


def write_status(status: dict[str, Any]) -> None:
    """写入状态文件"""
    ensure_sprintest_dir()
    with open(get_status_path(), "w") as f:
        json.dump(status, f, indent=2)


def read_status() -> dict[str, Any] | None:
    """读取状态文件"""
    path = get_status_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def remove_status() -> None:
    """删除状态文件"""
    path = get_status_path()
    if os.path.exists(path):
        os.remove(path)


def remove_socket() -> None:
    """删除 Unix socket 文件"""
    socket_path = get_socket_path()
    if os.path.exists(socket_path):
        os.remove(socket_path)
