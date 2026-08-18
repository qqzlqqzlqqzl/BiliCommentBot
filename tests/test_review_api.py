import unittest
from unittest.mock import Mock, patch

import server
from bot import ReviewOperationBusyError


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.fake_bot = Mock()
        self.fake_bot.send_approved_drafts.return_value = {"sent": 1, "failed": 0}
        self.fake_bot.set_review_approval.return_value = {
            "comment_id": "1",
            "approved": True,
            "status": "approved",
        }
        self.fake_bot.set_review_approvals.return_value = {
            "updated": 2,
            "approved": True,
        }
        self.fake_bot.regenerate_review_draft.return_value = {
            "comment_id": "1",
            "approved": False,
            "status": "pending",
            "reply": "新回复",
        }
        self.fake_bot.generate_review_drafts.return_value = {
            "generated": 2,
            "replyable": 1,
            "skipped": 1,
        }
        self.fake_bot.get_review_operation_status.return_value = {
            "active": {"generating": 0, "sending": 0}
        }
        self.fake_bot.reload_config.return_value = True
        self.fake_bot.start.return_value = True
        self.fake_bot.stop.return_value = True
        self.bot_patch = patch.object(server, "get_bot", return_value=self.fake_bot)
        self.bot_patch.start()
        self.config_patch = patch.object(
            server,
            "load_config",
            return_value={
                "reply": {"review_time_range": "", "review_since": ""},
                "bilibili": {},
            },
        )
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.bot_patch.stop()

    def test_draft_list_filters_existing_drafts_by_relative_time_range(self):
        self.fake_bot.get_review_drafts.return_value = [
            {"comment_id": "new", "comment_time": 1_999_999},
            {"comment_id": "old", "comment_time": 1_900_000},
        ]

        with patch.object(server.time, "time", return_value=2_000_000):
            response = self.client.get(
                "/api/review/drafts?review_time_range=24h"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["total_all"], 2)
        self.assertEqual(
            payload["since_timestamp"],
            2_000_000 - 24 * 60 * 60,
        )
        self.assertEqual(
            [draft["comment_id"] for draft in payload["drafts"]],
            ["new"],
        )

    def test_draft_list_rejects_unknown_time_range(self):
        self.fake_bot.get_review_drafts.return_value = []

        response = self.client.get(
            "/api/review/drafts?review_time_range=yesterday-ish"
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_send_requires_explicit_nonempty_comment_ids(self):
        missing = self.client.post("/api/review/send", json={})
        empty = self.client.post("/api/review/send", json={"comment_ids": []})

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(empty.status_code, 400)
        self.fake_bot.send_approved_drafts.assert_not_called()

    def test_send_passes_only_explicit_ids(self):
        response = self.client.post("/api/review/send", json={"comment_ids": ["1"]})

        self.assertEqual(response.status_code, 200)
        self.fake_bot.send_approved_drafts.assert_called_once_with(
            comment_ids=["1"],
            since_timestamp=None,
        )

    def test_send_enforces_stricter_submitted_time_range(self):
        with patch.object(server.time, "time", return_value=2_000_000):
            response = self.client.post(
                "/api/review/send",
                json={
                    "comment_ids": ["1"],
                    "review_time_range": "24h",
                    "review_since": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.fake_bot.send_approved_drafts.assert_called_once_with(
            comment_ids=["1"],
            since_timestamp=2_000_000 - 24 * 60 * 60,
        )

    def test_send_cannot_bypass_saved_time_range_with_empty_request_range(self):
        self.config_patch.stop()
        with (
            patch.object(
                server,
                "load_config",
                return_value={
                    "reply": {
                        "review_time_range": "24h",
                        "review_since": "",
                    }
                },
            ),
            patch.object(server.time, "time", return_value=2_000_000),
        ):
            response = self.client.post(
                "/api/review/send",
                json={
                    "comment_ids": ["1"],
                    "review_time_range": "",
                    "review_since": "",
                },
            )
        self.config_patch.start()

        self.assertEqual(response.status_code, 200)
        self.fake_bot.send_approved_drafts.assert_called_once_with(
            comment_ids=["1"],
            since_timestamp=2_000_000 - 24 * 60 * 60,
        )

    def test_approve_requires_real_boolean(self):
        response = self.client.post(
            "/api/review/approve",
            json={"comment_id": "1", "approved": "false"},
        )

        self.assertEqual(response.status_code, 400)
        self.fake_bot.set_review_approval.assert_not_called()

    def test_bulk_approve_requires_explicit_ids_and_real_boolean(self):
        missing_ids = self.client.post(
            "/api/review/approve-bulk",
            json={"approved": True},
        )
        invalid_approved = self.client.post(
            "/api/review/approve-bulk",
            json={"comment_ids": ["1"], "approved": "true"},
        )

        self.assertEqual(missing_ids.status_code, 400)
        self.assertEqual(invalid_approved.status_code, 400)
        self.fake_bot.set_review_approvals.assert_not_called()

    def test_bulk_approve_passes_only_explicit_ids(self):
        response = self.client.post(
            "/api/review/approve-bulk",
            json={"comment_ids": ["1", "2"], "approved": True},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["updated"], 2)
        self.fake_bot.set_review_approvals.assert_called_once_with(
            ["1", "2"],
            True,
        )

    def test_regenerate_requires_comment_id(self):
        response = self.client.post("/api/review/regenerate", json={})

        self.assertEqual(response.status_code, 400)
        self.fake_bot.regenerate_review_draft.assert_not_called()

    def test_regenerate_passes_single_comment_id(self):
        response = self.client.post("/api/review/regenerate", json={"comment_id": "1"})

        self.assertEqual(response.status_code, 200)
        self.fake_bot.regenerate_review_draft.assert_called_once_with("1")

    def test_generate_passes_count_and_time_boundaries(self):
        response = self.client.post(
            "/api/review/generate",
            json={
                "limit": 50,
                "review_since": "2026-08-10T00:00",
                "review_time_range": "custom",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.fake_bot.generate_review_drafts.assert_called_once_with(
            limit=50,
            review_since="2026-08-10T00:00",
            review_time_range="custom",
        )

    def test_generate_passes_relative_time_range_without_browser_timestamp(self):
        response = self.client.post(
            "/api/review/generate",
            json={
                "limit": 100,
                "review_since": "",
                "review_time_range": "24h",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.fake_bot.generate_review_drafts.assert_called_once_with(
            limit=100,
            review_since="",
            review_time_range="24h",
        )

    def test_generate_rejects_unknown_time_range(self):
        response = self.client.post(
            "/api/review/generate",
            json={"limit": 100, "review_time_range": "yesterday-ish"},
        )

        self.assertEqual(response.status_code, 400)
        self.fake_bot.generate_review_drafts.assert_not_called()

    def test_monitor_start_persists_restart_preference(self):
        config = {"bilibili": {"auto_start_monitor": False}}
        with (
            patch.object(server, "load_config", return_value=config),
            patch.object(server, "save_config", return_value=True) as save,
        ):
            response = self.client.post("/api/bot/start")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(config["bilibili"]["auto_start_monitor"])
        save.assert_called_once_with(config)
        self.fake_bot.reload_config.assert_called_once_with(config)
        self.fake_bot.start.assert_called_once_with()

    def test_monitor_stop_persists_restart_preference_even_if_already_stopped(self):
        config = {"bilibili": {"auto_start_monitor": True}}
        self.fake_bot.stop.return_value = False
        with (
            patch.object(server, "load_config", return_value=config),
            patch.object(server, "save_config", return_value=True) as save,
        ):
            response = self.client.post("/api/bot/stop")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(config["bilibili"]["auto_start_monitor"])
        save.assert_called_once_with(config)
        self.assertIn("保持停止", response.get_json()["message"])

    def test_review_preferences_persist_relative_range_for_monitor(self):
        config = {
            "reply": {
                "max_process": 500,
                "review_time_range": "",
                "review_since": "",
            }
        }
        with (
            patch.object(server, "load_config", return_value=config),
            patch.object(server, "save_config", return_value=True) as save,
        ):
            response = self.client.post(
                "/api/review/preferences",
                json={
                    "limit": 100,
                    "review_time_range": "24h",
                    "review_since": "不应保存的旧时间",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(config["reply"]["max_process"], 100)
        self.assertEqual(config["reply"]["review_time_range"], "24h")
        self.assertEqual(config["reply"]["review_since"], "")
        save.assert_called_once_with(config)

    def test_generate_defaults_to_five_hundred(self):
        response = self.client.post("/api/review/generate", json={})

        self.assertEqual(response.status_code, 200)
        self.fake_bot.generate_review_drafts.assert_called_once_with(
            limit=500,
            review_since=None,
            review_time_range=None,
        )

    def test_generate_respects_temporary_debug_cap(self):
        with patch.dict("os.environ", {"BILI_REVIEW_HARD_LIMIT": "110"}):
            response = self.client.post("/api/review/generate", json={"limit": 50000})

        self.assertEqual(response.status_code, 200)
        self.fake_bot.generate_review_drafts.assert_called_once_with(
            limit=110,
            review_since=None,
            review_time_range=None,
        )

    def test_duplicate_generate_returns_conflict(self):
        self.fake_bot.generate_review_drafts.side_effect = ReviewOperationBusyError(
            "已有生成任务正在执行"
        )

        response = self.client.post("/api/review/generate", json={"limit": 200})

        self.assertEqual(response.status_code, 409)
        self.assertIn("已有生成任务", response.get_json()["message"])

    def test_duplicate_send_returns_conflict(self):
        self.fake_bot.send_approved_drafts.side_effect = ReviewOperationBusyError(
            "已有发送任务正在执行"
        )

        response = self.client.post(
            "/api/review/send",
            json={"comment_ids": ["1"]},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("已有发送任务", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
