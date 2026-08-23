# -*- coding: utf-8 -*-
"""单进程多账号的目录、清单和机器人实例管理。"""
import copy
import io
import json
import os
import re
import shutil
import threading
import time
import tomllib
import uuid
import zipfile
from contextlib import contextmanager
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
MIGRATION_ACCOUNT_FILES = (
    "config.toml",
    "bilibili_cookie.json",
    "history.json",
    "review_drafts.json",
)
MIGRATION_MANIFEST_FILE = "migration.json"
MIGRATION_FORMAT = "BiliCommentReviewer.account-backup"
MIGRATION_VERSION = 1
MIGRATION_ARCHIVE_MAX_BYTES = 64 * 1024 * 1024
MIGRATION_UNCOMPRESSED_MAX_BYTES = 128 * 1024 * 1024


class AccountNotFoundError(KeyError):
    pass


class AccountBusyError(RuntimeError):
    pass


class AutomaticRoundCoordinator:
    """把多个账号的后台自动轮次排成单队列，并在账号间保留冷却时间。"""

    def __init__(self, gap_seconds: float = 600.0):
        self.gap_seconds = max(0.0, float(gap_seconds))
        self._condition = threading.Condition()
        self._queue = []
        self._next_ticket = 0
        self._active_account_id = None
        self._last_account_id = None
        self._next_allowed_at = 0.0

    @contextmanager
    def turn(
        self,
        account_id: str,
        account_name: str,
        stop_event: threading.Event,
        logger=None,
        should_continue=None,
    ):
        account_id = str(account_id)
        account_name = str(account_name or account_id)
        acquired = False
        ticket = None
        announced_wait = False

        try:
            with self._condition:
                ticket = self._next_ticket
                self._next_ticket += 1
                self._queue.append((ticket, account_id))

                while True:
                    if (
                        stop_event.is_set()
                        or (
                            should_continue is not None
                            and not should_continue()
                        )
                    ):
                        break

                    now = time.monotonic()
                    is_first = bool(
                        self._queue
                        and self._queue[0] == (ticket, account_id)
                    )
                    cooldown = (
                        max(0.0, self._next_allowed_at - now)
                        if (
                            self._last_account_id is not None
                            and self._last_account_id != account_id
                        )
                        else 0.0
                    )
                    if (
                        self._active_account_id is None
                        and is_first
                        and cooldown <= 0
                    ):
                        self._queue.pop(0)
                        self._active_account_id = account_id
                        acquired = True
                        if logger:
                            logger.info(
                                "多账号自动任务获得执行权：账号%s开始本轮；"
                                "同一时间只运行一个账号",
                                account_name,
                            )
                        break

                    if logger and not announced_wait:
                        logger.info(
                            "多账号自动任务排队：账号%s等待前序账号完成，"
                            "账号之间至少间隔%s秒",
                            account_name,
                            int(self.gap_seconds),
                        )
                        announced_wait = True

                    wait_seconds = 0.5
                    if cooldown > 0:
                        wait_seconds = min(wait_seconds, cooldown)
                    self._condition.wait(timeout=max(0.01, wait_seconds))

                if not acquired:
                    self._queue = [
                        item for item in self._queue
                        if item != (ticket, account_id)
                    ]
                    self._condition.notify_all()

            yield acquired
        finally:
            if acquired:
                with self._condition:
                    if self._active_account_id == account_id:
                        self._active_account_id = None
                        self._last_account_id = account_id
                        self._next_allowed_at = (
                            time.monotonic() + self.gap_seconds
                        )
                        self._condition.notify_all()
                if logger:
                    logger.info(
                        "账号%s本轮自动任务结束；下一个账号最早%s秒后开始",
                        account_name,
                        int(self.gap_seconds),
                    )


