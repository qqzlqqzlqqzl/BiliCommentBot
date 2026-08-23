import copy
import json
import logging
import os
import random
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

import bot as bot_module
from bot import (
    AutoSendBlockedError,
    BiliCommentBot,
    DEFAULT_CONFIG,
    ReplyAttemptResult,
    ReviewOperationBusyError,
)


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
        self.bot.stats = {
            "total_replied": 0,
            "start_time": None,
            "last_check": None,
        }
        self.bot.cached_videos = {}
        self.bot._running = False
        self.bot.socketio = None
        self.bot.save_history = Mock()
        self.bot._flush_history = Mock()
        self.bot.reply_comment = Mock(return_value=True)
        self.bot._wait_for_ark_slot = Mock()

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

    @staticmethod
    def _creator_row(
        comment_id,
        *,
        uid="100",
        message="观众评论",
        ctime=None,
        parent=0,
        root=0,
        replied=False,
    ):
        numeric_id = int(comment_id)
        return {
            "rpid": numeric_id,
            "oid": numeric_id + 1000,
            "type": 1,
            "root": root,
            "parent": parent,
            "bvid": f"BV{numeric_id}",
            "title": f"视频{numeric_id}",
            "ctime": int(ctime if ctime is not None else numeric_id),
            "member": {"mid": int(uid), "uname": f"用户{uid}"},
            "content": {"message": message},
            "up_action": {"reply": replied},
        }

    @staticmethod
    def _creator_page(rows, *, page_number=1, total=10000):
        return bot_module.CachedResponse({
            "code": 0,
            "message": "OK",
            "data": {
                "page": {
                    "num": page_number,
                    "size": 10,
                    "total": total,
                },
                "list": rows,
            },
        })

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
        custom_prompt = "这是用户保存的自定义提示词：禁止使用哈哈。"
        self.bot.config["ark"]["system_prompt"] = custom_prompt
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
        response.status_code = 200
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
        self.assertEqual(
            payload["instructions"],
            custom_prompt,
        )
        self.assertNotIn(custom_prompt, prompt)
        self.assertIn('"is_follow_up": true', prompt)
        self.assertIn("纯“谢谢/收到/哈哈”", prompt)
        self.assertIn("风格硬约束", prompt)
        self.assertIn("是不是AI", prompt)
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
        response.status_code = 200
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
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["max_output_tokens"], 128000)
        self.assertEqual(payload["reasoning"], {"effort": "medium"})

    def test_doubao_429_is_retried_without_aborting_batch(self):
        comment = bot_module.Comment(
            comment_id="1",
            content="测试评论",
            user="观众",
            uid="42",
            time=123,
        )
        limited = Mock()
        limited.status_code = 429
        limited.headers = {"Retry-After": "0"}
        limited.text = '{"error":{"code":"rate_limit"}}'

        success = Mock()
        success.status_code = 200
        success.raise_for_status = Mock()
        success.json.return_value = {
            "output": [{
                "content": [{
                    "type": "output_text",
                    "text": '[{"id":"1","should_reply":true,"reply":"收到","reason":"可回复"}]',
                }],
            }],
        }

        with (
            patch.object(bot_module.requests, "post", side_effect=[limited, success]) as post,
            patch.object(bot_module.time, "sleep"),
        ):
            result = self.bot.generate_reply_decisions([{"comment": comment}])

        self.assertEqual(post.call_count, 2)
        self.assertEqual(result[0]["reply"], "收到")

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

    def test_empty_doubao_output_is_split_and_single_failure_is_skipped(self):
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
            comment_ids = [entry["comment"].comment_id for entry in items]
            calls.append(comment_ids)
            if len(items) > 1 or comment_ids == ["1"]:
                raise bot_module.ArkEmptyOutputError("豆包没有返回文本")
            return [{
                "id": "2",
                "should_reply": True,
                "reply": "回复2",
                "reason": "可回复",
                "model": "doubao",
            }]

        self.bot.generate_reply_decisions = fake_generate
        result = self.bot.generate_reply_decisions_resilient([item(1), item(2)])

        self.assertEqual(calls, [["1", "2"], ["1"], ["2"]])
        self.assertFalse(result[0]["should_reply"])
        self.assertEqual(result[0]["reason"], "豆包未返回文本，暂不回复")
        self.assertEqual(result[1]["reply"], "回复2")

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

    def test_manual_dismissal_is_persisted_and_kept_for_scan_deduplication(self):
        self.bot._review_drafts["1"] = self._draft("1")
        self.bot.get_creator_comment_feed = Mock(return_value=[])

        dismissed = self.bot.set_review_dismissed("1", True)
        second_items = self.bot._collect_review_items(limit=100)

        self.assertEqual(dismissed["status"], "dismissed")
        self.assertFalse(dismissed["approved"])
        self.assertIn("dismissed_at", dismissed)
        self.assertEqual(second_items, [])
        skip_ids = self.bot.get_creator_comment_feed.call_args.kwargs[
            "skip_comment_ids"
        ]
        self.assertIn("1", skip_ids)
        with open(self.drafts_file, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved[0]["status"], "dismissed")

    def test_manual_dismissal_can_be_restored_without_regenerating(self):
        draft = self._draft("1", approved=True, status="approved")
        original_reply = draft["reply"]
        self.bot._review_drafts["1"] = draft

        self.bot.set_review_dismissed("1", True)
        restored = self.bot.set_review_dismissed("1", False)

        self.assertEqual(restored["status"], "pending")
        self.assertFalse(restored["approved"])
        self.assertEqual(restored["reply"], original_reply)
        self.assertNotIn("dismissed_at", restored)

    def test_sent_or_in_flight_draft_cannot_be_manually_dismissed(self):
        for status in ("sent", "sending", "regenerating", "send_unknown"):
            with self.subTest(status=status):
                self.bot._review_drafts["1"] = self._draft("1", status=status)
                with self.assertRaises(ValueError):
                    self.bot.set_review_dismissed("1", True)

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

    def test_bulk_approval_updates_replyable_drafts_in_one_save(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=False, status="pending")
        self.bot._review_drafts["2"] = self._draft("2", approved=False, status="failed")

        with patch.object(self.bot, "_save_review_drafts") as save:
            selected = self.bot.set_review_approvals(["1", "2"], True)

        self.assertEqual(selected, {"updated": 2, "approved": True})
        self.assertEqual(self.bot._review_drafts["1"]["status"], "approved")
        self.assertEqual(self.bot._review_drafts["2"]["status"], "approved")
        save.assert_called_once()

        with patch.object(self.bot, "_save_review_drafts") as save:
            cleared = self.bot.set_review_approvals(["1", "2"], False)

        self.assertEqual(cleared, {"updated": 2, "approved": False})
        self.assertEqual(self.bot._review_drafts["1"]["status"], "pending")
        self.assertEqual(self.bot._review_drafts["2"]["status"], "pending")
        save.assert_called_once()

    def test_bulk_approval_is_atomic_when_any_draft_is_not_replyable(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=False, status="pending")
        self.bot._review_drafts["2"] = self._draft("2", approved=False, status="skipped")
        self.bot._review_drafts["2"]["should_reply"] = False
        self.bot._review_drafts["2"]["reply"] = ""

        with self.assertRaisesRegex(ValueError, "没有可发送"):
            self.bot.set_review_approvals(["1", "2"], True)

        self.assertFalse(self.bot._review_drafts["1"]["approved"])
        self.assertEqual(self.bot._review_drafts["1"]["status"], "pending")

    def test_bulk_approval_is_blocked_during_active_review_operation(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=False, status="pending")

        with self.bot._review_operation("sending"):
            with self.assertRaisesRegex(ReviewOperationBusyError, "暂时不能批量"):
                self.bot.set_review_approvals(["1"], True)

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
        with self.assertLogs("review-tests", level="INFO") as captured:
            result = self.bot._generate_review_drafts([item(1), item(2)])

        self.assertEqual(result["generated"], 2)
        self.assertEqual(result["replyable"], 2)
        log_text = "\n".join(captured.output)
        self.assertIn("开始豆包生成：共2条", log_text)
        self.assertIn("豆包生成进度：已处理", log_text)
        self.assertIn("豆包生成完成：新增2条草稿", log_text)

    def test_review_read_limit_uses_temporary_debug_cap(self):
        self.bot.auto_refresh_cookie = False
        self.bot._collect_review_items = Mock(return_value=[])

        with patch.dict(os.environ, {"BILI_REVIEW_HARD_LIMIT": "110"}):
            result = self.bot.generate_review_drafts(limit=99999)

        self.assertEqual(result, {"generated": 0, "replyable": 0, "skipped": 0})
        self.assertEqual(self.bot._collect_review_items.call_args.args[0], 110)

    def test_review_read_limit_keeps_full_product_range(self):
        self.bot.auto_refresh_cookie = False
        self.bot._collect_review_items = Mock(return_value=[])

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BILI_REVIEW_HARD_LIMIT", None)
            result = self.bot.generate_review_drafts(limit=50000)

        self.assertEqual(result, {"generated": 0, "replyable": 0, "skipped": 0})
        self.assertEqual(self.bot._collect_review_items.call_args.args[0], 50000)

    def test_review_read_limit_defaults_to_five_hundred(self):
        self.bot.auto_refresh_cookie = False
        self.bot.config["reply"]["max_process"] = 500
        self.bot._collect_review_items = Mock(return_value=[])

        result = self.bot.generate_review_drafts()

        self.assertEqual(result, {"generated": 0, "replyable": 0, "skipped": 0})
        self.assertEqual(self.bot._collect_review_items.call_args.args[0], 500)

    def test_default_product_parameters_match_confirmed_values(self):
        self.assertEqual(DEFAULT_CONFIG["bilibili"]["check_interval"], 3600)
        self.assertEqual(DEFAULT_CONFIG["rate_limit"]["min_request_interval"], 10.0)
        self.assertEqual(DEFAULT_CONFIG["rate_limit"]["max_retries"], 3)
        self.assertEqual(DEFAULT_CONFIG["rate_limit"]["retry_delay"], 20)
        self.assertEqual(DEFAULT_CONFIG["reply"]["max_process"], 500)
        self.assertEqual(DEFAULT_CONFIG["reply"]["reply_delay"], 10)
        self.assertFalse(DEFAULT_CONFIG["reply"]["auto_send_enabled"])
        self.assertIn("默认不要使用", DEFAULT_CONFIG["ark"]["system_prompt"])
        self.assertIn("哈哈哈", DEFAULT_CONFIG["ark"]["system_prompt"])
        self.assertIn("是不是AI", DEFAULT_CONFIG["ark"]["system_prompt"])

    def test_scheduled_round_uses_product_account_coordinator(self):
        events = []

        @contextmanager
        def coordinated(_stop_event, _logger, _should_continue):
            events.append("enter")
            yield True
            events.append("exit")

        self.bot.set_automatic_round_context(coordinated)
        self.bot.process_comments = Mock()

        self.bot._process_scheduled_round()

        self.bot.process_comments.assert_called_once_with()
        self.assertEqual(events, ["enter", "exit"])

    def test_cancelled_scheduled_round_does_not_process_comments(self):
        @contextmanager
        def cancelled(_stop_event, _logger, _should_continue):
            yield False

        self.bot.set_automatic_round_context(cancelled)
        self.bot.process_comments = Mock()

        self.bot._process_scheduled_round()

        self.bot.process_comments.assert_not_called()

    def test_monitor_manual_mode_only_generates_drafts(self):
        self.bot.config["reply"]["enabled"] = True
        self.bot.config["reply"]["auto_send_enabled"] = False
        self.bot._running = True
        self.bot.generate_review_drafts = Mock(return_value={
            "generated": 1,
            "replyable": 1,
            "skipped": 0,
            "replyable_ids": ["new"],
            "auto_send_enabled": False,
        })
        self.bot.send_approved_drafts = Mock()

        self.bot.process_comments()

        self.bot.generate_review_drafts.assert_called_once_with(
            include_generated_ids=True
        )
        self.bot.send_approved_drafts.assert_not_called()

    def test_monitor_auto_mode_sends_only_current_round_replyable_ids(self):
        self.bot.config["reply"]["enabled"] = True
        self.bot.config["reply"]["auto_send_enabled"] = True
        self.bot._running = True
        self.bot.generate_review_drafts = Mock(return_value={
            "generated": 3,
            "replyable": 2,
            "skipped": 1,
            "generation_id": "round-1",
            "replyable_ids": ["new-1", "new-2"],
        })
        self.bot.send_approved_drafts = Mock(return_value={
            "sent": 2,
            "failed": 0,
            "unknown": 0,
        })

        self.bot.process_comments()

        self.bot.send_approved_drafts.assert_called_once_with(
            comment_ids=["new-1", "new-2"],
            auto_approve=True,
            auto_generation_id="round-1",
        )

    def test_monitor_stop_during_generation_leaves_new_drafts_pending(self):
        self.bot.config["reply"]["enabled"] = True
        self.bot.config["reply"]["auto_send_enabled"] = True
        self.bot._running = False
        self.bot.generate_review_drafts = Mock(return_value={
            "generated": 1,
            "replyable": 1,
            "skipped": 0,
            "generation_id": "round-stop",
            "replyable_ids": ["new"],
        })
        self.bot.send_approved_drafts = Mock()

        self.bot.process_comments()

        self.bot.send_approved_drafts.assert_not_called()

    def test_monitor_auto_send_busy_keeps_current_round_for_review(self):
        self.bot.config["reply"]["enabled"] = True
        self.bot.config["reply"]["auto_send_enabled"] = True
        self.bot._running = True
        self.bot.generate_review_drafts = Mock(return_value={
            "generated": 1,
            "replyable": 1,
            "skipped": 0,
            "generation_id": "round-busy",
            "replyable_ids": ["new"],
        })
        self.bot.send_approved_drafts = Mock(
            side_effect=ReviewOperationBusyError("已有发送任务")
        )

        self.bot.process_comments()

        self.bot.send_approved_drafts.assert_called_once_with(
            comment_ids=["new"],
            auto_approve=True,
            auto_generation_id="round-busy",
        )

    def test_monitor_config_disabled_during_generation_does_not_start_send(self):
        self.bot.config["reply"]["enabled"] = True
        self.bot.config["reply"]["auto_send_enabled"] = True
        self.bot._running = True

        def finish_generation_after_disable(**_kwargs):
            self.bot.config["reply"]["auto_send_enabled"] = False
            return {
                "generated": 1,
                "replyable": 1,
                "skipped": 0,
                "generation_id": "round-disabled",
                "replyable_ids": ["new"],
            }

        self.bot.generate_review_drafts = Mock(
            side_effect=finish_generation_after_disable
        )
        self.bot.send_approved_drafts = Mock()

        self.bot.process_comments()

        self.bot.send_approved_drafts.assert_not_called()

    def test_verify_login_updates_uid_and_persists_identity(self):
        self.bot.config["bilibili"]["uid"] = ""
        self.bot.cookie_manager = Mock()
        self.bot.cookie_manager.verify_cookie.return_value = (
            True,
            {
                "message": "Cookie有效",
                "user_info": {"mid": 3546589337487797, "name": "喵酱第一"},
            },
        )
        self.bot.on_identity_changed = Mock()
        self.bot._identity_verified = False

        result = self.bot.verify_login()

        self.assertTrue(result["valid"])
        self.assertEqual(self.bot.config["bilibili"]["uid"], "3546589337487797")
        self.assertEqual(self.bot._identity_name, "喵酱第一")
        self.bot.on_identity_changed.assert_called_once_with(
            "3546589337487797",
            "喵酱第一",
        )

    def test_instance_data_directories_isolate_drafts_and_history(self):
        account_a_dir = os.path.join(self.temp_dir.name, "account-a")
        account_b_dir = os.path.join(self.temp_dir.name, "account-b")
        logger = logging.getLogger("account-isolation-tests")
        config = copy.deepcopy(DEFAULT_CONFIG)

        account_a = BiliCommentBot(
            copy.deepcopy(config),
            logger,
            data_dir=account_a_dir,
            account_id="account-a",
        )
        account_b = BiliCommentBot(
            copy.deepcopy(config),
            logger,
            data_dir=account_b_dir,
            account_id="account-b",
        )
        account_a._review_drafts["a"] = self._draft("a")
        account_a._save_review_drafts()
        account_a._history_buffer = [{"comment_id": "history-a"}]
        account_a._history_dirty = True
        account_a._flush_history()

        reloaded_a = BiliCommentBot(
            copy.deepcopy(config),
            logger,
            data_dir=account_a_dir,
            account_id="account-a",
        )
        reloaded_b = BiliCommentBot(
            copy.deepcopy(config),
            logger,
            data_dir=account_b_dir,
            account_id="account-b",
        )

        self.assertEqual(
            [draft["comment_id"] for draft in reloaded_a.get_review_drafts()],
            ["a"],
        )
        self.assertEqual(reloaded_b.get_review_drafts(), [])
        self.assertEqual(reloaded_a.get_history(), [{"comment_id": "history-a"}])
        self.assertEqual(reloaded_b.get_history(), [])

    def test_empty_requested_list_sends_nothing(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=True, status="approved")

        result = self.bot.send_approved_drafts(comment_ids=[])

        self.assertEqual(result, {"sent": 0, "failed": 0, "unknown": 0})
        self.bot.reply_comment.assert_not_called()

    def test_auto_send_approves_only_explicit_new_pending_drafts(self):
        self.bot.config["reply"]["reply_delay"] = 0
        self.bot.config["reply"]["auto_send_enabled"] = True
        self.bot._running = True
        self.bot._review_drafts["new"] = self._draft(
            "new",
            approved=False,
            status="pending",
        )
        self.bot._review_drafts["new"]["auto_generation_id"] = "round-new"
        self.bot._review_drafts["old"] = self._draft(
            "old",
            approved=True,
            status="approved",
        )
        self.bot._ensure_review_operation_state()
        self.bot._auto_send_rounds["round-new"] = {"new"}

        result = self.bot.send_approved_drafts(
            comment_ids=["new"],
            auto_approve=True,
            auto_generation_id="round-new",
        )

        self.assertEqual(result, {"sent": 1, "failed": 0, "unknown": 0})
        self.bot.reply_comment.assert_called_once()
        self.assertEqual(self.bot.reply_comment.call_args.args[1], "new")
        self.assertEqual(self.bot._review_drafts["new"]["status"], "sent")
        self.assertEqual(
            self.bot._review_drafts["new"]["approval_source"],
            "auto",
        )
        self.assertEqual(self.bot._review_drafts["old"]["status"], "approved")
        self.assertTrue(self.bot._review_drafts["old"]["approved"])

    def test_auto_send_does_not_approve_doubao_skipped_draft(self):
        self.bot.config["reply"]["auto_send_enabled"] = True
        self.bot._running = True
        skipped = self._draft("skip", approved=False, status="skipped")
        skipped["should_reply"] = False
        skipped["reply"] = ""
        skipped["auto_generation_id"] = "round-skip"
        self.bot._review_drafts["skip"] = skipped
        self.bot._ensure_review_operation_state()
        self.bot._auto_send_rounds["round-skip"] = {"skip"}

        result = self.bot.send_approved_drafts(
            comment_ids=["skip"],
            auto_approve=True,
            auto_generation_id="round-skip",
        )

        self.assertEqual(result, {"sent": 0, "failed": 0, "unknown": 0})
        self.bot.reply_comment.assert_not_called()
        self.assertEqual(self.bot._review_drafts["skip"]["status"], "skipped")

    def test_auto_send_is_blocked_by_latest_disabled_config(self):
        self.bot.config["reply"]["auto_send_enabled"] = False
        self.bot._running = True
        draft = self._draft("new", approved=False, status="pending")
        draft["auto_generation_id"] = "round-disabled"
        self.bot._review_drafts["new"] = draft
        self.bot._ensure_review_operation_state()
        self.bot._auto_send_rounds["round-disabled"] = {"new"}

        with self.assertRaises(AutoSendBlockedError):
            self.bot.send_approved_drafts(
                comment_ids=["new"],
                auto_approve=True,
                auto_generation_id="round-disabled",
            )

        self.bot.reply_comment.assert_not_called()
        self.assertEqual(self.bot._review_drafts["new"]["status"], "pending")

    def test_auto_send_does_not_call_bilibili_when_sending_state_save_fails(self):
        self.bot.config["reply"]["auto_send_enabled"] = True
        self.bot._running = True
        draft = self._draft("new", approved=False, status="pending")
        draft["auto_generation_id"] = "round-save-failure"
        self.bot._review_drafts["new"] = draft
        self.bot._ensure_review_operation_state()
        self.bot._auto_send_rounds["round-save-failure"] = {"new"}
        original_save = self.bot._save_review_drafts
        save_calls = 0

        def fail_second_save():
            nonlocal save_calls
            save_calls += 1
            if save_calls == 2:
                raise OSError("模拟发送前落盘失败")
            return original_save()

        with patch.object(
            self.bot,
            "_save_review_drafts",
            side_effect=fail_second_save,
        ):
            with self.assertRaisesRegex(OSError, "模拟发送前落盘失败"):
                self.bot.send_approved_drafts(
                    comment_ids=["new"],
                    auto_approve=True,
                    auto_generation_id="round-save-failure",
                )

        self.bot.reply_comment.assert_not_called()
        current = self.bot._review_drafts["new"]
        self.assertEqual(current["status"], "pending")
        self.assertFalse(current["approved"])
        self.assertNotIn("approval_source", current)
        with open(self.drafts_file, "r", encoding="utf-8") as handle:
            persisted = json.load(handle)[0]
        self.assertEqual(persisted["status"], "pending")
        self.assertFalse(persisted["approved"])

    def test_auto_send_rejects_old_pending_draft_not_tagged_for_round(self):
        self.bot.config["reply"]["auto_send_enabled"] = True
        self.bot._running = True
        self.bot._review_drafts["old"] = self._draft(
            "old",
            approved=False,
            status="pending",
        )
        self.bot._ensure_review_operation_state()
        self.bot._auto_send_rounds["round-current"] = {"old"}

        with self.assertRaisesRegex(ValueError, "不属于本轮"):
            self.bot.send_approved_drafts(
                comment_ids=["old"],
                auto_approve=True,
                auto_generation_id="round-current",
            )

        self.bot.reply_comment.assert_not_called()
        self.assertEqual(self.bot._review_drafts["old"]["status"], "pending")

    def test_auto_send_stop_after_current_request_reverts_remaining_draft(self):
        self.bot.config["reply"]["reply_delay"] = 0
        self.bot.config["reply"]["auto_send_enabled"] = True
        self.bot._running = True
        for comment_id in ("first", "second"):
            draft = self._draft(comment_id, approved=False, status="pending")
            draft["auto_generation_id"] = "round-stop-mid-send"
            self.bot._review_drafts[comment_id] = draft
        self.bot._ensure_review_operation_state()
        self.bot._auto_send_rounds["round-stop-mid-send"] = {
            "first",
            "second",
        }

        def stop_after_first(*_args, **_kwargs):
            self.bot.stop()
            return True

        self.bot.reply_comment = Mock(side_effect=stop_after_first)

        result = self.bot.send_approved_drafts(
            comment_ids=["first", "second"],
            auto_approve=True,
            auto_generation_id="round-stop-mid-send",
        )

        self.assertEqual(result, {"sent": 1, "failed": 0, "unknown": 0})
        self.assertEqual(self.bot.reply_comment.call_count, 1)
        self.assertEqual(self.bot._review_drafts["first"]["status"], "sent")
        self.assertEqual(self.bot._review_drafts["second"]["status"], "pending")
        self.assertFalse(self.bot._review_drafts["second"]["approved"])

    def test_prepare_shutdown_reverts_unsent_auto_approval(self):
        draft = self._draft("queued", approved=True, status="approved")
        draft["approval_source"] = "auto"
        self.bot._review_drafts["queued"] = draft

        idle = self.bot.prepare_shutdown(wait_timeout=0)

        self.assertTrue(idle)
        self.assertEqual(self.bot._review_drafts["queued"]["status"], "pending")
        self.assertFalse(self.bot._review_drafts["queued"]["approved"])

    def test_scan_and_send_share_one_bilibili_gate_per_account(self):
        self.bot._ensure_review_operation_state()
        self.bot._review_drafts["1"] = self._draft(
            "1",
            approved=True,
            status="approved",
        )

        with self.bot._review_operation(
            "generating",
            gate=self.bot._review_generation_gate,
            exclusive_gate=self.bot._review_bilibili_gate,
        ):
            with self.assertRaisesRegex(
                ReviewOperationBusyError,
                "扫描或发送任务",
            ):
                self.bot.send_approved_drafts(comment_ids=["1"])

        self.bot.reply_comment.assert_not_called()

    def test_reply_comment_sends_doubao_text_and_targets_current_comment(self):
        reply_bot = BiliCommentBot.__new__(BiliCommentBot)
        reply_bot.config = copy.deepcopy(DEFAULT_CONFIG)
        reply_bot.config["reply"]["prefix"] = "不应发送的前缀"
        reply_bot.logger = logging.getLogger("reply-original-text-test")
        reply_bot.csrf_token = "csrf"
        reply_bot.cookie_manager = Mock()
        reply_bot.cookie_manager._get_csrf_from_cookie.return_value = "csrf"
        reply_bot.cookie_manager.verify_cookie.return_value = (True, {})
        reply_bot.bvid_to_aid = Mock(return_value="123")
        response = Mock()
        response.__bool__ = Mock(return_value=True)
        response.json.return_value = {"code": 0}
        reply_bot.make_request_with_retry = Mock(return_value=response)

        result = reply_bot.reply_comment(
            "BV1test",
            "100",
            "豆包原始候选",
            root_id="900",
        )

        self.assertTrue(result.ok)
        sent_data = reply_bot.make_request_with_retry.call_args.kwargs["data"]
        self.assertEqual(sent_data["message"], "豆包原始候选")
        self.assertEqual(sent_data["root"], "900")
        self.assertEqual(sent_data["parent"], "100")

    def test_reply_comment_marks_closed_comment_area_as_permanent_failure(self):
        reply_bot = BiliCommentBot.__new__(BiliCommentBot)
        reply_bot.config = copy.deepcopy(DEFAULT_CONFIG)
        reply_bot.logger = logging.getLogger("reply-closed-comment-area-test")
        reply_bot.csrf_token = "csrf"
        reply_bot.cookie_manager = Mock()
        reply_bot.cookie_manager._get_csrf_from_cookie.return_value = "csrf"
        reply_bot.cookie_manager.verify_cookie.return_value = (True, {})
        reply_bot.bvid_to_aid = Mock(return_value="123")
        response = Mock()
        response.__bool__ = Mock(return_value=True)
        response.json.return_value = {
            "code": -404,
            "message": "当前页面评论功能已关闭",
        }
        reply_bot.make_request_with_retry = Mock(return_value=response)

        result = reply_bot.reply_comment("BV1test", "100", "豆包候选")

        self.assertFalse(result.ok)
        self.assertFalse(result.uncertain)
        self.assertTrue(result.permanent)
        self.assertEqual(result.message, "当前页面评论功能已关闭")

    def test_only_explicitly_approved_requested_draft_is_sent(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=True, status="approved")
        self.bot._review_drafts["2"] = self._draft("2", approved=False, status="pending")
        self.bot._review_drafts["3"] = self._draft("3", approved=True, status="approved")

        result = self.bot.send_approved_drafts(comment_ids=["1", "2"])

        self.assertEqual(result, {"sent": 1, "failed": 0, "unknown": 0})
        self.bot.reply_comment.assert_called_once_with(
            "BV1test",
            "1",
            "豆包原文",
            root_id=None,
            oid=None,
            comment_type=1,
        )
        self.assertEqual(self.bot._review_drafts["1"]["status"], "sent")
        self.assertEqual(self.bot._review_drafts["3"]["status"], "approved")

    def test_child_reply_is_sent_to_child_not_its_existing_parent(self):
        draft = self._draft("200", approved=True, status="approved")
        draft.update({
            "root_id": "100",
            "parent_id": "100",
            "depth": 1,
        })
        self.bot._review_drafts["200"] = draft

        result = self.bot.send_approved_drafts(comment_ids=["200"])

        self.assertEqual(result, {"sent": 1, "failed": 0, "unknown": 0})
        self.bot.reply_comment.assert_called_once_with(
            "BV1test",
            "200",
            "豆包原文",
            root_id="100",
            oid=None,
            comment_type=1,
        )

    def test_send_rejects_approved_draft_outside_time_range_atomically(self):
        recent = self._draft("1", approved=True, status="approved")
        recent["comment_time"] = 200
        old = self._draft("2", approved=True, status="approved")
        old["comment_time"] = 100
        self.bot._review_drafts["1"] = recent
        self.bot._review_drafts["2"] = old

        with self.assertRaisesRegex(ValueError, "超出当前时间范围"):
            self.bot.send_approved_drafts(
                comment_ids=["1", "2"],
                since_timestamp=150,
            )

        self.bot.reply_comment.assert_not_called()
        self.assertEqual(self.bot._review_drafts["1"]["status"], "approved")
        self.assertEqual(self.bot._review_drafts["2"]["status"], "approved")

    def test_send_marks_draft_sending_on_disk_before_bilibili_request(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=True, status="approved")
        observed = {}

        def fake_reply(*args, **kwargs):
            with open(self.drafts_file, "r", encoding="utf-8") as handle:
                saved = json.load(handle)[0]
            observed["status"] = saved["status"]
            observed["approved"] = saved["approved"]
            return ReplyAttemptResult(True)

        self.bot.reply_comment = fake_reply
        result = self.bot.send_approved_drafts(comment_ids=["1"])

        self.assertEqual(observed, {"status": "sending", "approved": False})
        self.assertEqual(result, {"sent": 1, "failed": 0, "unknown": 0})
        self.assertEqual(self.bot._review_drafts["1"]["status"], "sent")

    def test_uncertain_send_is_never_automatically_reapproved(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=True, status="approved")
        self.bot.reply_comment = Mock(return_value=ReplyAttemptResult(
            False,
            True,
            "请求没有返回",
        ))

        result = self.bot.send_approved_drafts(comment_ids=["1"])

        draft = self.bot._review_drafts["1"]
        self.assertEqual(result, {"sent": 0, "failed": 0, "unknown": 1})
        self.assertEqual(draft["status"], "send_unknown")
        self.assertFalse(draft["approved"])
        with self.assertRaisesRegex(ValueError, "待核对"):
            self.bot.set_review_approval("1", True)

    def test_permanent_send_failure_is_marked_unavailable_and_not_retryable(self):
        self.bot._review_drafts["1"] = self._draft(
            "1",
            approved=True,
            status="approved",
        )
        self.bot.reply_comment = Mock(return_value=ReplyAttemptResult(
            False,
            False,
            "当前页面评论功能已关闭",
            True,
        ))

        result = self.bot.send_approved_drafts(comment_ids=["1"])

        draft = self.bot._review_drafts["1"]
        self.assertEqual(result, {"sent": 0, "failed": 1, "unknown": 0})
        self.assertEqual(draft["status"], "unavailable")
        self.assertFalse(draft["approved"])
        self.assertFalse(draft["should_reply"])
        self.assertEqual(draft["error"], "当前页面评论功能已关闭")
        self.assertIn("unavailable_at", draft)
        with self.assertRaisesRegex(ValueError, "不可回复"):
            self.bot.set_review_approval("1", True)
        with self.assertRaisesRegex(ValueError, "不可回复"):
            self.bot.regenerate_review_draft("1")

    def test_second_send_request_is_rejected_while_first_is_active(self):
        self.bot._review_drafts["1"] = self._draft("1", approved=True, status="approved")
        entered = threading.Event()
        release = threading.Event()
        result_holder = {}
        reply_calls = []

        def blocking_reply(*args, **kwargs):
            reply_calls.append(args[1])
            entered.set()
            release.wait(timeout=2)
            return ReplyAttemptResult(True)

        self.bot.reply_comment = blocking_reply

        def run_send():
            result_holder["result"] = self.bot.send_approved_drafts(comment_ids=["1"])

        thread = threading.Thread(target=run_send)
        thread.start()
        self.assertTrue(entered.wait(timeout=1))
        with self.assertRaisesRegex(ReviewOperationBusyError, "已有发送任务"):
            self.bot.send_approved_drafts(comment_ids=["1"])
        release.set()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(
            result_holder["result"],
            {"sent": 1, "failed": 0, "unknown": 0},
        )
        self.assertEqual(reply_calls, ["1"])

    def test_stop_then_immediate_start_waits_for_previous_worker_cleanup(self):
        entered = threading.Event()
        release = threading.Event()
        emitted_statuses = []

        def blocking_process():
            entered.set()
            release.wait(timeout=2)

        def record_emit(event, data):
            if event == "bot_status":
                emitted_statuses.append(bool(data["running"]))

        self.bot.process_comments = blocking_process
        self.bot._emit = record_emit
        worker = None

        try:
            self.assertTrue(self.bot.start())
            worker = self.bot._thread
            self.assertTrue(entered.wait(timeout=1))
            self.assertTrue(self.bot.stop())
            with self.assertRaisesRegex(
                ReviewOperationBusyError,
                "上一次定时处理仍在收尾",
            ):
                self.bot.start()
        finally:
            release.set()
            if worker is not None:
                worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.bot._thread)
        self.assertEqual(emitted_statuses, [True, False])

    def test_load_recovers_interrupted_sending_and_regeneration(self):
        sending = self._draft("1", approved=False, status="sending")
        regenerating = self._draft("2", approved=False, status="regenerating")
        with open(self.drafts_file, "w", encoding="utf-8") as handle:
            json.dump([sending, regenerating], handle, ensure_ascii=False)

        self.bot._review_drafts = {}
        self.bot.load_review_drafts()

        self.assertEqual(self.bot._review_drafts["1"]["status"], "send_unknown")
        self.assertFalse(self.bot._review_drafts["1"]["approved"])
        self.assertEqual(self.bot._review_drafts["2"]["status"], "pending")
        self.assertFalse(self.bot._review_drafts["2"]["approved"])

    def test_load_recovers_auto_approved_draft_without_resending(self):
        approved = self._draft("1", approved=True, status="approved")
        approved.update({
            "approval_source": "auto",
            "approved_at": "2026-08-23 12:00:00",
            "auto_generation_id": "old-round",
        })
        with open(self.drafts_file, "w", encoding="utf-8") as handle:
            json.dump([approved], handle, ensure_ascii=False)

        self.bot._review_drafts = {}
        self.bot.load_review_drafts()

        draft = self.bot._review_drafts["1"]
        self.assertEqual(draft["status"], "pending")
        self.assertFalse(draft["approved"])
        self.assertNotIn("approval_source", draft)
        self.assertNotIn("approved_at", draft)
        self.assertNotIn("auto_generation_id", draft)
        self.assertIn("不会自动重发", draft["error"])

    def test_load_migrates_closed_comment_failure_to_unavailable(self):
        failed = self._draft("1", approved=True, status="failed")
        failed["error"] = "当前页面评论功能已关闭"
        with open(self.drafts_file, "w", encoding="utf-8") as handle:
            json.dump([failed], handle, ensure_ascii=False)

        self.bot._review_drafts = {}
        self.bot.load_review_drafts()

        draft = self.bot._review_drafts["1"]
        self.assertEqual(draft["status"], "unavailable")
        self.assertFalse(draft["approved"])
        self.assertFalse(draft["should_reply"])
        self.assertIn("unavailable_at", draft)
        with self.assertRaisesRegex(ValueError, "不可回复"):
            self.bot.set_review_approval("1", True)

    def test_config_change_is_deferred_until_active_operation_finishes(self):
        old_config = copy.deepcopy(self.bot.config)
        new_config = copy.deepcopy(old_config)
        new_config["ark"]["system_prompt"] = "新提示词"

        def apply_config(cfg):
            self.bot.config = copy.deepcopy(cfg)

        with patch.object(self.bot, "_apply_config", side_effect=apply_config) as apply:
            with self.bot._review_operation("generating"):
                applied = self.bot.reload_config(new_config)
                self.assertFalse(applied)
                self.assertEqual(
                    self.bot.config["ark"]["system_prompt"],
                    old_config["ark"]["system_prompt"],
                )
                apply.assert_not_called()

            apply.assert_called_once()
            self.assertEqual(self.bot.config["ark"]["system_prompt"], "新提示词")

    def test_full_review_flow_does_not_regenerate_existing_drafts(self):
        self.bot.config["reply"]["reply_delay"] = 0
        comments = [
            bot_module.Comment(
                comment_id="501",
                content="值得回复的问题",
                user="观众甲",
                uid="1",
                time=501,
            ),
            bot_module.Comment(
                comment_id="500",
                content="普通感叹",
                user="观众乙",
                uid="2",
                time=500,
            ),
        ]
        feed = [
            {
                "bvid": "BV501",
                "oid": "9501",
                "comment_type": 1,
                "video_title": "视频甲",
                "video_desc": "",
                "comment": comments[0],
                "parent_comment": None,
            },
            {
                "bvid": "BV500",
                "oid": "9500",
                "comment_type": 1,
                "video_title": "视频乙",
                "video_desc": "",
                "comment": comments[1],
                "parent_comment": None,
            },
        ]
        self.bot.get_creator_comment_feed = Mock(return_value=feed)
        self.bot.generate_reply_decisions_resilient = Mock(return_value=[
            {
                "id": "501",
                "should_reply": True,
                "reply": "豆包候选回复",
                "reason": "问题清楚",
                "model": "doubao",
            },
            {
                "id": "500",
                "should_reply": False,
                "reply": "",
                "reason": "无需回复",
                "model": "doubao",
            },
        ])

        first_items = self.bot._collect_review_items(limit=100)
        generated = self.bot._generate_review_drafts(first_items)
        second_items = self.bot._collect_review_items(limit=100)
        approved = self.bot.set_review_approval("501", True)
        sent = self.bot.send_approved_drafts(comment_ids=["501", "500"])

        self.assertEqual(generated, {"generated": 2, "replyable": 1, "skipped": 1})
        self.assertEqual(second_items, [])
        self.assertEqual(self.bot.generate_reply_decisions_resilient.call_count, 1)
        self.assertEqual(approved["reply"], "豆包候选回复")
        self.assertEqual(sent, {"sent": 1, "failed": 0, "unknown": 0})
        self.bot.reply_comment.assert_called_once_with(
            "BV501",
            "501",
            "豆包候选回复",
            root_id=None,
            oid="9501",
            comment_type=1,
        )

    def test_creator_comment_feed_uses_time_order_and_cutoff(self):
        self.bot.make_request_with_retry = Mock(return_value=bot_module.CachedResponse({
            "code": 0,
            "message": "OK",
            "data": {
                "page": {"num": 1, "size": 10, "total": 3},
                "list": [
                    {
                        "rpid": 3,
                        "oid": 30,
                        "type": 1,
                        "root": 0,
                        "parent": 0,
                        "bvid": "BV3",
                        "title": "最新视频",
                        "ctime": 300,
                        "member": {"mid": 13, "uname": "甲"},
                        "content": {"message": "最新"},
                        "up_action": {"reply": False},
                    },
                    {
                        "rpid": 2,
                        "oid": 20,
                        "type": 1,
                        "root": 0,
                        "parent": 0,
                        "bvid": "BV2",
                        "title": "较早视频",
                        "ctime": 200,
                        "member": {"mid": 12, "uname": "乙"},
                        "content": {"message": "较早"},
                        "up_action": {"reply": False},
                    },
                    {
                        "rpid": 1,
                        "oid": 10,
                        "type": 1,
                        "root": 0,
                        "parent": 0,
                        "bvid": "BV1",
                        "title": "边界外视频",
                        "ctime": 100,
                        "member": {"mid": 11, "uname": "丙"},
                        "content": {"message": "边界外"},
                        "up_action": {"reply": False},
                    },
                ],
            },
        }))

        items = self.bot.get_creator_comment_feed(limit=5, since_timestamp=150)

        self.assertEqual([item["comment"].comment_id for item in items], ["3", "2"])
        request = self.bot.make_request_with_retry.call_args
        self.assertEqual(request.args[:2], ("GET", "https://api.bilibili.com/x/v2/reply/up/fulllist"))
        self.assertEqual(request.kwargs["params"]["order"], 1)
        self.assertEqual(request.kwargs["params"]["pn"], 1)
        self.assertEqual(request.kwargs["params"]["ps"], 10)
        self.assertFalse(request.kwargs["use_cache"])

    def test_creator_feed_excludes_own_rows_and_comments_already_replied_to(self):
        payload = {
            "code": 0,
            "message": "OK",
            "data": {
                "page": {"num": 1, "size": 10, "total": 4},
                "list": [
                    {
                        "rpid": 200,
                        "oid": 20,
                        "type": 1,
                        "root": 100,
                        "parent": 900,
                        "bvid": "BV2",
                        "title": "观众的新追评",
                        "ctime": 400,
                        "member": {"mid": 42, "uname": "观众"},
                        "content": {"message": "新的追问"},
                        "parent_info": {
                            "rpid": 900,
                            "ctime": 350,
                            "member": {"mid": 999, "uname": "UP"},
                            "content": {"message": "UP之前的回复"},
                        },
                        "up_action": {"reply": False},
                    },
                    {
                        "rpid": 900,
                        "oid": 20,
                        "type": 1,
                        "root": 100,
                        "parent": 100,
                        "bvid": "BV2",
                        "title": "UP自己的回复",
                        "ctime": 350,
                        "member": {"mid": 999, "uname": "UP"},
                        "content": {"message": "已经答过"},
                        "up_action": {"reply": False},
                    },
                    {
                        "rpid": 100,
                        "oid": 20,
                        "type": 1,
                        "root": 0,
                        "parent": 0,
                        "bvid": "BV2",
                        "title": "已回复的原评论",
                        "ctime": 300,
                        "member": {"mid": 42, "uname": "观众"},
                        "content": {"message": "原问题"},
                        "up_action": {"reply": False},
                    },
                    {
                        "rpid": 50,
                        "oid": 10,
                        "type": 1,
                        "root": 0,
                        "parent": 0,
                        "bvid": "BV1",
                        "title": "接口标记已回复",
                        "ctime": 200,
                        "member": {"mid": 43, "uname": "另一位观众"},
                        "content": {"message": "另一条"},
                        "up_action": {"reply": True},
                    },
                ],
            },
        }
        self.bot.make_request_with_retry = Mock(
            return_value=bot_module.CachedResponse(payload)
        )

        items = self.bot.get_creator_comment_feed(limit=10, my_uid="999")

        self.assertEqual(
            [item["comment"].comment_id for item in items],
            ["200"],
        )
        self.assertEqual(items[0]["parent_comment"].uid, "999")

    def test_creator_feed_stops_after_three_pages_without_new_candidates(self):
        pages = []
        known_ids = set()
        for page_number in range(1, 6):
            rows = []
            for offset in range(10):
                comment_id = page_number * 100 + offset
                known_ids.add(str(comment_id))
                rows.append(self._creator_row(comment_id))
            pages.append(self._creator_page(rows, page_number=page_number))
        self.bot.make_request_with_retry = Mock(side_effect=pages)

        items = self.bot.get_creator_comment_feed(
            limit=500,
            skip_comment_ids=known_ids,
        )

        self.assertEqual(items, [])
        self.assertEqual(self.bot.make_request_with_retry.call_count, 3)

    def test_creator_feed_can_disable_three_empty_page_stop(self):
        pages = []
        known_ids = set()
        for page_number in range(1, 6):
            rows = []
            for offset in range(10):
                comment_id = page_number * 100 + offset
                known_ids.add(str(comment_id))
                rows.append(self._creator_row(comment_id))
            pages.append(
                self._creator_page(
                    rows,
                    page_number=page_number,
                    total=50,
                )
            )
        self.bot.make_request_with_retry = Mock(side_effect=pages)

        items = self.bot.get_creator_comment_feed(
            limit=50,
            skip_comment_ids=known_ids,
            stop_after_empty_pages=False,
        )

        self.assertEqual(items, [])
        self.assertEqual(self.bot.make_request_with_retry.call_count, 5)

    def test_creator_feed_new_candidate_resets_empty_page_counter(self):
        pages = []
        known_ids = set()
        expected_new_id = "305"
        for page_number in range(1, 8):
            rows = []
            for offset in range(10):
                comment_id = page_number * 100 + offset
                if str(comment_id) != expected_new_id:
                    known_ids.add(str(comment_id))
                rows.append(self._creator_row(comment_id))
            pages.append(self._creator_page(rows, page_number=page_number))
        self.bot.make_request_with_retry = Mock(side_effect=pages)

        items = self.bot.get_creator_comment_feed(
            limit=500,
            skip_comment_ids=known_ids,
        )

        self.assertEqual(
            [item["comment"].comment_id for item in items],
            [expected_new_id],
        )
        self.assertEqual(self.bot.make_request_with_retry.call_count, 6)

    def test_creator_feed_filtered_rows_do_not_reset_empty_page_counter(self):
        pages = []
        for page_number in range(1, 6):
            rows = [
                self._creator_row(page_number * 100 + offset, message="过滤")
                for offset in range(10)
            ]
            pages.append(self._creator_page(rows, page_number=page_number))
        self.bot.make_request_with_retry = Mock(side_effect=pages)

        items = self.bot.get_creator_comment_feed(
            limit=500,
            candidate_filter=lambda comment: comment.content != "过滤",
        )

        self.assertEqual(items, [])
        self.assertEqual(self.bot.make_request_with_retry.call_count, 3)

    def test_creator_feed_raw_limit_is_a_hard_scan_boundary(self):
        pages = []
        for page_number in range(1, 6):
            rows = [
                self._creator_row(page_number * 100 + offset)
                for offset in range(10)
            ]
            pages.append(self._creator_page(rows, page_number=page_number))
        self.bot.make_request_with_retry = Mock(side_effect=pages)

        items = self.bot.get_creator_comment_feed(limit=25)

        self.assertEqual(len(items), 25)
        self.assertEqual(self.bot.make_request_with_retry.call_count, 3)

    def test_creator_feed_aborts_instead_of_generating_from_partial_scan(self):
        first_page = self._creator_page(
            [self._creator_row(100 + offset) for offset in range(10)],
            page_number=1,
        )
        self.bot.make_request_with_retry = Mock(
            side_effect=[first_page, None]
        )

        with self.assertRaisesRegex(RuntimeError, "扫描中断"):
            self.bot.get_creator_comment_feed(limit=500)

        self.assertEqual(self.bot.make_request_with_retry.call_count, 2)

    def test_creator_feed_later_own_reply_removes_provisional_candidate(self):
        target_id = "100"
        page_one = [
            self._creator_row(target_id, uid="42"),
            *[
                self._creator_row(110 + offset, uid="999")
                for offset in range(9)
            ],
        ]
        page_two = [
            self._creator_row(200, uid="999", parent=int(target_id), root=int(target_id)),
            *[
                self._creator_row(210 + offset, uid="999")
                for offset in range(9)
            ],
        ]
        pages = [
            self._creator_page(page_one, page_number=1),
            self._creator_page(page_two, page_number=2),
            self._creator_page(
                [self._creator_row(300 + offset, uid="999") for offset in range(10)],
                page_number=3,
            ),
            self._creator_page(
                [self._creator_row(400 + offset, uid="999") for offset in range(10)],
                page_number=4,
            ),
        ]
        self.bot.make_request_with_retry = Mock(side_effect=pages)

        items = self.bot.get_creator_comment_feed(limit=500, my_uid="999")

        self.assertEqual(items, [])
        self.assertEqual(self.bot.make_request_with_retry.call_count, 4)

    def test_creator_feed_random_page_sequences_preserve_stop_invariants(self):
        for seed in range(20):
            rng = random.Random(seed)
            page_has_new = [rng.random() < 0.45 for _ in range(20)]
            expected_pages = 20
            empty_streak = 0
            for index, has_new in enumerate(page_has_new, start=1):
                empty_streak = 0 if has_new else empty_streak + 1
                if empty_streak == 3:
                    expected_pages = index
                    break

            pages = []
            known_ids = set()
            expected_ids = []
            for page_number, has_new in enumerate(page_has_new, start=1):
                rows = []
                new_offset = rng.randrange(10) if has_new else -1
                for offset in range(10):
                    comment_id = seed * 10000 + page_number * 100 + offset
                    rows.append(self._creator_row(comment_id))
                    if offset == new_offset:
                        expected_ids.append(str(comment_id))
                    else:
                        known_ids.add(str(comment_id))
                pages.append(
                    self._creator_page(
                        rows,
                        page_number=page_number,
                        total=len(page_has_new) * 10,
                    )
                )

            self.bot.make_request_with_retry = Mock(side_effect=pages)
            items = self.bot.get_creator_comment_feed(
                limit=500,
                skip_comment_ids=known_ids,
            )

            self.assertEqual(
                self.bot.make_request_with_retry.call_count,
                expected_pages,
                msg=f"seed={seed}",
            )
            self.assertEqual(
                [item["comment"].comment_id for item in items],
                expected_ids[:sum(page_has_new[:expected_pages])],
                msg=f"seed={seed}",
            )

    def test_collect_review_items_uses_creator_timeline_and_skips_replied(self):
        self.bot.config["reply"]["only_bvid"] = ""
        self.bot.config["reply"]["review_since"] = "2026-08-10 00:00"
        self.bot.config["bilibili"]["uid"] = "999"
        fresh = bot_module.Comment(
            comment_id="200",
            content="新评论",
            user="观众甲",
            uid="1",
            time=200,
        )
        already_replied = bot_module.Comment(
            comment_id="201",
            content="已经回复",
            user="观众乙",
            uid="2",
            time=190,
            replied=True,
        )
        existing_draft = bot_module.Comment(
            comment_id="202",
            content="已经生成过",
            user="观众丙",
            uid="3",
            time=180,
        )
        self.bot._review_drafts["202"] = self._draft("202")
        self.bot.get_creator_comment_feed = Mock(return_value=[
            {
                "bvid": "BV2",
                "oid": "20",
                "comment_type": 1,
                "video_title": "新视频",
                "video_desc": "",
                "comment": fresh,
                "parent_comment": None,
            },
            {
                "bvid": "BV1",
                "oid": "10",
                "comment_type": 1,
                "video_title": "旧视频",
                "video_desc": "",
                "comment": already_replied,
                "parent_comment": None,
            },
            {
                "bvid": "BV0",
                "oid": "5",
                "comment_type": 1,
                "video_title": "更旧视频",
                "video_desc": "",
                "comment": existing_draft,
                "parent_comment": None,
            },
        ])

        items = self.bot._collect_review_items(limit=200)

        self.assertEqual([item["comment"].comment_id for item in items], ["200"])
        requested_limit, since_timestamp = self.bot.get_creator_comment_feed.call_args.args
        self.assertEqual(requested_limit, 200)
        self.assertEqual(
            since_timestamp,
            self.bot._parse_review_since("2026-08-10 00:00"),
        )
        self.assertEqual(
            self.bot.get_creator_comment_feed.call_args.kwargs["my_uid"],
            "999",
        )
        self.assertIn(
            "202",
            self.bot.get_creator_comment_feed.call_args.kwargs["skip_comment_ids"],
        )

    def test_collect_review_items_resolves_relative_time_range_at_task_start(self):
        self.bot.config["reply"]["only_bvid"] = ""
        self.bot.config["bilibili"]["uid"] = "999"
        self.bot.get_creator_comment_feed = Mock(return_value=[])

        with patch.object(bot_module.time, "time", return_value=2_000_000):
            items = self.bot._collect_review_items(
                limit=100,
                review_since="",
                review_time_range="24h",
            )

        self.assertEqual(items, [])
        requested_limit, since_timestamp = (
            self.bot.get_creator_comment_feed.call_args.args
        )
        self.assertEqual(requested_limit, 100)
        self.assertEqual(since_timestamp, 2_000_000 - 24 * 60 * 60)

    def test_collect_review_items_uses_persisted_relative_range_for_monitor(self):
        self.bot.config["reply"]["only_bvid"] = ""
        self.bot.config["reply"]["review_time_range"] = "7d"
        self.bot.config["reply"]["review_since"] = ""
        self.bot.get_creator_comment_feed = Mock(return_value=[])

        with patch.object(bot_module.time, "time", return_value=3_000_000):
            self.bot._collect_review_items(limit=50)

        _, since_timestamp = self.bot.get_creator_comment_feed.call_args.args
        self.assertEqual(since_timestamp, 3_000_000 - 7 * 24 * 60 * 60)

    def test_invalid_relative_time_range_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "时间范围无效"):
            self.bot._collect_review_items(
                limit=10,
                review_time_range="one-day-ish",
            )

    def test_collect_review_items_keeps_original_chain_for_only_bvid(self):
        self.bot.config["reply"]["only_bvid"] = "BV1test"
        self.bot.config["reply"]["context_comments_count"] = 0
        self.bot.config["bilibili"]["uid"] = "999"
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

        self.bot.get_video_comments.assert_called_once_with("BV1test")
        self.assertEqual([item["comment"].comment_id for item in items], ["100", "101"])
        self.assertEqual(items[1]["parent_comment"].comment_id, "100")
        self.assertTrue(items[1]["is_follow_up"])


if __name__ == "__main__":
    unittest.main()
