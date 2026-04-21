from dataclasses import dataclass, field


@dataclass(frozen=True)
class DaemonContext:
    lock_path: str
    socket_path: str | None
    status_path: str
    cwd: str
    port: int | None
    target_pkg: str | None
    target_pkg_path: str | None
    version: str
    skip_uvicorn: bool = False
    ignore_patterns: list[str] = field(default_factory=list)
    log_level: str = "INFO"
