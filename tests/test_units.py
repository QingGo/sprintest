import errno
from unittest.mock import patch

from sprintest.daemon import check_unix_socket_support
from sprintest.runner import TestRunner, clean_ansi
from sprintest.state import DaemonState

runner = TestRunner()


def test_clean_ansi() -> None:
    assert clean_ansi("\x1b[32mPASS\x1b[0m") == "PASS"
    assert clean_ansi("Hello \x1b[1;31mWorld\x1b[0m") == "Hello World"
    assert clean_ansi("No colors") == "No colors"
    assert clean_ansi("\x1b[KClear line") == "Clear line"


def test_prepare_pytest_args_no_color() -> None:
    args = ["tests/test_foo.py", "-v"]
    prepared = runner.prepare_pytest_args(args)
    assert "--color=no" in prepared
    assert "tests/test_foo.py" in prepared
    assert "-v" in prepared
    assert "-W" in prepared
    assert "ignore::pytest.PytestAssertRewriteWarning" in prepared


def test_prepare_pytest_args_override_color() -> None:
    args = ["--color=yes", "-k", "test_math"]
    prepared = runner.prepare_pytest_args(args)
    assert "--color=no" in prepared
    assert "--color=yes" not in prepared
    assert "-k" in prepared
    assert "test_math" in prepared


def test_prepare_pytest_args_preserves_others() -> None:
    args = ["-x", "--ff", "tests/"]
    prepared = runner.prepare_pytest_args(args)
    assert "-x" in prepared
    assert "--ff" in prepared
    assert "tests/" in prepared
    assert "--color=no" in prepared


# ---------------------------------------------------------------------------
# Regression tests: Unix socket detection & transport_mode
# ---------------------------------------------------------------------------


def test_check_unix_socket_support_eopnotsupp() -> None:
    """Returns False when bind() raises EOPNOTSUPP (cross-platform)."""
    with patch("sprintest.daemon.socket") as mock_socket:
        mock_socket.AF_UNIX = 1
        mock_socket.SOCK_STREAM = 1
        mock_instance = mock_socket.socket.return_value.__enter__.return_value
        mock_instance.bind.side_effect = OSError(errno.EOPNOTSUPP, "Operation not supported")

        assert check_unix_socket_support("/nonexistent/test.sock") is False


def test_check_unix_socket_support_other_oserror() -> None:
    """Returns True on non-EOPNOTSUPP errors (optimistic fallback)."""
    with patch("sprintest.daemon.socket") as mock_socket:
        mock_socket.AF_UNIX = 1
        mock_socket.SOCK_STREAM = 1
        mock_instance = mock_socket.socket.return_value.__enter__.return_value
        mock_instance.bind.side_effect = OSError(errno.EACCES, "Permission denied")

        assert check_unix_socket_support("/nonexistent/test.sock") is True


def test_check_unix_socket_support_success() -> None:
    """Returns True when bind() succeeds."""
    with patch("sprintest.daemon.socket") as mock_socket:
        mock_socket.AF_UNIX = 1
        mock_socket.SOCK_STREAM = 1

        assert check_unix_socket_support("/nonexistent/test.sock") is True


def test_check_unix_socket_support_no_af_unix() -> None:
    """Returns False when platform lacks AF_UNIX support."""
    import builtins
    with patch.object(builtins, "hasattr", return_value=False):
        assert check_unix_socket_support("/nonexistent/test.sock") is False


def test_daemon_state_transport_mode_default() -> None:
    """DaemonState.transport_mode defaults to 'tcp'."""
    state = DaemonState()
    assert state.transport_mode == "tcp"


def test_daemon_state_transport_mode_mutable() -> None:
    """DaemonState.transport_mode can be switched at runtime."""
    state = DaemonState()
    assert state.transport_mode == "tcp"

    state.transport_mode = "unix"
    assert state.transport_mode == "unix"

    state.transport_mode = "tcp"
    assert state.transport_mode == "tcp"
