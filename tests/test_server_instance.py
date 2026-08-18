import os
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
