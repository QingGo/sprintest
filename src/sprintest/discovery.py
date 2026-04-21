import ast
import logging
import os
import sys

logger = logging.getLogger(__name__)

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


def find_target_pkg() -> str | None:
    """Automatically discover the target package name from pyproject.toml, setup.py, or directory structure."""
    # 1. Try pyproject.toml
    if os.path.exists("pyproject.toml") and tomllib is not None:
        try:
            with open("pyproject.toml", "rb") as f:
                config = tomllib.load(f)
            if (
                "project" in config
                and "name" in config["project"]
                and isinstance(config["project"]["name"], str)
            ):
                return config["project"]["name"]
        except (OSError, tomllib.TOMLDecodeError) as e:
            logger.debug(f"Failed to load package name from pyproject.toml: {e}")

    # 2. Try setup.py
    if os.path.exists("setup.py"):
        try:
            with open("setup.py") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "setup"
                ):
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "name"
                            and isinstance(keyword.value, ast.Constant)
                            and isinstance(keyword.value.value, str)
                        ):
                            return keyword.value.value
        except (OSError, SyntaxError, ValueError) as e:
            logger.debug(f"Failed to parse setup.py for package name: {e}")

    # 3. Check src directory
    if os.path.exists("src"):
        for item in os.listdir("src"):
            item_path = os.path.join("src", item)
            if os.path.isdir(item_path) and os.path.exists(
                os.path.join(item_path, "__init__.py")
            ):
                return item

    # 4. Check current directory
    for item in os.listdir("."):
        if item in ("src", ".venv", "tests", "build", "dist"):
            continue
        item_path = os.path.join(".", item)
        if os.path.isdir(item_path) and os.path.exists(
            os.path.join(item_path, "__init__.py")
        ):
            return item

    return None


def discover_package_path(target_pkg: str) -> str | None:
    """Find the absolute path of the target package."""
    pkg_name = target_pkg.replace("-", "_")
    current_dir = os.getcwd()

    for _ in range(5):  # Limit search depth
        # Check direct subdirectories
        for dir_name in [target_pkg, pkg_name]:
            pkg_dir = os.path.join(current_dir, dir_name)
            if os.path.isdir(pkg_dir):
                # Check for src structure inside the package
                src_in_pkg = os.path.join(pkg_dir, "src")
                if os.path.isdir(src_in_pkg):
                    return src_in_pkg
                return pkg_dir

        # Check for src directory containing the package
        src_dir = os.path.join(current_dir, "src")
        if os.path.isdir(src_dir):
            for src_pkg_name in [target_pkg, pkg_name]:
                if os.path.isdir(os.path.join(src_dir, src_pkg_name)):
                    return src_dir

        # Move up
        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:
            break
        current_dir = parent_dir

    return None
