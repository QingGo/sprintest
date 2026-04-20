import os
from typing import Any

from sprintest.discovery import discover_package_path, find_target_pkg


def test_find_target_pkg_from_pyproject(tmp_path: Any, monkeypatch: Any) -> None:
    """测试从 pyproject.toml 发现目标包"""
    pyproject_content = """
[project]
name = "test_project"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    monkeypatch.chdir(tmp_path)
    result = find_target_pkg()
    assert result == "test_project"


def test_find_target_pkg_from_setup_py(tmp_path: Any, monkeypatch: Any) -> None:
    """测试从 setup.py 发现目标包"""
    setup_content = """
from setuptools import setup

setup(
    name="test_project",
)
"""
    (tmp_path / "setup.py").write_text(setup_content)
    monkeypatch.chdir(tmp_path)
    result = find_target_pkg()
    assert result == "test_project"


def test_find_target_pkg_from_src(tmp_path: Any, monkeypatch: Any) -> None:
    """测试从 src 目录发现目标包"""
    pkg_dir = tmp_path / "src" / "test_project"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("")

    monkeypatch.chdir(tmp_path)
    result = find_target_pkg()
    assert result == "test_project"


def test_discover_package_path_simple(tmp_path: Any, monkeypatch: Any) -> None:
    """测试发现包路径逻辑"""
    pkg_dir = tmp_path / "my_package"
    pkg_dir.mkdir()

    monkeypatch.chdir(tmp_path)
    # 应该能在当前目录下找到 my_package
    path = discover_package_path("my_package")
    assert path is not None
    assert os.path.basename(path) == "my_package"


def test_discover_package_path_src_layout(tmp_path: Any, monkeypatch: Any) -> None:
    """测试在 src 布局下发现包路径"""
    pkg_dir = tmp_path / "src" / "my_package"
    pkg_dir.mkdir(parents=True)

    monkeypatch.chdir(tmp_path)
    path = discover_package_path("my_package")
    assert path is not None
    assert os.path.basename(path) == "src"


def test_discover_package_path_nested_src(tmp_path: Any, monkeypatch: Any) -> None:
    """测试在包内部包含 src 的情况 (某些奇怪的布局)"""
    src_dir = tmp_path / "my-repo" / "src"
    src_dir.mkdir(parents=True)

    monkeypatch.chdir(tmp_path)
    path = discover_package_path("my-repo")
    assert path is not None
    assert os.path.basename(path) == "src"
