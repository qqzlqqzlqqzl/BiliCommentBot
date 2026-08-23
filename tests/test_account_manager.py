import json
import os
import tempfile
import threading
import time
import unittest

from account_manager import (
    AccountBusyError,
    AccountManager,
    AccountNotFoundError,
    AutomaticRoundCoordinator,
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
