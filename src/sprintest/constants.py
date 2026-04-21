# Versioning
from sprintest import __version__

VERSION = __version__

# Network Defaults
DEFAULT_PORT = 8000
DEFAULT_HOST = "127.0.0.1"

# Environment Variables
ENV_PORT = "SPRINTEST_PORT"
ENV_TARGET_PKG = "SPRINTEST_TARGET_PKG"
ENV_TARGET_PKG_PATH = "SPRINTEST_TARGET_PKG_PATH"
ENV_FORCE_TCP = "SPRINTEST_FORCE_TCP"
ENV_SKIP_UVICORN = "SPRINTEST_SKIP_UVICORN"

# File Names
STATUS_FILE = "status.json"
SPRINTEST_DIR = ".sprintest"
SOCKET_NAME = "daemon.sock"
LOG_FILE = "daemon.log"

# Environment Variables (continued)
ENV_LOG_LEVEL = "SPRINTEST_LOG_LEVEL"

# Timeout and Retries
DAEMON_START_RETRIES = 60
DAEMON_START_WAIT = 1
SOCKET_TIMEOUT = 5
HTTP_TIMEOUT = 5
