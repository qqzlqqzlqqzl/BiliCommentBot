import unittest
from unittest.mock import Mock, patch

import server


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
        self.fake_bot.regenerate_review_draft.return_value = {
            "comment_id": "1",
            "approved": False,
            "status": "pending",
            "reply": "新回复",
        }
        self.bot_patch = patch.object(server, "get_bot", return_value=self.fake_bot)
        self.bot_patch.start()

    def tearDown(self):
        self.bot_patch.stop()

    def test_send_requires_explicit_nonempty_comment_ids(self):
        missing = self.client.post("/api/review/send", json={})
        empty = self.client.post("/api/review/send", json={"comment_ids": []})

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(empty.status_code, 400)
        self.fake_bot.send_approved_drafts.assert_not_called()

    def test_send_passes_only_explicit_ids(self):
        response = self.client.post("/api/review/send", json={"comment_ids": ["1"]})

        self.assertEqual(response.status_code, 200)
        self.fake_bot.send_approved_drafts.assert_called_once_with(comment_ids=["1"])

    def test_approve_requires_real_boolean(self):
        response = self.client.post(
            "/api/review/approve",
            json={"comment_id": "1", "approved": "false"},
        )

        self.assertEqual(response.status_code, 400)
        self.fake_bot.set_review_approval.assert_not_called()

    def test_regenerate_requires_comment_id(self):
        response = self.client.post("/api/review/regenerate", json={})

        self.assertEqual(response.status_code, 400)
        self.fake_bot.regenerate_review_draft.assert_not_called()

    def test_regenerate_passes_single_comment_id(self):
        response = self.client.post("/api/review/regenerate", json={"comment_id": "1"})

        self.assertEqual(response.status_code, 200)
        self.fake_bot.regenerate_review_draft.assert_called_once_with("1")


if __name__ == "__main__":
    unittest.main()
