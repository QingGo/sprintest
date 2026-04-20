"""
Unit tests for Unix socket setup in the daemon.

These tests verify:
- The socket file is created by bind() before uvicorn starts
- Default socket permissions (no chmod needed) allow the owner to connect
- uvicorn's fd= parameter bypasses the internal os.chmod() that fails on overlayfs
"""

import os
import socket
import tempfile
from collections.abc import Generator

import pytest


@pytest.fixture()
def short_tmp_path() -> Generator[str, None, None]:
    """A temp directory with a short absolute path.

    AF_UNIX socket paths are limited to 104 bytes on macOS (108 on Linux).
    pytest's built-in tmp_path generates paths under /private/var/folders/...
    which easily exceeds this limit. Using /tmp keeps paths short.
    """
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="stest_") as d:
        yield d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bind_socket(socket_path: str) -> socket.socket:
    """Reproduce the exact logic used in daemon.run() for UDS startup."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(socket_path)
    return sock


# ---------------------------------------------------------------------------
# Socket file creation
# ---------------------------------------------------------------------------


def test_unix_socket_bind_creates_file(short_tmp_path: str) -> None:
    """bind() must create the socket file on disk before uvicorn is started.
    The integration fixture polls os.path.exists() to know when the daemon is
    ready — if bind() fails silently, the fixture times out.
    """
    socket_path = os.path.join(short_tmp_path, "daemon.sock")
    assert not os.path.exists(socket_path)

    sock = _bind_socket(socket_path)
    try:
        assert os.path.exists(socket_path), (
            "Socket file was not created by bind(). "
            "The daemon would appear to never start."
        )
        # Verify it's actually a socket, not a regular file
        assert os.stat(socket_path).st_mode & 0o170000 == 0o140000, (
            "Path exists but is not a socket file."
        )
    finally:
        sock.close()
        if os.path.exists(socket_path):
            os.remove(socket_path)


# ---------------------------------------------------------------------------
# chmod failure resilience (the Docker/overlayfs scenario)
# ---------------------------------------------------------------------------


def test_unix_socket_default_permissions_allow_owner_connect(
    short_tmp_path: str,
) -> None:
    """The socket created by bind() without any chmod must be connectable
    by the owner (same user that runs both daemon and CLI).

    We deliberately do NOT call chmod — it is cargo-culted from uvicorn's
    nginx/WSGI model (0o660 to allow a different-group web server to connect)
    and is irrelevant for a same-user local dev tool. This test documents
    that the default permissions are sufficient.
    """
    socket_path = os.path.join(short_tmp_path, "daemon.sock")
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(socket_path)
    server_sock.listen(1)

    # Verify that we (same user = owner) can connect without any chmod
    client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client_sock.connect(socket_path)
    except PermissionError:
        pytest.fail(
            "Could not connect to Unix socket with default permissions. "
            "This would mean the daemon is unreachable without chmod."
        )
    finally:
        client_sock.close()
        server_sock.close()
        if os.path.exists(socket_path):
            os.remove(socket_path)


# ---------------------------------------------------------------------------
# uvicorn.run() parameter contract
# ---------------------------------------------------------------------------


def test_uvicorn_run_accepts_fd_not_sockets() -> None:
    """Regression test: uvicorn.run() accepts 'fd=' but NOT 'sockets='.
    This test will fail immediately if someone accidentally reverts the fix
    to use sockets= (which causes TypeError at runtime).
    """
    import inspect

    import uvicorn

    sig = inspect.signature(uvicorn.run)
    params = set(sig.parameters.keys())

    assert "fd" in params, (
        "uvicorn.run() lost its 'fd' parameter. "
        "The UDS pre-bind workaround relies on this parameter."
    )
    assert "sockets" not in params, (
        "uvicorn.run() now has a 'sockets' parameter. "
        "Consider updating the daemon to use it directly instead of 'fd'."
    )


def test_uvicorn_run_fd_does_not_trigger_chmod(
    short_tmp_path: str,
) -> None:
    """When fd= is used, uvicorn must NOT call os.chmod().
    If it does, the Docker/overlayfs fix would be broken.
    We inspect uvicorn.Config to verify that config.uds is None when
    fd= is provided, which is what prevents the internal chmod call.
    """
    import uvicorn

    socket_path = os.path.join(short_tmp_path, "test.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(socket_path)

    try:
        config = uvicorn.Config(
            "sprintest.daemon:app", fd=sock.fileno(), log_config=None
        )
        # The key assertion: config.uds should be None when fd= is used,
        # which is what prevents uvicorn from calling chmod internally.
        assert config.uds is None, (
            f"uvicorn.Config with fd= set config.uds={config.uds!r}. "
            "This means uvicorn WILL call os.chmod() on startup, "
            "breaking the Docker/overlayfs fix."
        )
    finally:
        sock.close()
        if os.path.exists(socket_path):
            os.remove(socket_path)
