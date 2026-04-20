import os
import shutil
import sys
import tempfile

from sprintest.cli import find_target_pkg


def test_find_target_pkg_from_pyproject() -> None:
    """测试从 pyproject.toml 发现目标包"""
    # 创建临时目录
    temp_dir = tempfile.mkdtemp()
    try:
        # 创建 pyproject.toml
        pyproject_content = """
[project]
name = "test_project"
"""
        with open(os.path.join(temp_dir, "pyproject.toml"), "w") as f:
            f.write(pyproject_content)

        # 切换到临时目录
        original_dir = os.getcwd()
        os.chdir(temp_dir)

        try:
            # 测试发现功能
            result = find_target_pkg()
            assert result == "test_project"
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)


def test_find_target_pkg_from_setup_py() -> None:
    """测试从 setup.py 发现目标包"""
    # 创建临时目录
    temp_dir = tempfile.mkdtemp()
    try:
        # 创建 setup.py
        setup_content = """
from setuptools import setup

setup(
    name="test_project",
)
"""
        with open(os.path.join(temp_dir, "setup.py"), "w") as f:
            f.write(setup_content)

        # 切换到临时目录
        original_dir = os.getcwd()
        os.chdir(temp_dir)

        try:
            # 测试发现功能
            result = find_target_pkg()
            assert result == "test_project"
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)


def test_find_target_pkg_from_src() -> None:
    """测试从 src 目录发现目标包"""
    # 创建临时目录
    temp_dir = tempfile.mkdtemp()
    try:
        # 创建 src 目录和包
        src_dir = os.path.join(temp_dir, "src")
        pkg_dir = os.path.join(src_dir, "test_project")
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, "__init__.py"), "w") as f:
            f.write("")

        # 切换到临时目录
        original_dir = os.getcwd()
        os.chdir(temp_dir)

        try:
            # 测试发现功能
            result = find_target_pkg()
            assert result == "test_project"
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)


def test_find_target_pkg_from_current_dir() -> None:
    """测试从当前目录发现目标包"""
    # 创建临时目录
    temp_dir = tempfile.mkdtemp()
    try:
        # 创建包目录
        pkg_dir = os.path.join(temp_dir, "test_project")
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, "__init__.py"), "w") as f:
            f.write("")

        # 切换到临时目录
        original_dir = os.getcwd()
        os.chdir(temp_dir)

        try:
            # 测试发现功能
            result = find_target_pkg()
            assert result == "test_project"
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)


def test_find_target_pkg_not_found() -> None:
    """测试未找到目标包的情况"""
    # 创建临时目录
    temp_dir = tempfile.mkdtemp()
    try:
        # 切换到临时目录
        original_dir = os.getcwd()
        os.chdir(temp_dir)

        try:
            # 测试发现功能
            result = find_target_pkg()
            assert result is None
        finally:
            os.chdir(original_dir)
    finally:
        shutil.rmtree(temp_dir)
