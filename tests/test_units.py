from sprintest.runner import TestRunner, clean_ansi

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
