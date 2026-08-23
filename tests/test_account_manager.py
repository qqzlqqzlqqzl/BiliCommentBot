import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from io import BytesIO

from account_manager import (
    AccountBusyError,
    AccountManager,
    AccountNotFoundError,
    AutomaticRoundCoordinator,
    MIGRATION_MANIFEST_FILE,
)


class FakeBot:
    def __init__(self, account):
        self.account = account
        self.running = False
        self.busy = False
        self.shutdown_calls = 0
        self.automatic_round_context = None

    def is_running(self):
        return self.running

    def get_review_operation_status(self):
        return {
            "busy": self.busy,
            "active": {
                "generating": int(self.busy),
                "regenerating": 0,
                "sending": 0,
            },
        }

    def prepare_shutdown(self):
        self.shutdown_calls += 1

    def set_automatic_round_context(self, context_factory):
        self.automatic_round_context = context_factory


class AccountManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.created_bots = []

        def factory(account):
            bot = FakeBot(account)
            self.created_bots.append(bot)
            return bot

        self.manager = AccountManager(self.temp_dir.name, factory)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_creates_persistent_default_account(self):
        accounts = self.manager.list_accounts()

        self.assertEqual(len(accounts), 1)
        self.assertTrue(accounts[0]["current"])
        self.assertTrue(os.path.isdir(self.manager.account_dir(accounts[0]["id"])))

        reloaded = AccountManager(self.temp_dir.name, lambda account: FakeBot(account))
        self.assertEqual(
            reloaded.current_account_id(),
            self.manager.current_account_id(),
        )

    def test_two_accounts_use_distinct_directories_and_bots(self):
        first_id = self.manager.current_account_id()
        second = self.manager.create_account("第二个账号")

        first_bot = self.manager.get_bot(first_id)
        second_bot = self.manager.get_bot(second["id"])

        self.assertIsNot(first_bot, second_bot)
        self.assertNotEqual(
            os.path.normcase(first_bot.account["data_dir"]),
            os.path.normcase(second_bot.account["data_dir"]),
        )
        self.assertIsNotNone(first_bot.automatic_round_context)
        self.assertIsNotNone(second_bot.automatic_round_context)

    def test_automatic_rounds_are_global_serial_with_account_gap(self):
        coordinator = AutomaticRoundCoordinator(gap_seconds=0.12)
        release_first = threading.Event()
        first_entered = threading.Event()
        second_entered = threading.Event()
        timeline = {}
        allowed = {}

        def run_first():
            with coordinator.turn(
                "account-a",
                "账号 A",
                threading.Event(),
            ) as acquired:
                allowed["a"] = acquired
                timeline["a_start"] = time.monotonic()
                first_entered.set()
                release_first.wait(timeout=2)
                timeline["a_end"] = time.monotonic()

        def run_second():
            with coordinator.turn(
                "account-b",
                "账号 B",
                threading.Event(),
            ) as acquired:
                allowed["b"] = acquired
                timeline["b_start"] = time.monotonic()
                second_entered.set()

        first = threading.Thread(target=run_first)
        second = threading.Thread(target=run_second)
        first.start()
        self.assertTrue(first_entered.wait(timeout=1))
        second.start()
        self.assertFalse(second_entered.wait(timeout=0.04))

        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(allowed, {"a": True, "b": True})
        self.assertGreaterEqual(
            timeline["b_start"] - timeline["a_end"],
            0.08,
        )

    def test_waiting_automatic_round_can_be_cancelled(self):
        coordinator = AutomaticRoundCoordinator(gap_seconds=600)
        stop_event = threading.Event()
        stop_event.set()

        with coordinator.turn(
            "account-b",
            "账号 B",
            stop_event,
        ) as acquired:
            self.assertFalse(acquired)

    def test_disabled_account_leaves_automatic_round_queue(self):
        coordinator = AutomaticRoundCoordinator(gap_seconds=600)

        with coordinator.turn(
            "account-b",
            "账号 B",
            threading.Event(),
            should_continue=lambda: False,
        ) as acquired:
            self.assertFalse(acquired)

    def test_same_account_does_not_receive_inter_account_gap(self):
        coordinator = AutomaticRoundCoordinator(gap_seconds=600)
        stop_event = threading.Event()

        with coordinator.turn(
            "account-a",
            "账号 A",
            stop_event,
        ) as first:
            self.assertTrue(first)

        started_at = time.monotonic()
        with coordinator.turn(
            "account-a",
            "账号 A",
            stop_event,
        ) as second:
            self.assertTrue(second)

        self.assertLess(time.monotonic() - started_at, 0.1)

    def test_empty_account_name_creates_unique_pending_login_names(self):
        first_pending = self.manager.create_account("")
        second_pending = self.manager.create_account("   ")

        self.assertEqual(first_pending["name"], "待登录账号")
        self.assertEqual(second_pending["name"], "待登录账号 2")
        self.assertEqual(
            self.manager.current_account_id(),
            second_pending["id"],
        )

    def test_rename_account_persists_detected_bilibili_name(self):
        account_id = self.manager.current_account_id()

        renamed = self.manager.rename_account(account_id, "喵酱第一")
        reloaded = AccountManager(
            self.temp_dir.name,
            lambda account: FakeBot(account),
        )

        self.assertEqual(renamed["name"], "喵酱第一")
        self.assertEqual(reloaded.list_accounts()[0]["name"], "喵酱第一")

    def test_switch_is_blocked_while_current_account_is_busy(self):
        first_id = self.manager.current_account_id()
        second = self.manager.create_account("第二个账号")
        self.manager.select_account(first_id)
        self.manager.get_bot(first_id).busy = True

        with self.assertRaises(AccountBusyError):
            self.manager.select_account(second["id"])

        self.assertEqual(self.manager.current_account_id(), first_id)

    def test_switch_is_allowed_while_current_monitor_is_waiting(self):
        first_id = self.manager.current_account_id()
        second = self.manager.create_account("第二个账号")
        self.manager.select_account(first_id)
        first_bot = self.manager.get_bot(first_id)
        first_bot.running = True
        first_bot.busy = False

        selected = self.manager.select_account(second["id"])

        self.assertEqual(selected["id"], second["id"])
        self.assertEqual(self.manager.current_account_id(), second["id"])
        self.assertTrue(first_bot.is_running())

    def test_create_is_blocked_while_current_account_is_busy(self):
        first_id = self.manager.current_account_id()
        self.manager.get_bot(first_id).busy = True

        with self.assertRaises(AccountBusyError):
            self.manager.create_account("不应创建")

        self.assertEqual(len(self.manager.list_accounts()), 1)

    def test_imports_recognized_legacy_files_without_changing_source(self):
        with tempfile.TemporaryDirectory() as source_dir:
            config_path = os.path.join(source_dir, "config.toml")
            history_path = os.path.join(source_dir, "history.json")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write('[bilibili]\nuid = "123"\n')
            with open(history_path, "w", encoding="utf-8") as f:
                json.dump([{"comment_id": "1"}], f)

            imported = self.manager.import_legacy_account(
                "旧账号",
                source_dir,
            )
            destination = self.manager.account_dir(imported["id"])

            self.assertEqual(
                imported["imported_files"],
                ["config.toml", "history.json"],
            )
            self.assertTrue(os.path.isfile(os.path.join(destination, "config.toml")))
            self.assertTrue(os.path.isfile(os.path.join(destination, "history.json")))
            self.assertTrue(os.path.isfile(config_path))
            self.assertTrue(os.path.isfile(history_path))

    def test_exports_complete_sensitive_account_migration_bundle(self):
        account_id = self.manager.current_account_id()
        account_dir = self.manager.account_dir(account_id)
        with open(os.path.join(account_dir, "config.toml"), "w", encoding="utf-8") as f:
            f.write(
                '[bilibili]\nuid = "123"\n'
                '[ark]\napi_key = "secret-key"\n'
            )
        with open(os.path.join(account_dir, "bilibili_cookie.json"), "w", encoding="utf-8") as f:
            json.dump({"cookie": {"SESSDATA": "secret-cookie"}}, f)
        with open(os.path.join(account_dir, "history.json"), "w", encoding="utf-8") as f:
            json.dump([{"comment_id": "c1"}], f)
        with open(os.path.join(account_dir, "review_drafts.json"), "w", encoding="utf-8") as f:
            json.dump({"c2": {"comment": "待审核"}}, f)

        bundle = self.manager.export_account_bundle(account_id)

        with zipfile.ZipFile(BytesIO(bundle["content"])) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    MIGRATION_MANIFEST_FILE,
                    "config.toml",
                    "bilibili_cookie.json",
                    "history.json",
                    "review_drafts.json",
                },
            )
            manifest = json.loads(archive.read(MIGRATION_MANIFEST_FILE))
            self.assertEqual(manifest["uid"], "123")
            self.assertTrue(manifest["contains_sensitive_data"])
            self.assertIn(b"secret-key", archive.read("config.toml"))
            self.assertIn(b"secret-cookie", archive.read("bilibili_cookie.json"))

    def test_import_bundle_creates_account_on_new_computer(self):
        source_id = self.manager.current_account_id()
        source_dir = self.manager.account_dir(source_id)
        self.manager.rename_account(source_id, "来源账号")
        with open(os.path.join(source_dir, "config.toml"), "w", encoding="utf-8") as f:
            f.write('[bilibili]\nuid = "source-uid"\n')
        with open(os.path.join(source_dir, "history.json"), "w", encoding="utf-8") as f:
            json.dump([{"comment_id": "already-replied"}], f)
        bundle = self.manager.export_account_bundle(source_id)

        with tempfile.TemporaryDirectory() as target_root:
            target = AccountManager(target_root, lambda account: FakeBot(account))
            imported = target.import_account_bundle(bundle["content"])
            imported_dir = target.account_dir(imported["id"])

            self.assertEqual(imported["mode"], "created")
            self.assertEqual(imported["name"], "来源账号")
            self.assertEqual(target.current_account_id(), imported["id"])
            with open(os.path.join(imported_dir, "history.json"), encoding="utf-8") as f:
                self.assertEqual(
                    json.load(f)[0]["comment_id"],
                    "already-replied",
                )

    def test_same_uid_import_merges_history_without_overwriting_target_config(self):
        source_id = self.manager.current_account_id()
        source_dir = self.manager.account_dir(source_id)
        with open(os.path.join(source_dir, "config.toml"), "w", encoding="utf-8") as f:
            f.write(
                '[bilibili]\nuid = "same-uid"\n'
                '[ark]\napi_key = "source-secret"\n'
            )
        with open(os.path.join(source_dir, "history.json"), "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"comment_id": "remote", "reply_content": "远端"},
                    {"comment_id": "duplicate", "reply_content": "远端旧值"},
                ],
                f,
            )
        with open(os.path.join(source_dir, "review_drafts.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "remote": {"comment": "不应保留"},
                    "pending": {"comment": "应保留"},
                },
                f,
            )
        bundle = self.manager.export_account_bundle(source_id)

        with tempfile.TemporaryDirectory() as target_root:
            target = AccountManager(target_root, lambda account: FakeBot(account))
            target_id = target.current_account_id()
            target_dir = target.account_dir(target_id)
            with open(os.path.join(target_dir, "config.toml"), "w", encoding="utf-8") as f:
                f.write(
                    '[bilibili]\nuid = "same-uid"\n'
                    '[ark]\napi_key = "target-secret"\n'
                )
            with open(os.path.join(target_dir, "history.json"), "w", encoding="utf-8") as f:
                json.dump(
                    [
                        {"comment_id": "local", "reply_content": "本机"},
                        {"comment_id": "duplicate", "reply_content": "本机新值"},
                    ],
                    f,
                )
            with open(os.path.join(target_dir, "review_drafts.json"), "w", encoding="utf-8") as f:
                json.dump({"local": {"comment": "不应保留"}}, f)

            imported = target.import_account_bundle(bundle["content"])

            self.assertEqual(imported["mode"], "merged")
            self.assertEqual(imported["id"], target_id)
            self.assertEqual(imported["history_added"], 1)
            with open(os.path.join(target_dir, "config.toml"), encoding="utf-8") as f:
                target_config = f.read()
            self.assertIn("target-secret", target_config)
            self.assertNotIn("source-secret", target_config)
            with open(os.path.join(target_dir, "history.json"), encoding="utf-8") as f:
                merged_history = json.load(f)
            history_by_id = {
                item["comment_id"]: item for item in merged_history
            }
            self.assertEqual(
                set(history_by_id),
                {"remote", "duplicate", "local"},
            )
            self.assertEqual(
                history_by_id["duplicate"]["reply_content"],
                "本机新值",
            )
            with open(os.path.join(target_dir, "review_drafts.json"), encoding="utf-8") as f:
                merged_drafts = json.load(f)
            self.assertEqual(set(merged_drafts), {"pending"})

    def test_running_account_cannot_be_exported_or_merged(self):
        account_id = self.manager.current_account_id()
        account_dir = self.manager.account_dir(account_id)
        with open(os.path.join(account_dir, "config.toml"), "w", encoding="utf-8") as f:
            f.write('[bilibili]\nuid = "busy-uid"\n')
        bundle = self.manager.export_account_bundle(account_id)
        bot = self.manager.get_bot(account_id)
        bot.running = True

        with self.assertRaises(AccountBusyError):
            self.manager.export_account_bundle(account_id)
        with self.assertRaises(AccountBusyError):
            self.manager.import_account_bundle(bundle["content"])

    def test_import_cannot_switch_away_from_busy_current_account(self):
        source_id = self.manager.current_account_id()
        source_dir = self.manager.account_dir(source_id)
        with open(os.path.join(source_dir, "config.toml"), "w", encoding="utf-8") as f:
            f.write('[bilibili]\nuid = "incoming-uid"\n')
        bundle = self.manager.export_account_bundle(source_id)

        busy_account = self.manager.create_account("忙碌账号")
        busy_bot = self.manager.get_bot(busy_account["id"])
        busy_bot.busy = True

        with self.assertRaises(AccountBusyError):
            self.manager.import_account_bundle(bundle["content"])
        self.assertEqual(
            self.manager.current_account_id(),
            busy_account["id"],
        )

    def test_rejects_unknown_or_path_like_account_id(self):
        with self.assertRaises(AccountNotFoundError):
            self.manager.account_dir("../escape")
        with self.assertRaises(AccountNotFoundError):
            self.manager.get_bot("missing")

    def test_corrupt_manifest_fails_closed(self):
        manifest_file = os.path.join(self.temp_dir.name, "accounts.json")
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump({"accounts": [], "current_account_id": "missing"}, f)

        with self.assertRaises(RuntimeError):
            AccountManager(self.temp_dir.name, lambda account: FakeBot(account))


if __name__ == "__main__":
    unittest.main()
