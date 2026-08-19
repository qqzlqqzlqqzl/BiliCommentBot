# -*- coding: utf-8 -*-
"""Windows 产品入口：单实例、本地数据目录、内部端口和浏览器启动。"""
import json
import multiprocessing
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path


PRODUCT_ID = "BiliCommentReviewer"
PRODUCT_NAME = "B站评论审核助手"
DEFAULT_PRODUCT_PORT = 5000


def product_data_root() -> Path:
    override = os.environ.get("BILI_PRODUCT_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / PRODUCT_ID
    return Path.home() / "AppData" / "Local" / PRODUCT_ID


def show_error(message: str):
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, PRODUCT_NAME, 0x10)
    except Exception:
        pass


def open_log_streams(root: Path):
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stream = open(
        logs_dir / "launcher.log",
        "a",
        encoding="utf-8",
        buffering=1,
    )
    sys.stdout = stream
    sys.stderr = stream
    return stream


def acquire_single_instance(root: Path):
    import msvcrt

    lock_file = root / "app.lock"
    handle = open(lock_file, "a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return handle
    except OSError:
        handle.close()
        return None


def local_port_is_available(port: int) -> bool:
    if not 1 <= int(port) <= 65535:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False


def choose_local_port(previous_port: int = None) -> int:
    for preferred in (DEFAULT_PRODUCT_PORT, previous_port):
        if preferred and local_port_is_available(preferred):
            return int(preferred)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def runtime_file(root: Path) -> Path:
    return root / "runtime.json"


def write_runtime(root: Path, port: int):
    target = runtime_file(root)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(
            {
                "product": PRODUCT_ID,
                "port": port,
                "pid": os.getpid(),
                "started_at": int(time.time()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temp, target)


def read_runtime_port(root: Path):
    try:
        data = json.loads(runtime_file(root).read_text(encoding="utf-8"))
        if data.get("product") != PRODUCT_ID:
            return None
        port = int(data.get("port", 0))
        return port if 1 <= port <= 65535 else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/api/health"


def app_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def wait_until_ready(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url(port), timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("ok") and payload.get("product") == PRODUCT_ID:
                return True
        except Exception:
            time.sleep(0.2)
    return False


def open_when_ready(port: int):
    if wait_until_ready(port):
        webbrowser.open(app_url(port))
    else:
        show_error("本地服务启动超时。请查看应用数据目录中的 logs\\launcher.log。")


def open_existing_instance(root: Path) -> bool:
    port = read_runtime_port(root)
    if port and wait_until_ready(port, timeout=3.0):
        if os.environ.get("BILI_DISABLE_BROWSER", "").strip() != "1":
            webbrowser.open(app_url(port))
        return True
    return False


def configure_environment(root: Path, port: int):
    os.environ["BILI_PRODUCT_DATA_DIR"] = str(root)
    os.environ["BILI_PORT"] = str(port)
    os.environ["BILI_HOST"] = "127.0.0.1"
    os.environ.pop("BILI_AUTO_START_MONITOR", None)
    os.environ["BILI_OPEN_BROWSER"] = "0"
    os.environ["PYTHONUTF8"] = "1"
    os.environ.pop("BILI_REVIEW_HARD_LIMIT", None)


def main():
    multiprocessing.freeze_support()
    root = product_data_root()
    root.mkdir(parents=True, exist_ok=True)
    log_stream = open_log_streams(root)
    lock_handle = acquire_single_instance(root)
    if lock_handle is None:
        if not open_existing_instance(root):
            show_error("程序似乎已经在运行，但本地页面暂时无法连接。")
        return

    port = choose_local_port(read_runtime_port(root))
    configure_environment(root, port)
    write_runtime(root, port)
    if os.environ.get("BILI_DISABLE_BROWSER", "").strip() != "1":
        threading.Thread(
            target=open_when_ready,
            args=(port,),
            daemon=True,
        ).start()

    try:
        from server import main as run_server

        run_server()
    except Exception:
        traceback.print_exc()
        show_error("程序启动失败。详细信息已写入 logs\\launcher.log。")
        raise
    finally:
        lock_handle.close()
        log_stream.flush()


if __name__ == "__main__":
    main()
