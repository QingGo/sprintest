import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from sprintest import constants
from sprintest.paths import ensure_sprintest_dir


def setup_logger(name: str = "sprintest", is_daemon: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)

    # Avoid duplicate handlers if already configured
    if logger.handlers:
        return logger

    # Set log level from environment or default to INFO
    level_str = os.environ.get(constants.ENV_LOG_LEVEL, "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    # Formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler for daemon
    if is_daemon:
        try:
            ensure_sprintest_dir()
            log_path = os.path.join(constants.SPRINTEST_DIR, constants.LOG_FILE)
            file_handler = RotatingFileHandler(
                log_path, maxBytes=10 * 1024 * 1024, backupCount=5
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            logger.warning(f"Failed to setup file logging: {e}")

    return logger


# Default logger for general use
logger = setup_logger()
