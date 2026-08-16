import os
import tempfile
import unittest
from unittest.mock import patch

import server


class ServerAccountApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_patch = patch.dict(
            os.environ,
            {
                "BILI_PRODUCT_DATA_DIR": self.temp_dir.name,
                "BILI_AUTO_START_MONITOR": "0",
            },
            clear=False,
        )
        self.env_patch.start()
        server._account_manager = None
        server._account_log_handlers.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        if server._account_manager is not None:
            server._account_manager.shutdown_all()
        server._account_manager = None
        server._account_log_handlers.clear()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def test_creates_and_selects_accounts_through_api(self):
        initial = self.client.get("/api/accounts").get_json()
        first_id = initial["current_account_id"]

        created = self.client.post(
            "/api/accounts",
            json={"name": "第二个账号"},
        )
        second_id = created.get_json()["account"]["id"]

        self.assertEqual(created.status_code, 200)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(
            self.client.get("/api/accounts").get_json()["current_account_id"],
            second_id,
        )

        selected = self.client.post(
            "/api/accounts/select",
            json={"account_id": first_id},
        )
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(
            self.client.get("/api/accounts").get_json()["current_account_id"],
            first_id,
        )

    def test_account_configs_are_isolated(self):
        first_id = self.client.get("/api/accounts").get_json()["current_account_id"]
        first_save = self.client.post(
            "/api/config",
            json={"bilibili": {"uid": "111"}},
        )
        second_id = self.client.post(
            "/api/accounts",
            json={"name": "第二个账号"},
        ).get_json()["account"]["id"]

        second_before = self.client.get("/api/config").get_json()["config"]
        second_save = self.client.post(
            "/api/config",
            json={"bilibili": {"uid": "222"}},
        )
        self.client.post(
            "/api/accounts/select",
            json={"account_id": first_id},
        )
        first_after = self.client.get("/api/config").get_json()["config"]

        self.assertEqual(first_save.status_code, 200)
        self.assertEqual(second_save.status_code, 200)
        self.assertEqual(second_before["bilibili"]["uid"], "")
        self.assertEqual(first_after["bilibili"]["uid"], "111")
        self.assertNotEqual(first_id, second_id)

    def test_unknown_account_cannot_be_selected(self):
        response = self.client.post(
            "/api/accounts/select",
            json={"account_id": "missing"},
        )

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
