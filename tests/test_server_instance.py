import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import server


class ServerInstanceTests(unittest.TestCase):
    def test_default_instance_uses_port_5000(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server.get_server_port(), 5000)
            self.assertEqual(server.get_instance_name(), "账号 1")

    def test_second_instance_reads_environment(self):
        with patch.dict(
            os.environ,
            {"BILI_PORT": "5001", "BILI_ACCOUNT_NAME": "账号2"},
            clear=True,
        ):
            self.assertEqual(server.get_server_port(), 5001)
            self.assertEqual(server.get_instance_name(), "账号2")

    def test_invalid_port_is_rejected(self):
        with patch.dict(os.environ, {"BILI_PORT": "70000"}, clear=True):
            with self.assertRaises(RuntimeError):
                server.get_server_port()

    def test_health_endpoint_identifies_product(self):
        response = server.app.test_client().get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["product"], "BiliCommentReviewer")

    def test_product_monitor_restart_state_comes_from_account_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(
                server.should_auto_start_monitor({
                    "bilibili": {"auto_start_monitor": True}
                })
            )
            self.assertFalse(
                server.should_auto_start_monitor({
                    "bilibili": {"auto_start_monitor": False}
                })
            )

    def test_source_launcher_can_explicitly_override_monitor_restart_state(self):
        config = {"bilibili": {"auto_start_monitor": True}}
        with patch.dict(
            os.environ,
            {"BILI_AUTO_START_MONITOR": "0"},
            clear=True,
        ):
            self.assertFalse(server.should_auto_start_monitor(config))

    def test_product_restore_starts_every_enabled_account_independently(self):
        accounts = [
            {"id": "account-a", "name": "账号 A"},
            {"id": "account-b", "name": "账号 B"},
            {"id": "account-c", "name": "账号 C"},
        ]
        configs = {
            "account-a": {
                "bilibili": {
                    "auto_start_monitor": True,
                    "cookie": "SESSDATA=a",
                },
                "ark": {"api_key": "ark-a"},
            },
            "account-b": {
                "bilibili": {
                    "auto_start_monitor": True,
                    "cookie": "SESSDATA=b",
                },
                "ark": {"api_key": "ark-b"},
            },
            "account-c": {
                "bilibili": {
                    "auto_start_monitor": False,
                    "cookie": "SESSDATA=c",
                },
                "ark": {"api_key": "ark-c"},
            },
        }
        bots = {
            account["id"]: Mock(
                reload_config=Mock(return_value=True),
                start=Mock(return_value=True),
            )
            for account in accounts
        }
        manager = Mock()
        manager.list_accounts.return_value = accounts
        manager.get_bot.side_effect = lambda account_id: bots[account_id]

        with (
            patch.object(
                server,
                "load_config",
                side_effect=lambda account_id: configs[account_id],
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            result = server.restore_product_account_monitors(manager)

        self.assertEqual(result["started"], ["account-a", "account-b"])
        bots["account-a"].start.assert_called_once_with()
        bots["account-b"].start.assert_called_once_with()
        bots["account-c"].start.assert_not_called()

    def test_product_restore_failure_does_not_block_other_account(self):
        accounts = [
            {"id": "broken", "name": "坏账号"},
            {"id": "healthy", "name": "正常账号"},
        ]
        config = {
            "bilibili": {
                "auto_start_monitor": True,
                "cookie": "SESSDATA=value",
            },
            "ark": {"api_key": "ark"},
        }
        healthy_bot = Mock(
            reload_config=Mock(return_value=True),
            start=Mock(return_value=True),
        )
        manager = Mock()
        manager.list_accounts.return_value = accounts

        def get_bot(account_id):
            if account_id == "broken":
                raise RuntimeError("配置损坏")
            return healthy_bot

        manager.get_bot.side_effect = get_bot
        with (
            patch.object(server, "load_config", return_value=config),
            patch.dict(os.environ, {}, clear=True),
        ):
            result = server.restore_product_account_monitors(manager)

        self.assertEqual(result["started"], ["healthy"])
        self.assertEqual(result["failed"][0]["account_id"], "broken")
        healthy_bot.start.assert_called_once_with()

    def test_product_restore_accepts_account_cookie_file(self):
        with tempfile.TemporaryDirectory() as account_dir:
            cookie_file = os.path.join(account_dir, "bilibili_cookie.json")
            with open(cookie_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"cookie": {"SESSDATA": "saved-session"}},
                    f,
                )

            bot = Mock(
                reload_config=Mock(return_value=True),
                start=Mock(return_value=True),
            )
            manager = Mock()
            manager.list_accounts.return_value = [
                {"id": "legacy", "name": "旧账号"},
            ]
            manager.account_dir.return_value = account_dir
            manager.get_bot.return_value = bot
            config = {
                "bilibili": {
                    "auto_start_monitor": True,
                    "cookie": "",
                },
                "ark": {"api_key": "ark"},
            }

            with (
                patch.object(server, "load_config", return_value=config),
                patch.dict(os.environ, {}, clear=True),
            ):
                result = server.restore_product_account_monitors(manager)

        self.assertEqual(result["started"], ["legacy"])
        bot.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
