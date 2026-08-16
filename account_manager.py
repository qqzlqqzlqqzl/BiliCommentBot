# -*- coding: utf-8 -*-
"""单进程多账号的目录、清单和机器人实例管理。"""
import copy
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional


ACCOUNT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AccountNotFoundError(KeyError):
    pass


class AccountBusyError(RuntimeError):
    pass


class AccountManager:
    def __init__(self, root_dir: str, bot_factory: Callable[[dict], object]):
        self.root_dir = os.path.abspath(root_dir)
        self.accounts_dir = os.path.join(self.root_dir, "accounts")
        self.manifest_file = os.path.join(self.root_dir, "accounts.json")
        self.bot_factory = bot_factory
        self._lock = threading.RLock()
        self._bots = {}
        os.makedirs(self.accounts_dir, exist_ok=True)
        self._manifest = self._load_manifest()

    @staticmethod
    def _new_account_id() -> str:
        return f"account-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _default_manifest(self) -> dict:
        account_id = self._new_account_id()
        return {
            "version": 1,
            "current_account_id": account_id,
            "accounts": [
                {
                    "id": account_id,
                    "name": "账号 1",
                    "created_at": self._now(),
                }
            ],
        }

    def _load_manifest(self) -> dict:
        if not os.path.exists(self.manifest_file):
            manifest = self._default_manifest()
            self._save_manifest(manifest)
            return manifest
        try:
            with open(self.manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            self._validate_manifest(manifest)
            return manifest
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"账号清单损坏，无法安全启动: {exc}") from exc

    def _validate_manifest(self, manifest: dict):
        if not isinstance(manifest, dict):
            raise ValueError("账号清单不是对象")
        accounts = manifest.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            raise ValueError("账号清单为空")
        ids = set()
        for account in accounts:
            if not isinstance(account, dict):
                raise ValueError("账号记录格式错误")
            account_id = str(account.get("id", ""))
            if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
                raise ValueError(f"账号 ID 非法: {account_id}")
            if account_id in ids:
                raise ValueError(f"账号 ID 重复: {account_id}")
            ids.add(account_id)
        if manifest.get("current_account_id") not in ids:
            raise ValueError("当前账号不存在")

    def _save_manifest(self, manifest: Optional[dict] = None):
        manifest = manifest or self._manifest
        os.makedirs(self.root_dir, exist_ok=True)
        temp_file = f"{self.manifest_file}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, self.manifest_file)

    def account_dir(self, account_id: str) -> str:
        account_id = str(account_id)
        if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
            raise AccountNotFoundError(account_id)
        with self._lock:
            if not any(
                account["id"] == account_id
                for account in self._manifest["accounts"]
            ):
                raise AccountNotFoundError(account_id)
        path = os.path.abspath(os.path.join(self.accounts_dir, account_id))
        if os.path.commonpath([self.accounts_dir, path]) != self.accounts_dir:
            raise AccountNotFoundError(account_id)
        os.makedirs(path, exist_ok=True)
        return path

    def list_accounts(self) -> list:
        with self._lock:
            current_id = self._manifest["current_account_id"]
            result = []
            for account in self._manifest["accounts"]:
                item = copy.deepcopy(account)
                item["current"] = item["id"] == current_id
                bot = self._bots.get(item["id"])
                item["loaded"] = bot is not None
                item["running"] = bool(bot and bot.is_running())
                item["operations"] = (
                    bot.get_review_operation_status()
                    if bot is not None
                    else {
                        "busy": False,
                        "active": {
                            "generating": 0,
                            "regenerating": 0,
                            "sending": 0,
                        },
                    }
                )
                result.append(item)
            return result

    def current_account_id(self) -> str:
        with self._lock:
            return self._manifest["current_account_id"]

    def resolve_account_id(self, account_id: str = None) -> str:
        resolved = str(account_id or self.current_account_id())
        self.account_dir(resolved)
        return resolved

    def create_account(self, name: str) -> dict:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("账号名称不能为空")
        if len(clean_name) > 40:
            raise ValueError("账号名称不能超过 40 个字符")
        with self._lock:
            account = {
                "id": self._new_account_id(),
                "name": clean_name,
                "created_at": self._now(),
            }
            self._manifest["accounts"].append(account)
            self._manifest["current_account_id"] = account["id"]
            self.account_dir(account["id"])
            self._save_manifest()
            return copy.deepcopy(account)

    def select_account(self, account_id: str) -> dict:
        account_id = self.resolve_account_id(account_id)
        with self._lock:
            current_bot = self._bots.get(self._manifest["current_account_id"])
            if current_bot and current_bot.get_review_operation_status()["busy"]:
                raise AccountBusyError("当前账号有审核任务正在执行，暂时不能切换")
            self._manifest["current_account_id"] = account_id
            self._save_manifest()
            return next(
                copy.deepcopy(account)
                for account in self._manifest["accounts"]
                if account["id"] == account_id
            )

    def get_bot(self, account_id: str = None):
        account_id = self.resolve_account_id(account_id)
        with self._lock:
            bot = self._bots.get(account_id)
            if bot is None:
                account = next(
                    copy.deepcopy(item)
                    for item in self._manifest["accounts"]
                    if item["id"] == account_id
                )
                account["data_dir"] = self.account_dir(account_id)
                bot = self.bot_factory(account)
                self._bots[account_id] = bot
            return bot

    def shutdown_all(self):
        with self._lock:
            bots = list(self._bots.values())
        for bot in bots:
            bot.prepare_shutdown()
