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

    def test_imports_legacy_account_through_api(self):
        with tempfile.TemporaryDirectory() as source_dir:
            with open(
                os.path.join(source_dir, "config.toml"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write('[bilibili]\nuid = "legacy-uid"\n')

            response = self.client.post(
                "/api/accounts/import",
                json={"name": "旧账号", "source_dir": source_dir},
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["account"]["name"], "旧账号")
        self.assertEqual(
            server.load_config()["bilibili"]["uid"],
            "legacy-uid",
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

    def test_qr_cookie_is_saved_only_to_target_account(self):
        first_id = self.client.get("/api/accounts").get_json()["current_account_id"]
        second_id = self.client.post(
            "/api/accounts",
            json={"name": "第二个账号"},
        ).get_json()["account"]["id"]

        server._persist_qr_cookie(
            second_id,
            "SESSDATA=second; bili_jct=csrf-second",
        )

        self.assertEqual(
            server.load_config(first_id)["bilibili"]["cookie"],
            "",
        )
        self.assertEqual(
            server.load_config(second_id)["bilibili"]["cookie"],
            "SESSDATA=second; bili_jct=csrf-second",
        )

    def test_detected_identity_updates_uid_and_account_name(self):
        account_id = self.client.get("/api/accounts").get_json()["current_account_id"]

        server._save_identity(
            "3546589337487797",
            "喵酱第一",
            account_id,
        )

        self.assertEqual(
            server.load_config(account_id)["bilibili"]["uid"],
            "3546589337487797",
        )
        account = next(
            item
            for item in self.client.get("/api/accounts").get_json()["accounts"]
            if item["id"] == account_id
        )
        self.assertEqual(account["name"], "喵酱第一")

    def test_config_get_does_not_return_saved_secrets(self):
        self.client.post(
            "/api/config",
            json={
                "bilibili": {
                    "cookie": "SESSDATA=secret",
                    "refresh_token": "refresh-secret",
                },
                "ark": {"api_key": "ark-secret"},
                "auth": {"password": "password-hash"},
            },
        )

        payload = self.client.get("/api/config").get_json()

        self.assertNotIn("cookie", payload["config"]["bilibili"])
        self.assertNotIn("refresh_token", payload["config"]["bilibili"])
        self.assertNotIn("api_key", payload["config"]["ark"])
        self.assertNotIn("password", payload["config"]["auth"])
        self.assertTrue(payload["capabilities"]["bilibili_cookie_configured"])
        self.assertTrue(payload["capabilities"]["ark_api_key_configured"])

    def test_blank_secret_fields_preserve_existing_values(self):
        self.client.post(
            "/api/config",
            json={
                "bilibili": {
                    "cookie": "SESSDATA=secret",
                    "refresh_token": "refresh-secret",
                },
                "ark": {"api_key": "ark-secret"},
            },
        )

        self.client.post(
            "/api/config",
            json={
                "bilibili": {"cookie": "", "refresh_token": "", "uid": "123"},
                "ark": {"api_key": "", "model": "model-test"},
            },
        )
        stored = server.load_config()

        self.assertEqual(stored["bilibili"]["cookie"], "SESSDATA=secret")
        self.assertEqual(stored["bilibili"]["refresh_token"], "refresh-secret")
        self.assertEqual(stored["ark"]["api_key"], "ark-secret")
        self.assertEqual(stored["bilibili"]["uid"], "123")

    def test_saved_secrets_require_explicit_clear_action(self):
        self.client.post(
            "/api/config",
            json={
                "bilibili": {
                    "cookie": "SESSDATA=secret",
                    "refresh_token": "refresh-secret",
                },
                "ark": {"api_key": "ark-secret"},
            },
        )

        login_result = self.client.post(
            "/api/config/secrets/clear",
            json={"secret": "bilibili_login"},
        )
        ark_result = self.client.post(
            "/api/config/secrets/clear",
            json={"secret": "ark_api_key"},
        )
        stored = server.load_config()

        self.assertEqual(login_result.status_code, 200)
        self.assertEqual(ark_result.status_code, 200)
        self.assertEqual(stored["bilibili"]["cookie"], "")
        self.assertEqual(stored["bilibili"]["refresh_token"], "")
        self.assertEqual(stored["ark"]["api_key"], "")

    def test_rendered_product_page_uses_debug_cap_without_removing_full_options(self):
        with patch.dict(
            os.environ,
            {"BILI_REVIEW_HARD_LIMIT": "110"},
            clear=False,
        ):
            html = self.client.get("/").get_data(as_text=True)

        self.assertIn("const REVIEW_HARD_LIMIT = Number(110)", html)
        self.assertIn('<option value="50000">', html)


if __name__ == "__main__":
    unittest.main()
