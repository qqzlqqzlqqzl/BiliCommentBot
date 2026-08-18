import json
import os
import tempfile
import unittest

from account_manager import (
    AccountBusyError,
    AccountManager,
    AccountNotFoundError,
)


class FakeBot:
    def __init__(self, account):
        self.account = account
        self.running = False
        self.busy = False
        self.shutdown_calls = 0

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
