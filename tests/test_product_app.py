import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import product_app


class ProductAppTests(unittest.TestCase):
    def test_product_data_root_prefers_explicit_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {"BILI_PRODUCT_DATA_DIR": temp_dir},
                clear=False,
            ):
                self.assertEqual(
                    product_app.product_data_root(),
                    Path(temp_dir).resolve(),
                )

    def test_runtime_file_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            product_app.write_runtime(root, 32123)

            self.assertEqual(product_app.read_runtime_port(root), 32123)
            payload = json.loads(
                product_app.runtime_file(root).read_text(encoding="utf-8")
            )
            self.assertEqual(payload["product"], product_app.PRODUCT_ID)

    def test_configure_environment_uses_product_mode_without_debug_cap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {"BILI_REVIEW_HARD_LIMIT": "110"},
                clear=False,
            ):
                product_app.configure_environment(Path(temp_dir), 32123)

                self.assertEqual(
                    os.environ["BILI_PRODUCT_DATA_DIR"],
                    temp_dir,
                )
                self.assertEqual(os.environ["BILI_PORT"], "32123")
                self.assertNotIn("BILI_AUTO_START_MONITOR", os.environ)
                self.assertEqual(os.environ["BILI_OPEN_BROWSER"], "0")
                self.assertNotIn("BILI_REVIEW_HARD_LIMIT", os.environ)

    def test_health_url_is_local_only(self):
        self.assertEqual(
            product_app.health_url(32123),
            "http://127.0.0.1:32123/api/health",
        )

    def test_choose_local_port_prefers_stable_product_port(self):
        with patch.object(
            product_app,
            "local_port_is_available",
            side_effect=lambda port: port == product_app.DEFAULT_PRODUCT_PORT,
        ):
            self.assertEqual(
                product_app.choose_local_port(previous_port=32123),
                product_app.DEFAULT_PRODUCT_PORT,
            )

    def test_choose_local_port_reuses_previous_when_default_is_busy(self):
        with patch.object(
            product_app,
            "local_port_is_available",
            side_effect=lambda port: port == 32123,
        ):
            self.assertEqual(
                product_app.choose_local_port(previous_port=32123),
                32123,
            )

    def test_release_environment_does_not_set_debug_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            product_app.configure_environment(Path(temp_dir), 32123)

            self.assertNotIn("BILI_REVIEW_HARD_LIMIT", os.environ)

    def test_existing_instance_can_be_checked_without_opening_browser(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            product_app.write_runtime(root, 32123)
            with (
                patch.dict(
                    os.environ,
                    {"BILI_DISABLE_BROWSER": "1"},
                    clear=False,
                ),
                patch.object(product_app, "wait_until_ready", return_value=True),
                patch.object(product_app.webbrowser, "open") as open_browser,
            ):
                self.assertTrue(product_app.open_existing_instance(root))
                open_browser.assert_not_called()

    def test_product_ui_uses_automatic_identity_and_confirmed_defaults(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('value="500" selected>最近 500 条（默认）', template)
        self.assertIn('id="cfg-bilibili-check_interval" value="600"', template)
        self.assertIn(
            'id="cfg-rate_limit-min_request_interval" value="10"',
            template,
        )
        self.assertIn('id="cfg-rate_limit-retry_delay" value="20"', template)
        self.assertNotIn('id="cfg-bilibili-uid"', template)
        self.assertIn("review_time_range: reviewTimeRange", template)
        self.assertIn("function reviewDraftsUrl()", template)
        self.assertIn("fetch(reviewDraftsUrl())", template)
        self.assertIn("当前时间范围", template)
        self.assertIn("...reviewPreferencesPayload()", template)
        self.assertIn("连续 3 页没有新增待生成评论", template)
        self.assertIn("--log-bg: #f7f8fa", template)
        self.assertIn("fetch('/api/logs/clear'", template)
        self.assertIn("关闭后会保留你当前查看的位置", template)
        self.assertIn(".sidebar {", template)
        self.assertIn("position:sticky", template)
        self.assertIn(".sidebar-tools .form-label { display:none; }", template)
        self.assertNotIn(".sidebar { width: 60px; }", template)
        save_config_body = template.split(
            "function saveConfig()", 1
        )[1].split("function clearConfigSecret", 1)[0]
        self.assertNotIn(
            "max_process: normalizeReviewLimit",
            save_config_body,
        )
        self.assertNotIn(
            "review_since: get('cfg-reply-review_since')",
            save_config_body,
        )

    def test_add_account_starts_qr_login_without_manual_name(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        create_account_body = template.split(
            "async function createAccount()", 1
        )[1].split("function importAccount()", 1)[0]

        self.assertNotIn("prompt(", create_account_body)
        self.assertIn("body: JSON.stringify({})", create_account_body)
        self.assertIn("await loadAccounts()", create_account_body)
        self.assertIn("navigateTo('login')", create_account_body)
        self.assertIn("await generateQR(true)", create_account_body)
        self.assertIn("B站扫码登录", template)
        self.assertNotIn("微信扫码登录 B 站", template)
        self.assertIn("d.code < 0 || d.code === 86038", template)
        self.assertNotIn('data-tab="tab-auth"', template)
        self.assertNotIn('id="cfg-auth-enabled"', template)


if __name__ == "__main__":
    unittest.main()
