import threading
from dataclasses import dataclass, field


@dataclass
class DaemonState:
    """Mutable runtime state of the Sprintest Daemon."""

    shutdown_event: threading.Event = field(default_factory=threading.Event)
    internal_lock: threading.Lock = field(default_factory=threading.Lock)

    # Any other dynamic flags can be added here
    is_busy: bool = False
    active_connections: int = 0

    # Actual transport mode at runtime — set by daemon.run() after deciding use_unix.
    # This can change from "unix" to "tcp" at bind-failure fallback, unlike the
    # frozen DaemonContext.socket_path which is determined up front.
    transport_mode: str = "tcp"
