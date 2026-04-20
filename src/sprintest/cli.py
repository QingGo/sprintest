import ast
import json
import os
import re
import socket
import subprocess
import sys
import time

import requests  # type: ignore

# 条件导入tomli
try:
    import tomli
except ImportError:
    if sys.version_info >= (3, 11):
        try:
            import tomllib as tomli  # type: ignore
        except ImportError:
            tomli = None  # type: ignore
    else:
        tomli = None  # type: ignore
from sprintest import __version__
from sprintest.status import read_status

PORT = os.environ.get("SPRINTEST_PORT", "8000")
DAEMON_URL = f"http://localhost:{PORT}/v1/test/run"
STREAM_URL = f"http://localhost:{PORT}/v1/test/run/stream"


def send_socket_request(socket_path: str, request_data: dict) -> dict:
    """发送 Unix socket 请求"""
    client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client_socket.connect(socket_path)
    try:
        # 发送请求
        request_json = json.dumps(request_data).encode("utf-8") + b"\n\n"
        client_socket.sendall(request_json)

        # 接收响应
        response_data = b""
        while True:
            chunk = client_socket.recv(4096)
            if not chunk:
                break
            response_data += chunk
            if b"\n\n" in response_data:
                break

        if not response_data:
            raise Exception("No response from daemon")

        response: dict = json.loads(response_data.decode("utf-8"))
        return response
    finally:
        client_socket.close()


