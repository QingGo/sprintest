import os
import shutil
import sys
import tempfile

from sprintest.discovery import discover_package_path, find_target_pkg


def test_find_target_pkg_from_pyproject() -> None:
    """测试从 pyproject.toml 发现目标包"""
    temp_dir = tempfile.mkdtemp()
    try:
        pyproject_content = """
[project]
name = "test_project"
"""
        with open(os.path.join(temp_dir, "pyproject.toml"), "w") as f:
            f.write(pyproject_content)

        original_dir = os.getcwd()
        os.chdir(temp_dir)
        try:
            result = find_target_pkg()
            assert result == "test_project"
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)


def test_find_target_pkg_from_setup_py() -> None:
    """测试从 setup.py 发现目标包"""
    temp_dir = tempfile.mkdtemp()
    try:
        setup_content = """
from setuptools import setup

setup(
    name="test_project",
)
"""
        with open(os.path.join(temp_dir, "setup.py"), "w") as f:
            f.write(setup_content)

        original_dir = os.getcwd()
        os.chdir(temp_dir)
        try:
            result = find_target_pkg()
            assert result == "test_project"
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)


def test_find_target_pkg_from_src() -> None:
    """测试从 src 目录发现目标包"""
    temp_dir = tempfile.mkdtemp()
    try:
        src_dir = os.path.join(temp_dir, "src")
        pkg_dir = os.path.join(src_dir, "test_project")
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, "__init__.py"), "w") as f:
            f.write("")

        original_dir = os.getcwd()
        os.chdir(temp_dir)
        try:
            result = find_target_pkg()
            assert result == "test_project"
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)


def test_discover_package_path_simple() -> None:
    """测试发现包路径逻辑"""
    temp_dir = tempfile.mkdtemp()
    try:
        pkg_dir = os.path.join(temp_dir, "my_package")
        os.makedirs(pkg_dir)

        original_dir = os.getcwd()
        os.chdir(temp_dir)
        try:
            # 应该能在当前目录下找到 my_package
            path = discover_package_path("my_package")
            assert path is not None
            assert os.path.basename(path) == "my_package"
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)


def test_discover_package_path_src_layout() -> None:
    """测试在 src 布局下发现包路径"""
    temp_dir = tempfile.mkdtemp()
    try:
        src_dir = os.path.join(temp_dir, "src")
        pkg_dir = os.path.join(src_dir, "my_package")
        os.makedirs(pkg_dir)

        original_dir = os.getcwd()
        os.chdir(temp_dir)
        try:
            path = discover_package_path("my_package")
            assert path is not None
            assert os.path.basename(path) == "src"
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)


def test_discover_package_path_nested_src() -> None:
    """测试在包内部包含 src 的情况 (某些奇怪的布局)"""
    temp_dir = tempfile.mkdtemp()
    try:
        pkg_root = os.path.join(temp_dir, "my-repo")
        src_dir = os.path.join(pkg_root, "src")
        os.makedirs(src_dir)

        original_dir = os.getcwd()
        os.chdir(temp_dir)
        try:
            path = discover_package_path("my-repo")
            assert path is not None
            assert os.path.basename(path) == "src"
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)
