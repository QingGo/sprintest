import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from sprintest import constants
from sprintest.paths import ensure_sprintest_dir


def setup_logger(name: str = "sprintest", is_daemon: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)

    # Set log level from environment or default to INFO
    level_str = os.environ.get(constants.ENV_LOG_LEVEL, "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)
    logger.setLevel(level)

    # Prevent propagation to avoid duplicate output when root logger has handlers
    logger.propagate = False

    # Formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler - only add if no handlers exist
    if not logger.handlers:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    # File handler for daemon - add if requested and not already present
    if is_daemon:
        has_file_handler = any(
            isinstance(h, RotatingFileHandler) for h in logger.handlers
        )
        if not has_file_handler:
            try:
                ensure_sprintest_dir()
                log_path = os.path.join(constants.SPRINTEST_DIR, constants.LOG_FILE)
                file_handler = RotatingFileHandler(
                    log_path, maxBytes=10 * 1024 * 1024, backupCount=5
                )
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except OSError as e:
                logger.warning(f"Failed to setup file logging: {e}")

    return logger
