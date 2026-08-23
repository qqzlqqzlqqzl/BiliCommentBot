# -*- coding: utf-8 -*-
"""使用明确命令行参数启动一个隔离的 BiliBot Web 实例。"""
import argparse
import os
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--account-slot", required=True, choices=("1", "2"))
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--auto-start-monitor", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    project_dir = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir)
    if args.data_dir and not data_dir.is_absolute():
        data_dir = project_dir / data_dir

    os.environ["BILI_PORT"] = str(args.port)
    os.environ["BILI_ACCOUNT_NAME"] = f"账号{args.account_slot}"
    os.environ["BILI_DATA_DIR"] = str(data_dir) if args.data_dir else ""
    os.environ["BILI_AUTO_START_MONITOR"] = "1" if args.auto_start_monitor else "0"

    import server
    server.main()


if __name__ == "__main__":
    main()
