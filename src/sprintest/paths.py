import os

from sprintest import constants


def get_sprintest_dir() -> str:
    """Get the sprintest directory path."""
    return os.environ.get("SPRINTEST_DIR", os.path.abspath(constants.SPRINTEST_DIR))


def ensure_sprintest_dir() -> str:
    """Ensure the sprintest directory exists and return its path."""
    sprintest_dir = get_sprintest_dir()
    os.makedirs(sprintest_dir, exist_ok=True)
    return sprintest_dir


def get_socket_path() -> str:
    """Get the Unix socket file path.

    On Windows, ``os.path.join`` produces backslash separators which
    do not work with ``socket.AF_UNIX`` (POSIX convention requires
    forward slashes).  We normalise the path so that it always uses
    forward slashes.
    """
    path = os.path.join(get_sprintest_dir(), constants.SOCKET_NAME)
    return path.replace("\\", "/")


def get_status_path() -> str:
    """Get the daemon status file path."""
    return os.path.join(get_sprintest_dir(), constants.STATUS_FILE)


def get_lock_path() -> str:
    """Get the daemon lock file path."""
    return os.environ.get(
        "SPRINTEST_LOCK_FILE", os.path.join(get_sprintest_dir(), "daemon.lock")
    )
