import abc
import glob
import importlib
import logging
import os
import re
import shutil
import sys
from typing import Any

try:
    import torch
except ImportError:
    torch = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class BaseNukeStrategy(abc.ABC):
    """Abstract base class for module unloading strategies."""

    @abc.abstractmethod
    def nuke(self, target_pkg: str | None) -> int:
        """Identify and remove modules to allow hot-reloading."""
        pass

    @abc.abstractmethod
    def nuke_tests(self) -> None:
        """Specifically nuke test modules to ensure fresh collection."""
        pass


class NukeStrategy(BaseNukeStrategy):
    """Standard strategy for unloading modules during hot-reload."""

    def __init__(self, ignore_patterns: list[str] | None = None) -> None:
        self.root = os.path.abspath(os.getcwd())
        self.venv_dir = os.path.join(self.root, ".venv")
        self.ignore_patterns = self._get_default_ignore_patterns()
        if ignore_patterns:
            self.ignore_patterns.extend(ignore_patterns)

    def _get_default_ignore_patterns(self) -> list[str]:
        """Return the default ignore patterns."""
        return [
            "sprintest",
            "sprintest.*",
            "__main__",
            "sys",
            "builtins",
        ]

    def should_nuke(self, name: str, mod: Any, target_pkg: str | None) -> bool:
        """Determine if a module should be nuked."""
        # Check ignore patterns
        for pattern in self.ignore_patterns:
            if re.match(pattern.replace(".", "\\.").replace("*", ".*"), name):
                return False

        # Target package or tests should be nuked
        if (
            target_pkg and (name == target_pkg or name.startswith(target_pkg + "."))
        ) or (name == "tests" or name.startswith("tests.")):
            return True

        # Modules within project root (but not venv) should be nuked
        file_path = getattr(mod, "__file__", "")
        if file_path:
            abs_file_path = os.path.abspath(file_path)
            if abs_file_path.startswith(self.root) and not abs_file_path.startswith(
                self.venv_dir
            ):
                return True

        # Test files or modules containing 'test_'
        if file_path and ("test_" in file_path or "test_nuke" == name):
            return True

        return False

    def nuke(self, target_pkg: str | None) -> int:
        """Identify and remove modules to allow hot-reloading."""
        modules_to_delete: list[str] = []

        for name, mod in list(sys.modules.items()):
            if self.should_nuke(name, mod, target_pkg):
                modules_to_delete.append(name)

        modules_to_delete = list(set(modules_to_delete))

        if modules_to_delete:
            logger.debug(
                f"Nuking {len(modules_to_delete)} modules: {', '.join(sorted(modules_to_delete))}"
            )

        for name in modules_to_delete:
            if name in sys.modules:
                del sys.modules[name]

        importlib.invalidate_caches()

        # Cleanup __pycache__
        if target_pkg:
            pycache_dirs = glob.glob(f"{target_pkg}/**/__pycache__", recursive=True)
            for d in pycache_dirs:
                shutil.rmtree(d, ignore_errors=True)

        pycache_dirs = glob.glob("tests/**/__pycache__", recursive=True)
        for d in pycache_dirs:
            shutil.rmtree(d, ignore_errors=True)

        # Best-effort CUDA memory cleanup
        self._cleanup_cuda()

        return len(modules_to_delete)

    def _cleanup_cuda(self) -> None:
        """Release GPU memory held by PyTorch (if CUDA is available)."""
        if torch is None:
            return
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
                logger.debug("CUDA cache emptied after nuke")
        except Exception:  # noqa: BLE001
            logger.warning("CUDA cleanup failed", exc_info=True)

    def nuke_tests(self) -> None:
        """Specifically nuke test modules to ensure fresh collection."""
        self.nuke(None)