def start_daemon() -> bool:
    """启动 daemon 进程"""
    print("Starting Sprintest Daemon...")
    # 启动 daemon 进程
    subprocess.Popen(
        [sys.executable, "-m", "sprintest.daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # 等待 daemon 启动
    for _ in range(5):
        time.sleep(1)
        status = read_status()
        if status:
            print("Daemon started successfully!")
            return True
    print("Failed to start daemon. Please check logs.")
    return False


def find_target_pkg() -> str | None:
    """自动发现目标包"""
    # 查找 pyproject.toml
    if os.path.exists("pyproject.toml") and tomli is not None:
        try:
            with open("pyproject.toml", "rb") as f:
                config = tomli.load(f)
            # 查找项目名称
            if (
                "project" in config
                and "name" in config["project"]
                and isinstance(config["project"]["name"], str)
            ):
                return config["project"]["name"]
        except (ImportError, Exception):
            pass

    # 查找 setup.py
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
        except (ImportError, Exception):
            pass

    # 查找 src 目录下的包
    if os.path.exists("src"):
        src_items = os.listdir("src")
        for item in src_items:
            item_path = os.path.join("src", item)
            if os.path.isdir(item_path) and os.path.exists(
                os.path.join(item_path, "__init__.py")
            ):
                return item

    # 查找当前目录下的包
    current_items = os.listdir(".")
    for item in current_items:
        if item == "src":
            continue
        item_path = os.path.join(".", item)
        if os.path.isdir(item_path) and os.path.exists(
            os.path.join(item_path, "__init__.py")
        ):
            return item

    return None


def main() -> None:

    args = sys.argv[1:]

    if not args:
        print("Usage: sprintest [status|stop|--no-stream|--version] [pytest_args...]")
        sys.exit(0)

    if args[0] == "--version":
        print(f"sprintest {__version__}")
        sys.exit(0)

    # 读取状态文件
    status = read_status()
    if not status:
        # 启动 daemon
        if not start_daemon():
            sys.exit(1)
        status = read_status()

    if not status:
        print("Error: Daemon status not found")
        sys.exit(1)

    if args[0] == "status":
        try:
            if status.get("type") == "unix":
                # 使用 Unix socket
                socket_response = send_socket_request(
                    status["socket_path"], {"command": "status"}
                )
                print(
                    f"Sprintest Daemon ({socket_response['version']}): {socket_response['status']}"
                )
            else:
                # 使用 TCP
                http_response = requests.get(
                    f"http://localhost:{status.get('port', PORT)}/v1/status", timeout=5
                )
                http_response.raise_for_status()
                data = http_response.json()
                print(f"Sprintest Daemon ({data['version']}): {data['status']}")
        except Exception as e:
            print(f"Error: Could not reach daemon: {e}")
            sys.exit(1)
        sys.exit(0)

    if args[0] == "stop":
        try:
            if status.get("type") == "unix":
                # 使用 Unix socket
                socket_response = send_socket_request(
                    status["socket_path"], {"command": "stop"}
                )
                print(socket_response["message"])
            else:
                # 使用 TCP
                http_response = requests.post(
                    f"http://localhost:{status.get('port', PORT)}/v1/stop", timeout=5
                )
                http_response.raise_for_status()
                print(http_response.json()["message"])
        except Exception as e:
            print(f"Error: Could not stop daemon: {e}")
            sys.exit(1)
        sys.exit(0)

    use_stream = True
    if args[0] == "--no-stream":
        use_stream = False
        args = args[1:]
    elif args[0] == "--stream":
        use_stream = True
        args = args[1:]

    target_pkg = os.environ.get("SPRINTEST_TARGET_PKG")
    if not target_pkg:
        # 自动发现目标包
        target_pkg = find_target_pkg()
        if not target_pkg:
            print("Error: SPRINTEST_TARGET_PKG environment variable is required")
            sys.exit(1)
        print(f"[INFO] Auto-detected target package: {target_pkg}")
    payload = {"args": args, "target_pkg": target_pkg}

    try:
        if status.get("type") == "unix":
            # 使用 Unix socket
            socket_response = send_socket_request(
                status["socket_path"], {"command": "run_test", **payload}
            )
            print(socket_response["output"])
            sys.exit(socket_response["exit_code"])
        else:
            # 使用 TCP
            if use_stream:
                http_response = requests.post(
                    STREAM_URL, json=payload, timeout=None, stream=True
                )
                http_response.raise_for_status()
                exit_code = 0
                for chunk in http_response.iter_content(
                    chunk_size=None, decode_unicode=True
                ):
                    if chunk:
                        if "[DONE]" in chunk:
                            match = re.search(r"exit_code=(\d+)", chunk)
                            if match:
                                exit_code = int(match.group(1))
                        print(chunk, end="", flush=True)
                sys.exit(exit_code)
            else:
                http_response = requests.post(DAEMON_URL, json=payload, timeout=None)
                http_response.raise_for_status()
                data = http_response.json()
                print(data["output"])
                sys.exit(data["exit_code"])
    except (OSError, requests.exceptions.ConnectionError):
        # 连接失败，尝试启动 daemon
        print("Daemon not found. Starting daemon...")
        if start_daemon():
            # 重新读取状态
            status = read_status()
            if status:
                # 重试请求
                payload = {"args": args, "target_pkg": target_pkg}
                if status.get("type") == "unix":
                    socket_response = send_socket_request(
                        status["socket_path"], {"command": "run_test", **payload}
                    )
                    print(socket_response["output"])
                    sys.exit(socket_response["exit_code"])
                else:
                    if use_stream:
                        http_response = requests.post(
                            STREAM_URL, json=payload, timeout=None, stream=True
                        )
                        http_response.raise_for_status()
                        exit_code = 0
                        for chunk in http_response.iter_content(
                            chunk_size=None, decode_unicode=True
                        ):
                            if chunk:
                                if "[DONE]" in chunk:
                                    match = re.search(r"exit_code=(\d+)", chunk)
                                    if match:
                                        exit_code = int(match.group(1))
                                print(chunk, end="", flush=True)
                        sys.exit(exit_code)
                    else:
                        http_response = requests.post(
                            DAEMON_URL, json=payload, timeout=None
                        )
                        http_response.raise_for_status()
                        data = http_response.json()
                        print(data["output"])
                        sys.exit(data["exit_code"])
        print("Failed to start daemon. Please check logs.")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
