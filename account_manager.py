# -*- coding: utf-8 -*-
"""单进程多账号的目录、清单和机器人实例管理。"""
import copy
import json
import os
import re
import shutil
import threading
import tomllib
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional


ACCOUNT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
LEGACY_ACCOUNT_FILES = (
    "config.toml",
    "bilibili_cookie.json",
    "history.json",
    "review_drafts.json",
    "video_cache.json",
)


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

    @staticmethod
    def _is_path_within(parent: str, child: str) -> bool:
        try:
            return os.path.commonpath([parent, child]) == parent
        except ValueError:
            return False

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
                running_state = getattr(bot, "is_running", False) if bot else False
                item["running"] = bool(
                    running_state() if callable(running_state) else running_state
                )
                operations = (
                    bot.get_review_operation_status()
                    if bot is not None
                    else {
                        "active": {
                            "generating": 0,
                            "regenerating": 0,
                            "sending": 0,
                        },
                    }
                )
                operations["busy"] = any(operations.get("active", {}).values())
                item["operations"] = operations
                result.append(item)
            return result

    def current_account_id(self) -> str:
        with self._lock:
            return self._manifest["current_account_id"]

    def resolve_account_id(self, account_id: str = None) -> str:
        resolved = str(account_id or self.current_account_id())
        self.account_dir(resolved)
        return resolved

    def _assert_current_idle(self):
        current_bot = self._bots.get(self._manifest["current_account_id"])
        if not current_bot:
            return
        operations = current_bot.get_review_operation_status()
        if any(operations.get("active", {}).values()):
            raise AccountBusyError("当前账号有审核任务正在执行，暂时不能切换")

    def _pending_account_name(self) -> str:
        existing_names = {
            str(account.get("name") or "").strip()
            for account in self._manifest["accounts"]
        }
        base_name = "待登录账号"
        if base_name not in existing_names:
            return base_name
        suffix = 2
        while f"{base_name} {suffix}" in existing_names:
            suffix += 1
        return f"{base_name} {suffix}"

    def create_account(self, name: str) -> dict:
        clean_name = str(name or "").strip()
        if len(clean_name) > 40:
            raise ValueError("账号名称不能超过 40 个字符")
        with self._lock:
            self._assert_current_idle()
            if not clean_name:
                clean_name = self._pending_account_name()
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

    def import_legacy_account(self, name: str, source_dir: str) -> dict:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("账号名称不能为空")
        if len(clean_name) > 40:
            raise ValueError("账号名称不能超过 40 个字符")
        source = os.path.abspath(os.path.expanduser(str(source_dir or "").strip()))
        if not os.path.isdir(source):
            raise ValueError("旧数据目录不存在")
        if self._is_path_within(self.root_dir, source):
            raise ValueError("不能从当前产品数据目录内部导入")

        existing_files = [
            filename
            for filename in LEGACY_ACCOUNT_FILES
            if os.path.isfile(os.path.join(source, filename))
        ]
        if not existing_files:
            raise ValueError("目录中没有识别到旧版账号数据")
        for filename in existing_files:
            path = os.path.join(source, filename)
            if filename == "config.toml":
                with open(path, "rb") as f:
                    tomllib.load(f)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    json.load(f)

        with self._lock:
            self._assert_current_idle()
            account = {
                "id": self._new_account_id(),
                "name": clean_name,
                "created_at": self._now(),
                "imported_at": self._now(),
            }
            destination = os.path.abspath(
                os.path.join(self.accounts_dir, account["id"])
            )
            if not self._is_path_within(self.accounts_dir, destination):
                raise ValueError("导入目标目录非法")
            os.makedirs(destination, exist_ok=False)
            for filename in existing_files:
                shutil.copy2(
                    os.path.join(source, filename),
                    os.path.join(destination, filename),
                )
            self._manifest["accounts"].append(account)
            self._manifest["current_account_id"] = account["id"]
            self._save_manifest()
            result = copy.deepcopy(account)
            result["imported_files"] = existing_files
            return result

    def select_account(self, account_id: str) -> dict:
        account_id = self.resolve_account_id(account_id)
        with self._lock:
            self._assert_current_idle()
            self._manifest["current_account_id"] = account_id
            self._save_manifest()
            return next(
                copy.deepcopy(account)
                for account in self._manifest["accounts"]
                if account["id"] == account_id
            )

    def rename_account(self, account_id: str, name: str) -> dict:
        account_id = self.resolve_account_id(account_id)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("账号名称不能为空")
        if len(clean_name) > 40:
            raise ValueError("账号名称不能超过 40 个字符")
        with self._lock:
            account = next(
                item for item in self._manifest["accounts"]
                if item["id"] == account_id
            )
            if account.get("name") != clean_name:
                account["name"] = clean_name
                self._save_manifest()
            return copy.deepcopy(account)

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

    def get_loaded_bot(self, account_id: str):
        account_id = self.resolve_account_id(account_id)
        with self._lock:
            return self._bots.get(account_id)

    def shutdown_all(self):
        with self._lock:
            bots = list(self._bots.values())
        for bot in bots:
            bot.prepare_shutdown()
            logger = getattr(bot, "logger", None)
            if logger:
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)
