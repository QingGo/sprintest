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