class AccountManager:
    def __init__(
        self,
        root_dir: str,
        bot_factory: Callable[[dict], object],
        automatic_round_gap_seconds: float = 600.0,
    ):
        self.root_dir = os.path.abspath(root_dir)
        self.accounts_dir = os.path.join(self.root_dir, "accounts")
        self.manifest_file = os.path.join(self.root_dir, "accounts.json")
        self.bot_factory = bot_factory
        self.automatic_round_coordinator = AutomaticRoundCoordinator(
            automatic_round_gap_seconds
        )
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

    def _unique_account_name(self, name: str) -> str:
        existing_names = {
            str(account.get("name") or "").strip()
            for account in self._manifest["accounts"]
        }
        base_name = str(name or "").strip() or "迁移账号"
        if len(base_name) > 40:
            base_name = base_name[:40].rstrip()
        if base_name not in existing_names:
            return base_name
        suffix = 2
        while True:
            suffix_text = f" {suffix}"
            candidate = f"{base_name[:40 - len(suffix_text)].rstrip()}{suffix_text}"
            if candidate not in existing_names:
                return candidate
            suffix += 1

    def _assert_account_migration_idle(self, account_id: str):
        bot = self._bots.get(account_id)
        if bot is None:
            return
        running_state = getattr(bot, "is_running", False)
        running = bool(
            running_state() if callable(running_state) else running_state
        )
        operations = bot.get_review_operation_status()
        busy = any(operations.get("active", {}).values())
        if running or busy:
            raise AccountBusyError(
                "账号正在定时处理、生成或发送，停止任务后再迁移"
            )

    @staticmethod
    def _load_account_uid(directory: str) -> str:
        config_file = os.path.join(directory, "config.toml")
        if not os.path.isfile(config_file):
            return ""
        with open(config_file, "rb") as f:
            config = tomllib.load(f)
        return str(config.get("bilibili", {}).get("uid") or "").strip()

    @staticmethod
    def _write_bytes_atomic(path: str, content: bytes):
        temp_file = f"{path}.migration.tmp"
        with open(temp_file, "wb") as f:
            f.write(content)
        os.replace(temp_file, path)

    @staticmethod
    def _load_json_bytes(content: bytes, label: str, expected_type):
        try:
            value = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} 不是有效 JSON") from exc
        if not isinstance(value, expected_type):
            raise ValueError(f"{label} 数据结构不正确")
        return value

    def export_account_bundle(self, account_id: str = None) -> dict:
        account_id = self.resolve_account_id(account_id)
        with self._lock:
            self._assert_account_migration_idle(account_id)
            account = next(
                copy.deepcopy(item)
                for item in self._manifest["accounts"]
                if item["id"] == account_id
            )
            source_dir = self.account_dir(account_id)
            existing_files = [
                filename
                for filename in MIGRATION_ACCOUNT_FILES
                if os.path.isfile(os.path.join(source_dir, filename))
            ]
            if "config.toml" not in existing_files:
                raise ValueError("当前账号尚未保存配置，无法生成迁移包")
            uid = self._load_account_uid(source_dir)
            manifest = {
                "format": MIGRATION_FORMAT,
                "version": MIGRATION_VERSION,
                "exported_at": self._now(),
                "account_name": account.get("name") or "迁移账号",
                "uid": uid,
                "files": existing_files,
                "contains_sensitive_data": True,
            }
            buffer = io.BytesIO()
            with zipfile.ZipFile(
                buffer,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr(
                    MIGRATION_MANIFEST_FILE,
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
                for filename in existing_files:
                    archive.write(
                        os.path.join(source_dir, filename),
                        arcname=filename,
                    )
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            return {
                "content": buffer.getvalue(),
                "filename": f"BiliCommentReviewer-account-{stamp}.zip",
                "account_id": account_id,
                "account_name": account.get("name") or "",
                "uid": uid,
                "files": existing_files,
            }

    def import_account_bundle(self, content: bytes) -> dict:
        if not content:
            raise ValueError("迁移包为空")
        if len(content) > MIGRATION_ARCHIVE_MAX_BYTES:
            raise ValueError("迁移包超过 64 MB 上限")
        try:
            with zipfile.ZipFile(io.BytesIO(content), mode="r") as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                allowed_names = {
                    MIGRATION_MANIFEST_FILE,
                    *MIGRATION_ACCOUNT_FILES,
                }
                names = [item.filename for item in members]
                if len(names) != len(set(names)):
                    raise ValueError("迁移包包含重复文件")
                if any(
                    name not in allowed_names
                    or name != os.path.basename(name)
                    for name in names
                ):
                    raise ValueError("迁移包包含不允许的文件")
                if sum(item.file_size for item in members) > MIGRATION_UNCOMPRESSED_MAX_BYTES:
                    raise ValueError("迁移包解压后超过 128 MB 上限")
                if MIGRATION_MANIFEST_FILE not in names:
                    raise ValueError("迁移包缺少 migration.json")
                files = {
                    item.filename: archive.read(item)
                    for item in members
                }
        except zipfile.BadZipFile as exc:
            raise ValueError("文件不是有效的迁移 ZIP") from exc

        manifest = self._load_json_bytes(
            files[MIGRATION_MANIFEST_FILE],
            MIGRATION_MANIFEST_FILE,
            dict,
        )
        if manifest.get("format") != MIGRATION_FORMAT:
            raise ValueError("不是 BiliCommentReviewer 账号迁移包")
        if manifest.get("version") != MIGRATION_VERSION:
            raise ValueError("迁移包版本不受支持")
        declared_files = manifest.get("files")
        if not isinstance(declared_files, list):
            raise ValueError("迁移包文件清单不正确")
        if len(declared_files) != len(set(declared_files)):
            raise ValueError("迁移包文件清单包含重复项")
        actual_account_files = [
            name for name in names if name in MIGRATION_ACCOUNT_FILES
        ]
        if set(declared_files) != set(actual_account_files):
            raise ValueError("迁移包文件清单与实际内容不一致")
        if "config.toml" not in files:
            raise ValueError("迁移包缺少 config.toml")

        try:
            imported_config = tomllib.loads(
                files["config.toml"].decode("utf-8-sig")
            )
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError("迁移包中的 config.toml 无效") from exc
        imported_uid = str(
            imported_config.get("bilibili", {}).get("uid") or ""
        ).strip()
        imported_history = self._load_json_bytes(
            files.get("history.json", b"[]"),
            "history.json",
            list,
        )
        imported_drafts = self._load_json_bytes(
            files.get("review_drafts.json", b"{}"),
            "review_drafts.json",
            dict,
        )
        if "bilibili_cookie.json" in files:
            self._load_json_bytes(
                files["bilibili_cookie.json"],
                "bilibili_cookie.json",
                dict,
            )

        with self._lock:
            self._assert_current_idle()
            matching_account = None
            if imported_uid:
                for account in self._manifest["accounts"]:
                    try:
                        account_uid = self._load_account_uid(
                            self.account_dir(account["id"])
                        )
                    except (OSError, ValueError, tomllib.TOMLDecodeError):
                        continue
                    if account_uid == imported_uid:
                        matching_account = account
                        break

            if matching_account is None:
                account = {
                    "id": self._new_account_id(),
                    "name": self._unique_account_name(
                        manifest.get("account_name") or "迁移账号"
                    ),
                    "created_at": self._now(),
                    "imported_at": self._now(),
                }
                destination = os.path.abspath(
                    os.path.join(self.accounts_dir, account["id"])
                )
                if not self._is_path_within(self.accounts_dir, destination):
                    raise ValueError("导入目标目录非法")
                os.makedirs(destination, exist_ok=False)
                for filename in actual_account_files:
                    self._write_bytes_atomic(
                        os.path.join(destination, filename),
                        files[filename],
                    )
                self._manifest["accounts"].append(account)
                self._manifest["current_account_id"] = account["id"]
                self._save_manifest()
                result = copy.deepcopy(account)
                result.update({
                    "mode": "created",
                    "uid": imported_uid,
                    "imported_files": actual_account_files,
                    "history_added": len(imported_history),
                })
                return result

            account_id = matching_account["id"]
            self._assert_account_migration_idle(account_id)
            destination = self.account_dir(account_id)
            history_file = os.path.join(destination, "history.json")
            drafts_file = os.path.join(destination, "review_drafts.json")
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    local_history = json.load(f)
            except FileNotFoundError:
                local_history = []
            if not isinstance(local_history, list):
                raise ValueError("目标账号 history.json 数据结构不正确")
            try:
                with open(drafts_file, "r", encoding="utf-8") as f:
                    local_drafts = json.load(f)
            except FileNotFoundError:
                local_drafts = {}
            if not isinstance(local_drafts, dict):
                raise ValueError("目标账号 review_drafts.json 数据结构不正确")

            merged_by_id = {}
            ordered_ids = []
            unkeyed_items = []
            for item in [*imported_history, *local_history]:
                if not isinstance(item, dict):
                    raise ValueError("history.json 包含无效记录")
                comment_id = str(item.get("comment_id") or "").strip()
                if not comment_id:
                    unkeyed_items.append(item)
                    continue
                if comment_id not in merged_by_id:
                    ordered_ids.append(comment_id)
                merged_by_id[comment_id] = item
            merged_history = [
                merged_by_id[comment_id] for comment_id in ordered_ids
            ]
            merged_history.extend(unkeyed_items)
            local_ids = {
                str(item.get("comment_id") or "").strip()
                for item in local_history
                if isinstance(item, dict) and item.get("comment_id")
            }
            imported_ids = {
                str(item.get("comment_id") or "").strip()
                for item in imported_history
                if isinstance(item, dict) and item.get("comment_id")
            }

            merged_drafts = {
                str(comment_id): draft
                for comment_id, draft in imported_drafts.items()
            }
            merged_drafts.update({
                str(comment_id): draft
                for comment_id, draft in local_drafts.items()
            })
            processed_ids = set(merged_by_id)
            for comment_id in processed_ids:
                merged_drafts.pop(comment_id, None)

            self._write_bytes_atomic(
                history_file,
                json.dumps(
                    merged_history,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            )
            self._write_bytes_atomic(
                drafts_file,
                json.dumps(
                    merged_drafts,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            )
            matching_account["imported_at"] = self._now()
            self._manifest["current_account_id"] = account_id
            self._save_manifest()
            self._bots.pop(account_id, None)
            result = copy.deepcopy(matching_account)
            result.update({
                "mode": "merged",
                "uid": imported_uid,
                "imported_files": [
                    "history.json",
                    "review_drafts.json",
                ],
                "history_added": len(imported_ids - local_ids),
                "history_total": len(merged_history),
            })
            return result

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
                set_context = getattr(
                    bot,
                    "set_automatic_round_context",
                    None,
                )
                if callable(set_context):
                    def automatic_round_context(
                        stop_event,
                        logger,
                        should_continue=None,
                        aid=account_id,
                        name=account.get("name", account_id),
                    ):
                        return self.automatic_round_coordinator.turn(
                            aid,
                            name,
                            stop_event,
                            logger,
                            should_continue,
                        )

                    set_context(automatic_round_context)
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
