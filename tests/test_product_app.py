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
                self.assertEqual(os.environ["BILI_AUTO_START_MONITOR"], "0")
                self.assertEqual(os.environ["BILI_OPEN_BROWSER"], "0")
                self.assertNotIn("BILI_REVIEW_HARD_LIMIT", os.environ)

    def test_health_url_is_local_only(self):
        self.assertEqual(
            product_app.health_url(32123),
            "http://127.0.0.1:32123/api/health",
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
        self.assertNotIn('data-tab="tab-auth"', template)
        self.assertNotIn('id="cfg-auth-enabled"', template)


if __name__ == "__main__":
    unittest.main()
