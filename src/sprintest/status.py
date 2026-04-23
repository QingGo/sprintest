import json
import logging
import os
from typing import Any

import psutil

from sprintest.paths import (
    ensure_sprintest_dir,
    get_lock_path,
    get_socket_path,
    get_status_path,
)

logger = logging.getLogger(__name__)


def is_daemon_alive(pid: int) -> bool:
    """Verify if the process with given PID is actually a running sprintest daemon.

    Returns ``False`` if the process is a zombie (defunct), even though its PID
    may still appear in the process table.
    """
    if not psutil.pid_exists(pid):
        return False
    try:
        p = psutil.Process(pid)
        # Zombie processes are dead — their entry hasn't been reaped yet but
        # they have no file descriptors, no memory, and will never respond.
        if p.status() == psutil.STATUS_ZOMBIE:
            logger.debug(
                f"PID {pid} is a zombie (defunct) process, treating as dead."
            )
            return False
        # Check if the command line contains our known markers.
        # This prevents collision with recycled PIDs or unrelated processes.
        cmdline = p.cmdline()
        cmdline_str = " ".join(cmdline)
        res = "sprintest.daemon" in cmdline_str or "stest-daemon" in cmdline_str
        if not res:
            logger.debug(f"PID {pid} is NOT a sprintest daemon. cmdline: {cmdline}")
        return res
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logger.debug(
            f"Cannot inspect PID {pid} (NoSuchProcess/AccessDenied), "
            f"assuming it might still be alive: {e}"
        )
        return True


def write_status(data: dict[str, Any], path: str | None = None) -> str:
    """写入状态文件"""
    ensure_sprintest_dir()
    path = path or get_status_path()
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
            if pid and not is_daemon_alive(pid):
                logger.warning(f"Removing stale status file (PID {pid} is not a sprintest daemon)")
                remove_status(path)
                return None

            return data
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f"Failed to read or parse status file at {path}: {e}")
        return None


def read_lock_pid() -> int | None:
    """Return the PID from the daemon lock file if the process is alive.

    If the lock file exists but the PID inside it is dead or a zombie, the
    stale lock file is removed immediately to give subsequent daemon startups
    a clean slate.

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
            try:
                os.remove(lock_path)
                logger.debug(f"Removed empty lock file at {lock_path}")
            except OSError as e:
                logger.warning(f"Failed to remove empty lock file at {lock_path}: {e}")
            return None

        pid = int(content)
        if is_daemon_alive(pid):
            return pid
        # Stale lock — process is dead, zombie, or not ours. Eagerly remove it.
        logger.debug(
            f"Lock file {lock_path} is stale (PID {pid} is not a sprintest daemon), removing it"
        )
        try:
            os.remove(lock_path)
            logger.debug(f"Removed stale lock file at {lock_path}")
        except OSError as e:
            logger.warning(f"Failed to remove stale lock file at {lock_path}: {e}")
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
