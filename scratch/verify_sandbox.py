import io
import logging
import sys
from unittest.mock import patch

from sprintest.runner import TestRunner


def test_sandboxing():
    runner = TestRunner()

    # Setup a dummy handler
    root_logger = logging.getLogger()
    initial_handlers = root_logger.handlers[:]
    print(f"Initial handlers: {len(initial_handlers)}")

    # Run tests that we know will add a handler (or simulate it)
    # We can mock pytest.main to simulate hijacking

    def mock_pytest_main(args):
        # Hijack logging
        root_logger.addHandler(logging.StreamHandler())
        # Hijack sys.stdout
        sys.stdout = io.StringIO()
        print("This is hijacked output")
        return 0

    with patch("pytest.main", side_effect=mock_pytest_main):
        exit_code, output, nuked = runner.run_tests(["dummy"], None)
        print(f"Test run output: {output.strip()}")

    print(f"Handlers after sandbox: {len(root_logger.handlers)}")
    assert len(root_logger.handlers) == len(initial_handlers)
    assert sys.stdout is not isinstance(sys.stdout, io.StringIO)  # Should be original
    print("Sandboxing verification PASSED")


if __name__ == "__main__":
    test_sandboxing()
