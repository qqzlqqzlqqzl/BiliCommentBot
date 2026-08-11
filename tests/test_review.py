import copy
import json
import logging
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import bot as bot_module
from bot import BiliCommentBot, DEFAULT_CONFIG


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drafts_file = os.path.join(self.temp_dir.name, "review_drafts.json")
        self.file_patch = patch.object(bot_module, "REVIEW_DRAFTS_FILE", self.drafts_file)
        self.file_patch.start()

        self.bot = BiliCommentBot.__new__(BiliCommentBot)
        self.bot.config = copy.deepcopy(DEFAULT_CONFIG)
        self.bot.logger = logging.getLogger("review-tests")
        self.bot._review_lock = threading.Lock()
        self.bot._review_drafts = {}
        self.bot.processed_comments = set()
        self.bot.stats = {"total_replied": 0}
        self.bot.socketio = None
        self.bot.save_history = Mock()
        self.bot.reply_comment = Mock(return_value=True)

    def tearDown(self):
        self.file_patch.stop()
        self.temp_dir.cleanup()

    def _draft(self, comment_id, approved=False, status="pending"):
        return {
            "comment_id": str(comment_id),
            "bvid": "BV1test",
            "video_title": "测试视频",
            "author": "观众",
            "author_uid": "42",
            "comment": "原评论",
            "comment_time": 123456,
            "parent_id": None,
            "root_id": None,
            "depth": 0,
            "should_reply": True,
            "reply": "豆包原文",
            "reason": "语境清楚",
            "approved": approved,
            "status": status,
        }

    def test_parses_ark_output_text_and_fenced_json(self):
        payload = {
            "output": [{
                "content": [
                    {"type": "output_text", "text": "```json\n"},
                    {"type": "output_text", "text": '[{"id":"1","should_reply":true}]'},
                    {"type": "output_text", "text": "\n```"},
                ]
            }]
        }
        text = self.bot._ark_output_text(payload)
        parsed = self.bot._parse_json_text(text)
        self.assertEqual(parsed[0]["id"], "1")
        self.assertTrue(parsed[0]["should_reply"])

    def test_doubao_prompt_marks_follow_up_and_requires_new_value(self):
        comment = bot_module.Comment(
            comment_id="1",
            content="谢谢哈哈",
            user="观众",
            uid="42",
            time=123,
            root_id="99",
            depth=1,
        )
        parent = bot_module.Comment(
            comment_id="99",
            content="这是之前的回复",
            user="上级评论",
            uid="",
            time=100,
        )
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = {
            "output": [{
                "content": [{
                    "type": "output_text",
                    "text": '[{"id":"1","should_reply":false,"reply":"","reason":"无新内容"}]',
                }],
            }],
        }
        with patch.object(bot_module.requests, "post", return_value=response) as post:
            result = self.bot.generate_reply_decisions([{
                "comment": comment,
                "context": [parent],
                "video_title": "测试视频",
                "is_follow_up": True,
            }])

        payload = post.call_args.kwargs["json"]
        prompt = payload["input"][0]["content"][0]["text"]
        self.assertIn('"is_follow_up": true', prompt)
        self.assertIn("纯“谢谢/收到/哈哈”", prompt)
        self.assertFalse(result[0]["should_reply"])

    def test_bad_doubao_json_is_split_serially_without_losing_whole_batch(self):
        def item(comment_id):
            return {
                "comment": bot_module.Comment(
                    comment_id=str(comment_id),
                    content=f"评论{comment_id}",
                    user="观众",
                    uid="42",
                    time=123,
                ),
            }

        calls = []

        def fake_generate(items):
            calls.append([entry["comment"].comment_id for entry in items])
            if len(items) > 1:
                raise json.JSONDecodeError("bad json", "[", 1)
            comment_id = str(items[0]["comment"].comment_id)
            return [{
                "id": comment_id,
                "should_reply": True,
                "reply": f"回复{comment_id}",
                "reason": "可回复",
                "model": "doubao",
            }]

        self.bot.generate_reply_decisions = fake_generate
        result = self.bot.generate_reply_decisions_resilient([item(1), item(2)])

        self.assertEqual(calls, [["1", "2"], ["1"], ["2"]])
        self.assertEqual([entry["reply"] for entry in result], ["回复1", "回复2"])

    def test_approval_is_persisted_without_changing_reply(self):
        self.bot._review_drafts["1"] = self._draft("1")
        original_reply = self.bot._review_drafts["1"]["reply"]

        result = self.bot.set_review_approval("1", True)

        self.assertTrue(result["approved"])
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["reply"], original_reply)
        with open(self.drafts_file, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved[0]["reply"], original_reply)

    def test_regenerate_uses_current_prompt_result_and_clears_approval(self):
        draft = self._draft("1", approved=True, status="approved")
        draft["parent_comment"] = "之前的回复"
        draft["parent_author"] = "上级评论"
        draft["root_id"] = "99"
        draft["depth"] = 1
        self.bot._review_drafts["1"] = draft
        self.bot.generate_reply_decisions_resilient = Mock(return_value=[{
            "id": "1",
            "should_reply": True,
            "reply": "豆包重新生成的原文",
            "reason": "有新的互动价值",
            "model": "doubao-new",
        }])

        result = self.bot.regenerate_review_draft("1")

        self.assertEqual(result["reply"], "豆包重新生成的原文")
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["approved"])
        item = self.bot.generate_reply_decisions_resilient.call_args.args[0][0]
        self.assertTrue(item["is_follow_up"])
        self.assertEqual(item["context"][0].content, "之前的回复")

    def test_sent_draft_cannot_be_regenerated(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=False, status="sent")

        with self.assertRaisesRegex(ValueError, "已发送"):
            self.bot.regenerate_review_draft("1")

    def test_empty_requested_list_sends_nothing(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=True, status="approved")

        result = self.bot.send_approved_drafts(comment_ids=[])

        self.assertEqual(result, {"sent": 0, "failed": 0})
        self.bot.reply_comment.assert_not_called()

    def test_only_explicitly_approved_requested_draft_is_sent(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=True, status="approved")
        self.bot._review_drafts["2"] = self._draft("2", approved=False, status="pending")
        self.bot._review_drafts["3"] = self._draft("3", approved=True, status="approved")

        result = self.bot.send_approved_drafts(comment_ids=["1", "2"])

        self.assertEqual(result, {"sent": 1, "failed": 0})
        self.bot.reply_comment.assert_called_once_with(
            "BV1test",
            "1",
            "豆包原文",
            root_id=None,
            parent_id=None,
            oid=None,
            comment_type=1,
        )
        self.assertEqual(self.bot._review_drafts["1"]["status"], "sent")
        self.assertEqual(self.bot._review_drafts["3"]["status"], "approved")

    def test_account_reply_feed_uses_recent_notification_pages(self):
        self.bot.config["bilibili"]["max_comment_pages"] = 2
        self.bot.config["reply"]["only_bvid"] = ""
        self.bot.make_request_with_retry = Mock()
        first_page = {
            "code": 0,
            "data": {
                "cursor": {"is_end": False, "id": 900, "time": 120},
                "items": [{
                        "reply_time": 123,
                        "user": {"nickname": "观众甲", "mid": 99},
                        "item": {
                            "source_id": 1001,
                            "subject_id": 2001,
                            "root_id": 0,
                            "business_id": 1,
                            "source_content": "测试评论",
                            "title": "测试标题",
                            "uri": "https://www.bilibili.com/video/BV1abc123",
                        },
                }],
            },
        }
        self.bot.make_request_with_retry.side_effect = [
            Mock(json=Mock(return_value=first_page)),
        ]

        items = self.bot.get_account_reply_feed(limit=1)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["comment"].comment_id, "1001")
        self.assertEqual(items[0]["comment"].content, "测试评论")
        self.assertEqual(items[0]["oid"], "2001")
        self.assertEqual(items[0]["bvid"], "BV1abc123")
        self.bot.make_request_with_retry.assert_called_once_with(
            "GET",
            "https://api.bilibili.com/x/msgfeed/reply",
            params={"ps": 10, "platform": "web"},
            use_cache=False,
        )

    def test_account_reply_feed_uses_cursor_for_second_page(self):
        self.bot.config["bilibili"]["max_comment_pages"] = 2
        self.bot.config["reply"]["only_bvid"] = ""
        first = {
            "code": 0,
            "data": {
                "cursor": {"is_end": False, "id": 900, "time": 120},
                "items": [
                    {
                        "reply_time": 123,
                        "user": {"nickname": "甲", "mid": 1},
                        "item": {
                            "source_id": 1001 + index,
                            "subject_id": 2001,
                            "root_id": 0,
                            "business_id": 1,
                            "source_content": "第一页",
                            "title": "标题一",
                            "uri": "https://www.bilibili.com/video/BV1pageone",
                        },
                    }
                    for index in range(10)
                ],
            },
        }
        second = {
            "code": 0,
            "data": {
                "cursor": {"is_end": True, "id": 800, "time": 100},
                "items": [{
                    "reply_time": 100,
                    "user": {"nickname": "乙", "mid": 2},
                    "item": {
                        "source_id": 2001,
                        "subject_id": 3001,
                        "root_id": 0,
                        "business_id": 1,
                        "source_content": "第二页",
                        "title": "标题二",
                        "uri": "https://www.bilibili.com/video/BV1pagetwo",
                    },
                }],
            },
        }
        self.bot.make_request_with_retry = Mock(side_effect=[
            Mock(json=Mock(return_value=first)),
            Mock(json=Mock(return_value=second)),
        ])

        items = self.bot.get_account_reply_feed(limit=20)

        self.assertEqual(len(items), 11)
        self.assertEqual(items[-1]["comment"].content, "第二页")
        self.assertEqual(
            self.bot.make_request_with_retry.call_args_list[1].kwargs["params"],
            {"ps": 10, "platform": "web", "id": 900, "reply_time": 120},
        )


if __name__ == "__main__":
    unittest.main()
