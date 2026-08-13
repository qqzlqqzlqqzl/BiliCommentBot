import copy
import json
import logging
import os
import tempfile
import threading
import time
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

    def test_regenerate_prompt_contains_previous_reply_and_requires_difference(self):
        comment = bot_module.Comment(
            comment_id="1",
            content="测试评论",
            user="观众",
            uid="42",
            time=123,
        )
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = {
            "output": [{
                "content": [{
                    "type": "output_text",
                    "text": '[{"id":"1","should_reply":true,"reply":"全新的说法","reason":"已重写"}]',
                }],
            }],
        }
        with patch.object(bot_module.requests, "post", return_value=response) as post:
            self.bot.generate_reply_decisions([{
                "comment": comment,
                "regenerate": True,
                "previous_reply": "原来的说法",
                "avoid_replies": ["原来的说法"],
            }])

        prompt = post.call_args.kwargs["json"]["input"][0]["content"][0]["text"]
        self.assertIn('"previous_reply": "原来的说法"', prompt)
        self.assertIn("不能只替换标点", prompt)

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

    def test_busy_message_identifies_bilibili_read_owner(self):
        message = bot_module._review_generation_busy_message({
            "active": True,
            "account": "账号2",
            "port": "5001",
            "operation": "读取 B站视频评论",
        })

        self.assertEqual(
            message,
            "账号2（端口 5001）正在读取 B站视频评论，请完成后再试",
        )

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
        self.bot.generate_distinct_reply_decision = Mock(return_value={
            "id": "1",
            "should_reply": True,
            "reply": "豆包重新生成的原文",
            "reason": "有新的互动价值",
            "model": "doubao-new",
        })

        result = self.bot.regenerate_review_draft("1")

        self.assertEqual(result["reply"], "豆包重新生成的原文")
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["approved"])
        item = self.bot.generate_distinct_reply_decision.call_args.args[0]
        self.assertTrue(item["is_follow_up"])
        self.assertEqual(item["context"][0].content, "之前的回复")
        self.assertEqual(item["previous_reply"], "豆包原文")

    def test_regenerate_retries_when_doubao_returns_same_reply(self):
        item = {
            "comment": bot_module.Comment(
                comment_id="1",
                content="测试评论",
                user="观众",
                uid="42",
                time=123,
            ),
            "previous_reply": "原来的回复！",
        }
        self.bot.generate_reply_decisions_resilient = Mock(side_effect=[
            [{
                "id": "1",
                "should_reply": True,
                "reply": "原来的回复。",
                "reason": "第一次",
                "model": "doubao",
            }],
            [{
                "id": "1",
                "should_reply": True,
                "reply": "换一个完全不同的角度",
                "reason": "第二次",
                "model": "doubao",
            }],
        ])

        result = self.bot.generate_distinct_reply_decision(item)

        self.assertEqual(result["reply"], "换一个完全不同的角度")
        self.assertEqual(self.bot.generate_reply_decisions_resilient.call_count, 2)
        second_item = self.bot.generate_reply_decisions_resilient.call_args_list[1].args[0][0]
        self.assertIn("原来的回复。", second_item["avoid_replies"])

    def test_sent_draft_cannot_be_regenerated(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=False, status="sent")

        with self.assertRaisesRegex(ValueError, "已发送"):
            self.bot.regenerate_review_draft("1")

    def test_doubao_batches_run_concurrently_after_bilibili_read(self):
        def item(comment_id):
            return {
                "bvid": "BV1test",
                "oid": "100",
                "comment_type": 1,
                "video_title": "测试视频",
                "comment": bot_module.Comment(
                    comment_id=str(comment_id),
                    content=f"评论{comment_id}",
                    user="观众",
                    uid="42",
                    time=123,
                ),
                "parent_comment": None,
            }

        self.bot.config["reply"]["review_batch_size"] = 1
        barrier = threading.Barrier(2)

        def fake_generate(batch):
            barrier.wait(timeout=1)
            time.sleep(0.02)
            comment_id = str(batch[0]["comment"].comment_id)
            return [{
                "id": comment_id,
                "should_reply": True,
                "reply": f"回复{comment_id}",
                "reason": "可回复",
                "model": "doubao",
            }]

        self.bot.generate_reply_decisions_resilient = fake_generate
        result = self.bot._generate_review_drafts([item(1), item(2)])

        self.assertEqual(result["generated"], 2)
        self.assertEqual(result["replyable"], 2)

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

    def test_collect_review_items_uses_original_video_comment_chain(self):
        self.bot.config["reply"]["only_bvid"] = ""
        self.bot.config["reply"]["context_comments_count"] = 0
        self.bot.config["bilibili"]["uid"] = "999"
        self.bot.get_video_list = Mock(return_value=[{
            "bvid": "BV1test",
            "title": "测试视频",
            "desc": "视频简介",
        }])
        root = bot_module.Comment(
            comment_id="100",
            content="主评论",
            user="观众甲",
            uid="1",
            time=100,
        )
        child = bot_module.Comment(
            comment_id="101",
            content="楼中楼",
            user="观众乙",
            uid="2",
            time=101,
            parent_id="100",
            root_id="100",
            depth=1,
        )
        self.bot.get_video_comments = Mock(return_value=[root, child])

        items = self.bot._collect_review_items(limit=20)

        self.bot.get_video_list.assert_called_once_with()
        self.bot.get_video_comments.assert_called_once_with("BV1test")
        self.assertEqual([item["comment"].comment_id for item in items], ["100", "101"])
        self.assertEqual(items[1]["parent_comment"].comment_id, "100")
        self.assertTrue(items[1]["is_follow_up"])


if __name__ == "__main__":
    unittest.main()
