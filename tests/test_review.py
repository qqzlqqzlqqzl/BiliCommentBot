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
from bot import (
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
        self.bot.stats = {"total_replied": 0}
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

    def test_review_read_limit_defaults_to_ten(self):
        self.bot.auto_refresh_cookie = False
        self.bot.config["reply"]["max_process"] = 10
        self.bot._collect_review_items = Mock(return_value=[])

        result = self.bot.generate_review_drafts()

        self.assertEqual(result, {"generated": 0, "replyable": 0, "skipped": 0})
        self.assertEqual(self.bot._collect_review_items.call_args.args[0], 10)

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
            parent_id=None,
            oid=None,
            comment_type=1,
        )
        self.assertEqual(self.bot._review_drafts["1"]["status"], "sent")
        self.assertEqual(self.bot._review_drafts["3"]["status"], "approved")

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
            parent_id=None,
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
