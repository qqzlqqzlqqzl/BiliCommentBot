#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站评论人工审核回复工具 — 机器人核心逻辑
"""
import os
import time
import json
import logging
import threading
import hashlib
import urllib.parse
import re
import random
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────────────────────────────────────────
#  文件路径常量
# ─────────────────────────────────────────────
DATA_DIR = os.environ.get("BILI_DATA_DIR", "")
CONFIG_FILE = os.path.join(DATA_DIR, "config.toml") if DATA_DIR else "config.toml"
HISTORY_FILE = os.path.join(DATA_DIR, "history.json") if DATA_DIR else "history.json"
COOKIE_FILE = os.path.join(DATA_DIR, "bilibili_cookie.json") if DATA_DIR else "bilibili_cookie.json"
VIDEO_CACHE_FILE = os.path.join(DATA_DIR, "video_cache.json") if DATA_DIR else "video_cache.json"
REVIEW_DRAFTS_FILE = os.path.join(DATA_DIR, "review_drafts.json") if DATA_DIR else "review_drafts.json"

# ─────────────────────────────────────────────
#  默认配置
# ─────────────────────────────────────────────
DEFAULT_CONFIG = {
    "bilibili": {
        "cookie": "",
        "refresh_token": "",
        "uid": "",
        "check_interval": 60,
        "auto_refresh_cookie": True,
        "cookie_refresh_interval": 30,
        "max_comment_pages": 10,
        "max_video_pages": 10,
    },
    "rate_limit": {
        "min_request_interval": 3.0,
        "max_retries": 3,
        "retry_delay": 5,
    },
    "cache": {
        "expire_time": 300,
        "enabled": True,
    },
    "video_cache": {
        "expire_time": 43200,
        "cache_file": "video_cache.json",
    },
    "ark": {
        "api_key": "",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3/responses",
        "model": "doubao-seed-2-1-turbo-260628",
        "max_tokens": 2400,
        "system_prompt": "你是B站UP主的评论回复助手。只回复语境清楚、有互动价值、低误判风险的评论；回复要自然、简短、具体，不要客服腔，不要编造事实。",
    },
    "reply": {
        "enabled": True,
        "prefix": "",
        "only_new": True,
        "max_process": 10,
        "review_since": "",
        "reply_delay": 2,
        "like_enabled": False,
        "context_comments_count": 0,
        "only_bvid": "",
        "like_user_video_enabled": False,
        "like_user_video_only_followers": False,
        "chained_reply_enabled": True,
        "max_reply_depth": 3,
        "review_batch_size": 4,
        "keyword_filter": {
            "enabled": False,
            "whitelist": "",
            "blacklist": "",
            "mode": "any",
            "match_case": False,
        },
        "length_filter": {
            "enabled": False,
            "min_length": 0,
            "max_length": 500,
        },
        "user_filter": {
            "enabled": False,
            "whitelist": "",
            "blacklist": "",
        },
    },
    "logging": {
        "level": "INFO",
        "file": "logs/bot.log",
        "console": True,
    },
    "auth": {
        "enabled": False,
        "password": "",
    },
}

# ─────────────────────────────────────────────
#  模拟响应（缓存命中时返回）
# ─────────────────────────────────────────────
class CachedResponse:
    """模拟 requests.Response，由缓存数据构造"""
    def __init__(self, data: dict):
        self.status_code = 200
        self.headers = {}
        self._data = data

    def json(self):
        return self._data

    @property
    def text(self):
        return json.dumps(self._data, ensure_ascii=False)

    @property
    def content(self):
        return self.text.encode("utf-8")


# ─────────────────────────────────────────────
#  评论数据类
# ─────────────────────────────────────────────
@dataclass
class Comment:
    comment_id: str
    content: str
    user: str
    uid: str
    time: int
    replied: bool = False
    parent_id: Optional[str] = None
    root_id: Optional[str] = None
    depth: int = 0
    children: List['Comment'] = None

    def __post_init__(self):
        if self.children is None:
            self.children = []


# ─────────────────────────────────────────────
#  B站Cookie管理器
# ─────────────────────────────────────────────
class BilibiliCookieManager:
    def __init__(self, cookie_str: str = None, refresh_token: str = None, logger=None):
        self.logger = logger or logging.getLogger("BiliBot")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        })
        adapter = HTTPAdapter(pool_connections=5, pool_maxsize=10)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        if cookie_str:
            self.set_cookie_from_str(cookie_str)
        self.refresh_token = refresh_token
        self.csrf_token = self._get_csrf_from_cookie()

    def set_cookie_from_str(self, cookie_str: str):
        cookie_dict = {}
        for item in cookie_str.split(";"):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                key, value = item.split("=", 1)
                cookie_dict[key.strip()] = value.strip()
        self.session.cookies.update(cookie_dict)

    def _get_csrf_from_cookie(self) -> Optional[str]:
        return self.session.cookies.get("bili_jct", None)

    def check_cookie_status(self) -> dict:
        url = "https://passport.bilibili.com/x/passport-login/web/cookie/info"
        try:
            response = self.session.get(url, timeout=10)
            data = response.json()
            if data.get("code") == 0:
                return {"need_refresh": data.get("data", {}).get("refresh", False), "message": "OK"}
            return {"need_refresh": False, "message": data.get("message", "未知错误")}
        except Exception as e:
            return {"need_refresh": False, "message": str(e)}

    def get_refresh_csrf(self) -> Optional[str]:
        timestamp = int(time.time())
        md5 = hashlib.md5(f"{timestamp}".encode()).hexdigest()
        correspond_path = f"/apis/redirect/login?from=bilibili.com&timestamp={timestamp}&md5={md5}"
        encoded_path = urllib.parse.quote(correspond_path, safe="")
        url = f"https://www.bilibili.com/correspond/1/{encoded_path}"
        try:
            response = self.session.get(url, timeout=15)
            html_content = response.text
            # 合并为单一正则（B站可能用双引号或单引号）
            match = re.search(
                r'''refresh_csrf\s*[=:]\s*['"]([^'"]+)['"]''',
                html_content, re.IGNORECASE,
            )
            if match:
                return match.group(1).strip()
            return self.session.cookies.get("refresh_csrf")
        except Exception as e:
            self.logger.error(f"获取refresh_csrf异常: {e}")
            return None

    def refresh_cookie(self, refresh_token: str = None) -> Tuple[bool, dict]:
        token = refresh_token or self.refresh_token
        if not token:
            return False, {"message": "refresh_token不存在"}
        refresh_csrf = self.get_refresh_csrf()
        if not refresh_csrf:
            return False, {"message": "获取refresh_csrf失败"}
        csrf_token = self._get_csrf_from_cookie()
        if not csrf_token:
            return False, {"message": "获取CSRF token失败"}
        url = "https://passport.bilibili.com/x/passport-login/web/cookie/refresh"
        params = {"csrf": csrf_token, "refresh_csrf": refresh_csrf, "refresh_token": token, "source": "main_web"}
        try:
            response = self.session.post(url, data=params, timeout=15)
            data = response.json()
            if data.get("code") == 0:
                response_data = data.get("data", {})
                new_refresh_token = response_data.get("refresh_token")
                if new_refresh_token:
                    self.refresh_token = new_refresh_token
                if response.cookies:
                    for k, v in response.cookies.items():
                        self.session.cookies.set(k, v)
                self.csrf_token = self._get_csrf_from_cookie()
                return True, {"message": "刷新成功", "new_refresh_token": new_refresh_token, "cookies": dict(self.session.cookies)}
            return False, {"message": data.get("message", "刷新失败")}
        except Exception as e:
            return False, {"message": str(e)}

    def verify_cookie(self) -> Tuple[bool, dict]:
        sessdata = self.session.cookies.get("SESSDATA")
        bili_jct = self.session.cookies.get("bili_jct")
        if not sessdata or not bili_jct:
            return False, {"message": "关键Cookie缺失", "code": -1}
        url = "https://api.bilibili.com/x/space/myinfo"
        try:
            response = self.session.get(url, timeout=10)
            data = response.json()
            if data.get("code") == 0:
                user_info = data.get("data", {})
                return True, {"message": "Cookie有效", "user_info": {"mid": user_info.get("mid"), "name": user_info.get("name")}}
            return False, {"message": data.get("message", "验证失败"), "code": data.get("code")}
        except Exception as e:
            return False, {"message": str(e), "code": -999}

    def auto_refresh_if_needed(self) -> Tuple[bool, dict]:
        status = self.check_cookie_status()
        if status.get("need_refresh"):
            success, result = self.refresh_cookie()
            return True, {"success": success, **result}
        return False, {"message": "Cookie状态正常"}

    def get_cookie_str(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.session.cookies.items())

    def save_to_file(self, filename: str = COOKIE_FILE):
        data = {"cookie": dict(self.session.cookies), "refresh_token": self.refresh_token, "timestamp": time.time()}
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_from_file(self, filename: str = COOKIE_FILE) -> bool:
        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.get("cookie", {}).items():
                self.session.cookies.set(k, v)
            self.refresh_token = data.get("refresh_token", "")
            self.csrf_token = self._get_csrf_from_cookie()
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────
#  机器人核心
# ─────────────────────────────────────────────
class BiliCommentBot:
    # 本地 BVID ↔ AID 互转常量（所有实例共享）
    _BV_XOR = 23442827791579
    _BV_MASK = 2251799813685247
    _BV_BASE = 58
    _BV_TABLE = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"

    def __init__(self, config: dict, logger: logging.Logger, socketio=None, on_config_changed=None):
        self.config = config
        self.logger = logger
        self.socketio = socketio  # 可选，用于推送到前端
        self.on_config_changed = on_config_changed  # 配置变更回调，用于持久化

        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=Retry(total=0))
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/92.0.4515.107 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
        ]
        self.referers = [
            "https://www.bilibili.com/",
            "https://search.bilibili.com/",
            "https://space.bilibili.com/",
        ]
        self.update_headers()

        # Cookie 管理器
        self.cookie_manager: Optional[BilibiliCookieManager] = None
        self.csrf_token: Optional[str] = None
        self.last_cookie_refresh_time = 0
        self.cookie_refresh_interval = self.config["bilibili"].get("cookie_refresh_interval", 30) * 60
        self.auto_refresh_cookie = self.config["bilibili"].get("auto_refresh_cookie", True)
        self._init_cookie()

        # 历史记录缓冲（必须在 load_history 之前初始化）
        self._history_buffer: List[dict] = []
        self._history_dirty = False
        self._history_flush_interval = 10  # 每 10 条 flush 一次

        # 历史 & 缓存
        self.processed_comments: set = set()
        self.load_history()
        self._review_lock = threading.Lock()
        self._review_drafts: Dict[str, dict] = {}
        self.load_review_drafts()
        self.cache: dict = {}
        self.cache_expire_time = self.config.get("cache", {}).get("expire_time", 300)

        # 频率控制
        self.last_request_time = 0
        rl = self.config.get("rate_limit", {})
        self.min_request_interval = rl.get("min_request_interval", 2.0)
        self.max_retries = rl.get("max_retries", 3)
        self.retry_delay = rl.get("retry_delay", 5)
        self.consecutive_failures = 0
        self.adaptive_interval = self.min_request_interval

        # 视频缓存
        vc = self.config.get("video_cache", {})
        self.cached_videos: List[dict] = []
        self.last_video_fetch_time = 0
        cache_file_path = vc.get("cache_file", "video_cache.json")
        if DATA_DIR and not os.path.isabs(cache_file_path):
            cache_file_path = os.path.join(DATA_DIR, cache_file_path)
        self.video_cache_file = cache_file_path
        self.video_cache_expire_time = vc.get("expire_time", 43200)
        self.load_video_cache()

        # 运行状态
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 统计
        self.stats = {"total_replied": 0, "start_time": None, "last_check": None}

        # B站频率限制相关错误码
        self.BILI_RATE_LIMIT_CODES = frozenset({-509, -412, -799, 412, 509, 799, 10403})

    # ── SocketIO 推送辅助 ──
    def _emit(self, event: str, data: dict):
        """安全推送事件到前端（SocketIO 可选）"""
        if self.socketio:
            try:
                self.socketio.emit(event, data)
            except Exception:
                pass

    # ── Cookie 初始化 ──
    def _init_cookie(self):
        cookie_str = self.config["bilibili"].get("cookie", "")
        refresh_token = self.config["bilibili"].get("refresh_token", "")
        if cookie_str:
            self.cookie_manager = BilibiliCookieManager(cookie_str, refresh_token, logger=self.logger)
            self.session.cookies.update(self.cookie_manager.session.cookies)
        elif os.path.exists(COOKIE_FILE):
            self.cookie_manager = BilibiliCookieManager(logger=self.logger)
            if self.cookie_manager.load_from_file(COOKIE_FILE):
                self.session.cookies.update(self.cookie_manager.session.cookies)
        if self.cookie_manager:
            self.csrf_token = self.cookie_manager._get_csrf_from_cookie()

    def reload_config(self, config: dict):
        """热更新配置"""
        self.config = config
        self.cookie_refresh_interval = config["bilibili"].get("cookie_refresh_interval", 30) * 60
        self.auto_refresh_cookie = config["bilibili"].get("auto_refresh_cookie", True)
        rl = config.get("rate_limit", {})
        self.min_request_interval = rl.get("min_request_interval", 2.0)
        self.max_retries = rl.get("max_retries", 3)
        self.retry_delay = rl.get("retry_delay", 5)
        self.adaptive_interval = self.min_request_interval
        vc = config.get("video_cache", {})
        self.video_cache_expire_time = vc.get("expire_time", 43200)
        # 刷新缓存设置（旧缓存最终会超时，但 expire_time 需立即生效）
        self.cache_expire_time = config.get("cache", {}).get("expire_time", 300)
        self.cache = {}  # 清空缓存让新配置立即生效
        # 重新初始化 Cookie
        self._init_cookie()

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return False
        self._running = True
        self._stop_event.clear()
        self.stats["start_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.logger.info("机器人已启动")
        self._emit("bot_status", {"running": True})
        return True

    def stop(self):
        if not self._running:
            return False
        self._running = False
        self._stop_event.set()
        self.logger.info("机器人已停止")
        self._emit("bot_status", {"running": False})
        # 刷出历史记录和 Cookie
        self._flush_history()
        if self.cookie_manager:
            try:
                self.cookie_manager.save_to_file(COOKIE_FILE)
            except Exception:
                pass
        return True

    def _run_loop(self):
        while self._running:
            self.stats["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                self.process_comments()
            except Exception as e:
                self.logger.error(f"处理评论异常: {e}", exc_info=True)
            self._emit("stats", self.get_stats())
            interval = max(1, int(self.config["bilibili"].get("check_interval", 60)))
            self.logger.info(f"等待 {interval} 秒后进行下次检查")
            # 使用 Event.wait() 可被停止信号立即唤醒，避免循环 sleep
            self._stop_event.wait(timeout=interval)

    def update_headers(self):
        self.session.headers.update({
            "User-Agent": random.choice(self.user_agents),
            "Referer": random.choice(self.referers),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        })

    # ── APP 端参数常量 ──
    _APP_COMMON_PARAMS = {
        "build": "2001100",
        "version": "2.0.1",
        "mobi_app": "android_hd",
        "platform": "android",
        "channel": "master",
        "c_locale": "zh_CN",
        "s_locale": "zh_CN",
        "statistics": '{"appId":5,"platform":3,"version":"2.0.1","abtest":""}',
        "qn": "80",
    }

    _APP_USER_AGENTS = [
        "Mozilla/5.0 BiliDroid/8.43.0 (bbcallen@gmail.com) os/android model/android mobi_app/android build/8430300 channel/master innerVer/8430300 osVer/15 network/2",
        "Mozilla/5.0 BiliDroid/8.42.0 (bbcallen@gmail.com) os/android model/android mobi_app/android build/8420300 channel/master innerVer/8420300 osVer/14 network/2",
        "Mozilla/5.0 BiliDroid/8.43.0 (bbcallen@gmail.com) os/android model/android_hd mobi_app/android_hd build/2001100 channel/master innerVer/2001100 osVer/15 network/2",
    ]

    _APP_BASE_HEADERS = {
        "env": "prod",
        "app-key": "android64",
        "x-bili-aurora-zone": "sh001",
        "bili-http-engine": "cronet",
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    def _make_app_headers(self) -> dict:
        headers = dict(self._APP_BASE_HEADERS)
        headers["User-Agent"] = random.choice(self._APP_USER_AGENTS)
        return headers

    def _app_sign(self, params: dict) -> dict:
        signed = dict(params)
        signed["appkey"] = "dfca71928277209b"
        signed["ts"] = str(int(time.time()))
        sorted_keys = sorted(signed.keys())
        raw = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(signed[k]), safe='')}"
            for k in sorted_keys
        )
        appsec = "b5475a8825547a4fc26c7d518eaaa02e"
        signed["sign"] = hashlib.md5((raw + appsec).encode()).hexdigest()
        return signed

    # ── 缓存 ──
    def get_cache_key(self, url: str, params: dict = None) -> str:
        cache_data = f"{url}_{str(sorted(params.items()) if params else '')}"
        return hashlib.md5(cache_data.encode()).hexdigest()

    def get_from_cache(self, key: str) -> Optional[dict]:
        if key in self.cache:
            data, ts = self.cache[key]
            if time.time() - ts < self.cache_expire_time:
                return data
            del self.cache[key]
        return None

    def set_cache(self, key: str, data: dict):
        self.cache[key] = (data, time.time())

    # ── 请求带频率控制 ──
    def _is_bili_rate_limited(self, response) -> bool:
        if response.status_code == 429:
            return True
        try:
            ct = response.headers.get("Content-Type", "")
            if "json" not in ct:
                return False
            data = response.json()
            code = data.get("code", 0)
            if code in self.BILI_RATE_LIMIT_CODES:
                return True
            msg = data.get("message", "")
            if isinstance(msg, str) and ("过于频繁" in msg or "请求过于频繁" in msg or "访问被拒绝" in msg):
                return True
        except Exception:
            pass
        return False

    def rate_limit_request(self):
        current_time = time.time()
        elapsed = current_time - self.last_request_time
        if self.consecutive_failures > 0:
            self.adaptive_interval = min(
                self.min_request_interval * (2 ** self.consecutive_failures),
                self.min_request_interval * 10,
            )
        else:
            self.adaptive_interval = self.min_request_interval
        if elapsed < self.adaptive_interval:
            sleep_time = self.adaptive_interval - elapsed + random.uniform(0, 1.0)
            time.sleep(sleep_time)
        self.last_request_time = time.time()
        self.update_headers()

    def make_request_with_retry(self, method: str, url: str, use_cache: bool = True, **kwargs) -> Optional[requests.Response]:
        if use_cache and method.upper() == "GET":
            cache_key = self.get_cache_key(url, kwargs.get("params"))
            cached = self.get_from_cache(cache_key)
            if cached:
                return CachedResponse(cached)

        for attempt in range(self.max_retries):
            try:
                self.rate_limit_request()
                response = self.session.request(method, url, timeout=15, **kwargs)
                if self._is_bili_rate_limited(response):
                    self.consecutive_failures += 1
                    if attempt < self.max_retries - 1:
                        retry_after = response.headers.get("Retry-After", "")
                        if retry_after.isdigit():
                            wait = int(retry_after)
                        else:
                            wait = max(self.retry_delay * (2 ** attempt), self.min_request_interval * (2 + attempt))
                        wait += random.uniform(0, 2)
                        self.logger.warning(
                            f"请求频率限制 [{url}], 等待 {wait:.1f}s 后重试 "
                            f"(attempt {attempt + 1}/{self.max_retries}, "
                            f"failures: {self.consecutive_failures})"
                        )
                        time.sleep(wait)
                        continue
                elif response.status_code >= 500:
                    self.consecutive_failures += 1
                    if attempt < self.max_retries - 1:
                        wait = self.retry_delay * (2 ** attempt) + random.uniform(0, 2)
                        time.sleep(wait)
                        continue
                else:
                    self.consecutive_failures = 0
                if not response.text:
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                        continue
                    return None
                if use_cache and method.upper() == "GET" and response.status_code == 200:
                    try:
                        data = response.json()
                        self.set_cache(self.get_cache_key(url, kwargs.get("params")), data)
                    except Exception:
                        pass
                return response
            except requests.exceptions.RequestException as e:
                self.consecutive_failures += 1
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2 ** attempt) + random.uniform(0, 2))
                    continue
                self.logger.error(f"请求失败: {e}")
                return None
        return None

    # ── 历史记录 ──
    def load_history(self):
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
                self.processed_comments = set(item.get("comment_id") for item in history)
                self._history_buffer = history
                self.logger.info(f"加载历史记录，已处理 {len(self.processed_comments)} 条评论")
        except Exception as e:
            self.logger.error(f"加载历史记录失败: {e}")
            self.processed_comments = set()
            self._history_buffer = []

    def save_history(self, comment: Comment, reply_content: str):
        """追加到内存缓冲区，满 N 条后刷到磁盘"""
        try:
            item = {
                "comment_id": comment.comment_id,
                "content": comment.content,
                "user": comment.user,
                "uid": comment.uid,
                "time": comment.time,
                "reply_time": int(time.time()),
                "reply_content": reply_content,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._history_buffer.append(item)
            self._history_dirty = True
            self._emit("new_history", item)

            if len(self._history_buffer) % self._history_flush_interval == 0:
                self._flush_history()
        except Exception as e:
            self.logger.error(f"保存历史记录失败: {e}")

    def _flush_history(self):
        """将内存缓冲区写入磁盘（批量写，减少 I/O）"""
        if not self._history_dirty:
            return
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self._history_buffer, f, ensure_ascii=False, indent=2)
            self._history_dirty = False
        except Exception as e:
            self.logger.error(f"刷出历史记录失败: {e}")

    def get_history(self) -> list:
        """返回内存中的完整历史记录（比读文件快）"""
        return self._history_buffer

    # ── 人工审核草稿 ──
    def load_review_drafts(self):
        try:
            if os.path.exists(REVIEW_DRAFTS_FILE):
                with open(REVIEW_DRAFTS_FILE, "r", encoding="utf-8") as f:
                    items = json.load(f)
                if isinstance(items, list):
                    self._review_drafts = {
                        str(item["comment_id"]): item
                        for item in items
                        if isinstance(item, dict) and item.get("comment_id")
                    }
            self.logger.info(f"加载审核草稿 {len(self._review_drafts)} 条")
        except Exception as e:
            self.logger.error(f"加载审核草稿失败: {e}")
            self._review_drafts = {}

    def _save_review_drafts(self):
        directory = os.path.dirname(REVIEW_DRAFTS_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp_file = f"{REVIEW_DRAFTS_FILE}.tmp"
        items = sorted(
            self._review_drafts.values(),
            key=lambda item: item.get("comment_time", 0),
            reverse=True,
        )
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, REVIEW_DRAFTS_FILE)

    def get_review_drafts(self) -> list:
        with self._review_lock:
            return sorted(
                [dict(item) for item in self._review_drafts.values()],
                key=lambda item: item.get("comment_time", 0),
                reverse=True,
            )

    def set_review_approval(self, comment_id: str, approved: bool) -> dict:
        with self._review_lock:
            draft = self._review_drafts.get(str(comment_id))
            if not draft:
                raise KeyError("草稿不存在")
            if not draft.get("should_reply"):
                raise ValueError("豆包已判断该评论不建议回复")
            if draft.get("status") == "sent":
                raise ValueError("该回复已经发送")
            if draft.get("status") == "regenerating":
                raise ValueError("豆包正在重新生成该回复")
            draft["approved"] = bool(approved)
            draft["status"] = "approved" if approved else "pending"
            self._save_review_drafts()
            return dict(draft)

    def regenerate_review_draft(self, comment_id: str) -> dict:
        """使用当前最新配置重新生成单条候选回复，不访问 B站接口。"""
        comment_id = str(comment_id)
        with self._review_lock:
            current = self._review_drafts.get(comment_id)
            if not current:
                raise KeyError("草稿不存在")
            if current.get("status") == "sent":
                raise ValueError("已发送的回复不能重新生成")
            if current.get("status") == "regenerating":
                raise ValueError("该回复已经在重新生成")
            original = dict(current)
            current["approved"] = False
            current["status"] = "regenerating"
            self._save_review_drafts()

        comment = Comment(
            comment_id=comment_id,
            content=str(original.get("comment") or ""),
            user=str(original.get("author") or ""),
            uid=str(original.get("author_uid") or ""),
            time=int(original.get("comment_time") or 0),
            parent_id=original.get("parent_id"),
            root_id=original.get("root_id"),
            depth=int(original.get("depth") or 0),
        )
        parent = None
        if original.get("parent_comment"):
            parent = Comment(
                comment_id=str(original.get("parent_id") or original.get("root_id") or "context"),
                content=str(original["parent_comment"]),
                user=str(original.get("parent_author") or "上级评论"),
                uid="",
                time=0,
            )

        item = {
            "bvid": original.get("bvid", ""),
            "oid": original.get("oid", ""),
            "comment_type": original.get("comment_type", 1),
            "video_title": original.get("video_title", ""),
            "video_desc": "",
            "comment": comment,
            "context": [parent] if parent else [],
            "parent_comment": parent,
            "is_follow_up": bool(original.get("root_id") or original.get("depth")),
            "regenerate": True,
            "previous_reply": str(original.get("reply") or ""),
        }

        try:
            decision = self.generate_distinct_reply_decision(item)
            should_reply = bool(decision.get("should_reply") and decision.get("reply"))
            with self._review_lock:
                current = self._review_drafts.get(comment_id)
                if not current:
                    raise KeyError("草稿不存在")
                if current.get("status") == "sent":
                    raise ValueError("回复已在重新生成期间发送，不能覆盖")
                current.update({
                    "should_reply": should_reply,
                    "reply": decision.get("reply", "") if should_reply else "",
                    "reason": decision.get("reason", ""),
                    "model": decision.get("model", ""),
                    "approved": False,
                    "status": "pending" if should_reply else "skipped",
                    "regenerated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                self._save_review_drafts()
                result = dict(current)
        except Exception:
            with self._review_lock:
                current = self._review_drafts.get(comment_id)
                if current and current.get("status") == "regenerating":
                    self._review_drafts[comment_id] = original
                    self._save_review_drafts()
            raise

        self._emit("review_updated", {"regenerated": comment_id})
        return result

    # ── 视频缓存 ──
    def load_video_cache(self):
        try:
            if os.path.exists(self.video_cache_file):
                with open(self.video_cache_file, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
                if isinstance(cache_data, dict):
                    self.cached_videos = cache_data.get("videos", [])
                    self.last_video_fetch_time = cache_data.get("fetch_time", 0)
                elif isinstance(cache_data, list):
                    self.cached_videos = cache_data
                    self.last_video_fetch_time = 0
                else:
                    self.cached_videos = []
                    self.last_video_fetch_time = 0
                age_h = (time.time() - self.last_video_fetch_time) / 3600
                self.logger.info(f"加载视频缓存，缓存{age_h:.1f}小时，共{len(self.cached_videos)}个视频")
        except Exception as e:
            self.logger.error(f"加载视频缓存失败: {e}")
            self.cached_videos = []

    def save_video_cache(self, videos: List[dict]):
        try:
            with open(self.video_cache_file, "w", encoding="utf-8") as f:
                json.dump({
                    "videos": videos,
                    "fetch_time": int(time.time()),
                    "fetch_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"保存视频缓存失败: {e}")

    def get_video_list(self) -> List[dict]:
        uid = self.config["bilibili"].get("uid")
        if not uid:
            self.logger.error("未配置uid")
            return []
        current_time = time.time()
        if self.cached_videos and (current_time - self.last_video_fetch_time) < self.video_cache_expire_time:
            self.logger.info(f"使用视频缓存，共{len(self.cached_videos)}个")
            return self.cached_videos
        self.logger.info("重新获取视频列表（APP API）...")
        max_pn = self.config["bilibili"].get("max_video_pages", 5)
        all_videos = []
        pn = 1
        url = "https://app.bilibili.com/x/v2/space/archive/cursor"
        inter_page_delay_base = max(self.min_request_interval * 1.2, 3.0)
        while pn <= max_pn:
            params = self._app_sign({"vmid": uid, "ps": 20, "pn": pn, "order": "pubdate", "sort": "desc", **self._APP_COMMON_PARAMS})
            try:
                if pn > 1:
                    extra_delay = inter_page_delay_base + random.uniform(0, 1.5)
                    self.logger.debug(f"视频列表页间延迟 {extra_delay:.1f}s（第{pn}页）")
                    time.sleep(extra_delay)
                response = self.make_request_with_retry(
                    "GET", url, params=params, use_cache=False,
                    headers=self._make_app_headers(),
                )
                if not response:
                    break
                data = response.json()
                if data.get("code") == 0:
                    items = data.get("data", {}).get("item", [])
                    if not items:
                        break
                    for item in items:
                        stat = item.get("stat") or {}
                        all_videos.append({
                            "bvid": item.get("bvid", ""),
                            "title": item.get("title", ""),
                            "desc": item.get("description") or item.get("title", ""),
                            "play": item.get("play") or stat.get("view", 0),
                            "comment": item.get("comment") or stat.get("reply", 0),
                        })
                    self.logger.info(f"第{pn}页获取到{len(items)}个视频，累计{len(all_videos)}个")
                    has_next = data.get("data", {}).get("has_next", True)
                    if not has_next or len(items) < 20:
                        break
                    pn += 1
                else:
                    error_code = data.get("code", 0)
                    error_msg = data.get("message", "")
                    self.logger.error(f"获取视频列表第{pn}页失败: code={error_code} msg={error_msg}")
                    if error_code in self.BILI_RATE_LIMIT_CODES or "过于频繁" in str(error_msg):
                        if all_videos:
                            self.logger.warning(
                                f"视频列表获取被频率限制，保留已取得的 {len(all_videos)} 个视频",
                            )
                            self._partial_save_video_cache(all_videos, current_time)
                        else:
                            self.logger.warning("视频列表被频率限制且无已获取数据，将使用过期缓存")
                        break
                    break
            except Exception as e:
                self.logger.error(f"获取视频列表异常: {e}")
                break
        if all_videos:
            self.cached_videos = all_videos
            self.last_video_fetch_time = current_time
            self.save_video_cache(all_videos)
            self._emit("video_list", {"count": len(all_videos), "videos": all_videos[:20]})
            return all_videos
        if self.cached_videos:
            cache_age_h = (current_time - self.last_video_fetch_time) / 3600
            self.logger.warning(
                f"获取视频列表失败，回退到过期缓存（{cache_age_h:.1f}小时前，{len(self.cached_videos)}个视频）"
            )
        return self.cached_videos

    def _partial_save_video_cache(self, videos: List[dict], fetch_time: float):
        try:
            self.cached_videos = videos
            self.last_video_fetch_time = fetch_time
            self.save_video_cache(videos)
            self._emit("video_list", {"count": len(videos), "videos": videos[:20]})
        except Exception as e:
            self.logger.error(f"保存部分视频缓存失败: {e}")

    # ── BVID ↔ AID 互转 ──
    _BV_REVERSE = {c: i for i, c in enumerate(_BV_TABLE)}

    def bvid_to_aid(self, bvid: str) -> str:
        if not bvid or not bvid.startswith("BV"):
            return ""
        try:
            bvid_arr = list(bvid[3:])
            bvid_arr[0], bvid_arr[6] = bvid_arr[6], bvid_arr[0]
            bvid_arr[1], bvid_arr[4] = bvid_arr[4], bvid_arr[1]
            tmp = 0
            for char in bvid_arr:
                idx = self._BV_REVERSE.get(char)
                if idx is None:
                    return ""
                tmp = tmp * self._BV_BASE + idx
            return str((tmp & self._BV_MASK) ^ self._BV_XOR)
        except Exception:
            return ""

    # ── 评论获取 ──
    def get_video_comments(self, bvid: str) -> List[Comment]:
        url = "https://api.bilibili.com/x/v2/reply"
        aid = self.bvid_to_aid(bvid)
        if not aid:
            return []

        chained_reply_enabled = self.config["reply"].get("chained_reply_enabled", True)
        max_reply_depth = self.config["reply"].get("max_reply_depth", 3)

        all_comments = []
        seen_ids = set()
        pn = 1
        max_pn = self.config["bilibili"].get("max_comment_pages", 10)
        page_size = 20

        while pn <= max_pn:
            params = {"type": 1, "oid": aid, "pn": pn, "ps": page_size, "sort": 2}
            try:
                response = self.make_request_with_retry("GET", url, params=params)
                if not response:
                    break
                data = response.json()
                if data.get("code") == 0:
                    replies = data.get("data", {}).get("replies", [])
                    if not replies:
                        break

                    for r in replies:
                        cid = str(r["rpid"])
                        if cid in seen_ids:
                            continue
                        seen_ids.add(cid)

                        main_comment = Comment(
                            comment_id=cid,
                            content=r["content"]["message"],
                            user=r["member"]["uname"],
                            uid=str(r["member"]["mid"]),
                            time=r["ctime"],
                            depth=0,
                        )
                        all_comments.append(main_comment)

                        if chained_reply_enabled:
                            self.logger.debug(f"检查评论 {main_comment.comment_id} 的子评论...")
                            child_replies = self.get_comment_replies(
                                bvid,
                                main_comment.comment_id,
                                max_depth=max_reply_depth - 1,
                            )
                            if child_replies:
                                filtered_children = [c for c in child_replies if c.comment_id not in seen_ids]
                                for c in filtered_children:
                                    seen_ids.add(c.comment_id)
                                self.logger.info(
                                    f"评论 {main_comment.comment_id} 有 {len(child_replies)} 条子评论"
                                    f"（去重后 {len(filtered_children)} 条）"
                                )
                                all_comments.extend(filtered_children)
                                main_comment.children = filtered_children

                    if len(replies) < page_size:
                        break
                    pn += 1
                else:
                    err = data.get("message", "")
                    if "ps out of bounds" in err and pn == 1 and page_size > 10:
                        page_size = 10
                        continue
                    break
            except Exception as e:
                self.logger.error(f"获取评论异常: {e}")
                break

        if chained_reply_enabled:
            main_count = sum(1 for c in all_comments if c.depth == 0)
            child_count = len(all_comments) - main_count
            self.logger.info(f"共获取 {main_count} 条主评论和 {child_count} 条子评论")

        return all_comments

    def get_comment_replies(self, bvid: str, root_comment_id: str, max_depth: int = 2, current_depth: int = 1) -> List[Comment]:
        if current_depth > max_depth:
            return []

        url = "https://api.bilibili.com/x/v2/reply/reply"
        aid = self.bvid_to_aid(bvid)
        if not aid:
            return []

        all_replies = []
        pn = 1
        page_size = 10

        while True:
            params = {"type": 1, "oid": aid, "root": root_comment_id, "pn": pn, "ps": page_size}
            try:
                response = self.make_request_with_retry("GET", url, params=params)
                if not response:
                    break

                data = response.json()
                if data.get("code") != 0:
                    break

                replies_data = data.get("data", {}).get("replies", [])
                if not replies_data:
                    break

                for r in replies_data:
                    child_comment = Comment(
                        comment_id=str(r["rpid"]),
                        content=r["content"]["message"],
                        user=r["member"]["uname"],
                        uid=str(r["member"]["mid"]),
                        time=r["ctime"],
                        parent_id=root_comment_id,
                        root_id=root_comment_id,
                        depth=current_depth,
                    )

                    if current_depth < max_depth:
                        grandchildren = self.get_comment_replies(
                            bvid,
                            child_comment.comment_id,
                            max_depth,
                            current_depth + 1,
                        )
                        child_comment.children = grandchildren

                    all_replies.append(child_comment)

                page_info = data.get("data", {}).get("page", {})
                if page_info.get("count", 0) <= pn * page_size:
                    break
                pn += 1

            except Exception as e:
                self.logger.error(f"获取子评论异常: {e}")
                break

        return all_replies

    @staticmethod
    def _creator_reply_to_comment(reply: dict) -> Comment:
        root_id = str(reply.get("root") or "") or None
        parent_id = str(reply.get("parent") or "") or None
        member = reply.get("member") or {}
        content = reply.get("content") or {}
        return Comment(
            comment_id=str(reply.get("rpid") or ""),
            content=str(content.get("message") or ""),
            user=str(member.get("uname") or ""),
            uid=str(member.get("mid") or reply.get("mid") or ""),
            time=int(reply.get("ctime") or 0),
            replied=bool((reply.get("up_action") or {}).get("reply")),
            parent_id=parent_id,
            root_id=root_id,
            depth=1 if root_id else 0,
        )

    def get_creator_comment_feed(self, limit: int, since_timestamp: int = None) -> List[dict]:
        """读取创作中心“评论管理”的账号最新评论流。"""
        url = "https://api.bilibili.com/x/v2/reply/up/fulllist"
        page_size = 10
        max_pages = max(1, (limit + page_size - 1) // page_size)
        all_items = []
        seen_ids = set()
        reached_since = False

        for pn in range(1, max_pages + 1):
            params = {
                "order": 1,
                "filter": -1,
                "is_hidden": 0,
                "type": 1,
                "pn": pn,
                "ps": page_size,
                "charge_plus_filter": False,
            }
            response = self.make_request_with_retry(
                "GET",
                url,
                params=params,
                use_cache=False,
                headers={"Referer": "https://member.bilibili.com/platform/comment/article"},
            )
            if not response:
                if pn == 1:
                    raise RuntimeError("创作中心评论列表请求无响应")
                break

            payload = response.json()
            if payload.get("code") != 0:
                message = payload.get("message", "未知错误")
                if pn == 1:
                    raise RuntimeError(f"创作中心评论列表获取失败: {message}")
                self.logger.warning("创作中心评论第%s页获取失败: %s", pn, message)
                break

            data = payload.get("data") or {}
            replies = data.get("list") or []
            if not replies:
                break

            for reply in replies:
                comment = self._creator_reply_to_comment(reply)
                if since_timestamp is not None and comment.time < since_timestamp:
                    reached_since = True
                    break
                if not comment.comment_id or comment.comment_id in seen_ids:
                    continue
                seen_ids.add(comment.comment_id)

                parent_data = reply.get("parent_info") or {}
                if not parent_data and comment.parent_id:
                    root_data = reply.get("root_info") or {}
                    if str(root_data.get("rpid") or "") == comment.parent_id:
                        parent_data = root_data
                parent_comment = (
                    self._creator_reply_to_comment(parent_data)
                    if parent_data and parent_data.get("rpid")
                    else None
                )

                all_items.append({
                    "bvid": str(reply.get("bvid") or ""),
                    "oid": str(reply.get("oid") or ""),
                    "comment_type": int(reply.get("type") or 1),
                    "video_title": str(reply.get("title") or ""),
                    "video_desc": "",
                    "comment": comment,
                    "parent_comment": parent_comment,
                })
                if len(all_items) >= limit:
                    break

            page = data.get("page") or {}
            total = int(page.get("total") or 0)
            self.logger.info(
                "创作中心最新评论第%s页获取到%s条，累计%s条",
                pn,
                len(replies),
                len(all_items),
            )
            if (
                reached_since
                or len(all_items) >= limit
                or len(replies) < page_size
                or (total and pn * page_size >= total)
            ):
                break

        return all_items

    @staticmethod
    def _parse_review_since(value: str) -> Optional[int]:
        value = str(value or "").strip()
        if not value:
            return None
        try:
            return int(datetime.fromisoformat(value.replace("T", " ")).timestamp())
        except (ValueError, OSError, OverflowError) as exc:
            raise ValueError("起始时间格式无效，请使用 YYYY-MM-DD HH:MM") from exc

    # ── 豆包回复生成 ──
    def _ark_output_text(self, payload: dict) -> str:
        parts = []
        for output in payload.get("output", []) or []:
            for part in output.get("content", []) or []:
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
        return "".join(parts).strip()

    def _parse_json_text(self, text: str):
        cleaned = text.strip()
        fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", cleaned, re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        return json.loads(cleaned)

    def generate_reply_decisions(self, items: List[dict]) -> List[dict]:
        if not items:
            return []

        api_config = self.config.get("ark", {})
        api_key = (
            os.environ.get("ARK_API_KEY")
            or os.environ.get("VOLCENGINE_ARK_API_KEY")
            or api_config.get("api_key", "")
        )
        if not api_key:
            raise RuntimeError("未配置 ARK_API_KEY")

        model = api_config.get("model", DEFAULT_CONFIG["ark"]["model"])
        system_prompt = api_config.get("system_prompt", DEFAULT_CONFIG["ark"]["system_prompt"])
        input_items = []
        for item in items:
            comment = item["comment"]
            context = item.get("context") or []
            input_items.append({
                "id": str(comment.comment_id),
                "video_title": item.get("video_title", ""),
                "author": comment.user,
                "comment": comment.content,
                "is_follow_up": bool(item.get("is_follow_up") or comment.root_id),
                "regenerate": bool(item.get("regenerate")),
                "previous_reply": str(item.get("previous_reply") or ""),
                "avoid_replies": [
                    str(reply)
                    for reply in (item.get("avoid_replies") or [])
                    if str(reply).strip()
                ],
                "parent_context": [
                    {"author": parent.user, "comment": parent.content}
                    for parent in context
                ],
            })

        prompt = f"""{system_prompt}

请处理下面这批评论。

判断规则：
1. 只有容易理解、容易自然回应、不容易犯错的评论才 should_reply=true。
2. 上下文不足、事实争议大、容易引战、敏感、纯辱骂、只能写万能套话的评论，should_reply=false。
3. 不为了数量硬回。短评论如果能自然接梗，也可以回复。
4. is_follow_up=true 表示观众是在继续一段已有对话。只有追问带来新问题、新信息或确实值得继续的互动点时才回复；纯“谢谢/收到/哈哈”、表情、重复上一句、无新内容的附和必须跳过。
5. 不要为了追平对话而回复每一条追评。拿不准是否值得继续时 should_reply=false。
6. reply 必须是可直接发出的豆包原文，通常不超过60个汉字；不要加“回复：”、引号、分析或备选项。
7. 不要编造视频和评论里没有的事实。
8. regenerate=true 表示用户不满意旧回复。必须重新组织表达，不能与 previous_reply 或 avoid_replies 中的内容相同，也不能只替换标点、语气词或少量近义词。

只输出严格 JSON 数组：
[{{"id":"评论id","should_reply":true,"reply":"直接回复正文","reason":"简短判断"}}]
不建议回复时 reply 必须为空字符串。

评论数据：
{json.dumps(input_items, ensure_ascii=False)}
"""
        request_payload = {
            "model": model,
            "input": [{
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }],
            "reasoning": {"effort": "low"},
            "max_output_tokens": int(api_config.get("max_tokens", 2400)),
        }
        proxy_url = os.environ.get("ARK_PROXY_URL", "").strip()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        response = requests.post(
            api_config.get("base_url", DEFAULT_CONFIG["ark"]["base_url"]),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=request_payload,
            timeout=90,
            proxies=proxies,
        )
        response.raise_for_status()
        output_text = self._ark_output_text(response.json())
        if not output_text:
            raise RuntimeError("豆包没有返回文本")
        parsed = self._parse_json_text(output_text)
        if not isinstance(parsed, list):
            raise RuntimeError("豆包返回格式不是 JSON 数组")

        known_ids = {str(item["comment"].comment_id) for item in items}
        results = []
        for decision in parsed:
            decision_id = str(decision.get("id", ""))
            if decision_id not in known_ids:
                continue
            should_reply = decision.get("should_reply") is True
            reply = str(decision.get("reply", "")).strip() if should_reply else ""
            results.append({
                "id": decision_id,
                "should_reply": should_reply and bool(reply),
                "reply": reply,
                "reason": str(decision.get("reason", "")).strip(),
                "model": model,
            })
        return results

    @staticmethod
    def _normalized_reply_text(text: str) -> str:
        return re.sub(r"[\W_]+", "", str(text or ""), flags=re.UNICODE).lower()

    def generate_distinct_reply_decision(self, item: dict) -> dict:
        """重新生成时最多调用豆包两次，确保不是原样返回。"""
        previous_reply = str(item.get("previous_reply") or "")
        avoid_replies = [previous_reply] if previous_reply else []

        for _ in range(2):
            request_item = dict(item)
            request_item["regenerate"] = True
            request_item["previous_reply"] = previous_reply
            request_item["avoid_replies"] = list(avoid_replies)
            decisions = self.generate_reply_decisions_resilient([request_item])
            decision = decisions[0] if decisions else None
            if not decision or not decision.get("should_reply"):
                return decision or {
                    "id": str(item["comment"].comment_id),
                    "should_reply": False,
                    "reply": "",
                    "reason": "豆包未返回该条，暂不回复",
                    "model": self.config.get("ark", {}).get(
                        "model",
                        DEFAULT_CONFIG["ark"]["model"],
                    ),
                }

            new_reply = str(decision.get("reply") or "")
            normalized_new = self._normalized_reply_text(new_reply)
            if normalized_new and all(
                normalized_new != self._normalized_reply_text(old_reply)
                for old_reply in avoid_replies
            ):
                return decision
            if new_reply:
                avoid_replies.append(new_reply)

        raise RuntimeError("豆包连续两次返回相同回复，旧回复已保留，请稍后再试")

    def generate_reply_decisions_resilient(self, items: List[dict]) -> List[dict]:
        """豆包偶发返回坏 JSON 时串行拆小批次，避免一条坏结果拖垮整批。"""
        try:
            return self.generate_reply_decisions(items)
        except json.JSONDecodeError as exc:
            self.logger.warning(
                "豆包返回 JSON 不完整，当前批次 %s 条，准备串行拆分: %s",
                len(items),
                exc,
            )
            if len(items) <= 1:
                comment_id = str(items[0]["comment"].comment_id)
                return [{
                    "id": comment_id,
                    "should_reply": False,
                    "reply": "",
                    "reason": "豆包返回格式异常，暂不回复",
                    "model": self.config.get("ark", {}).get(
                        "model",
                        DEFAULT_CONFIG["ark"]["model"],
                    ),
                }]

            middle = len(items) // 2
            return (
                self.generate_reply_decisions_resilient(items[:middle])
                + self.generate_reply_decisions_resilient(items[middle:])
            )

    def generate_reply(self, comment: str, context: List[Comment] = None, video_title: str = None, video_desc: str = None) -> Optional[str]:
        placeholder = Comment(
            comment_id="single",
            content=comment,
            user="观众",
            uid="",
            time=int(time.time()),
        )
        decisions = self.generate_reply_decisions([{
            "comment": placeholder,
            "context": context or [],
            "video_title": video_title or "",
            "video_desc": video_desc or "",
        }])
        return decisions[0]["reply"] if decisions and decisions[0]["should_reply"] else None

    # ── 评论点赞 ──
    def like_comment(self, bvid: str, comment_id: str) -> bool:
        if self.cookie_manager:
            self.csrf_token = self.cookie_manager._get_csrf_from_cookie()
        if not self.csrf_token:
            return False
        url = "https://api.bilibili.com/x/v2/reply/action"
        aid = self.bvid_to_aid(bvid)
        data = {"type": 1, "oid": aid, "rpid": comment_id, "action": 1, "csrf": self.csrf_token}
        try:
            response = self.make_request_with_retry("POST", url, data=data)
            if not response:
                return False
            result = response.json()
            return result.get("code") == 0
        except Exception:
            return False

    # ── 获取用户最新视频（APP API） ──
    def get_user_latest_video(self, uid: str) -> Optional[dict]:
        self.logger.debug(f"开始获取用户 {uid} 的最新视频...")
        url = "https://app.bilibili.com/x/v2/space/archive/cursor"
        params = self._app_sign({"vmid": uid, "ps": 1, "pn": 1, "order": "pubdate", "sort": "desc", **self._APP_COMMON_PARAMS})
        try:
            response = self.make_request_with_retry(
                "GET", url, params=params, use_cache=False,
                headers=self._make_app_headers(),
            )
            if not response:
                self.logger.warning(f"获取用户 {uid} 视频列表失败：请求无响应")
                return None
            data = response.json()
            if data.get("code") == 0:
                items = data.get("data", {}).get("item", [])
                if items:
                    video = items[0]
                    stat = video.get("stat") or {}
                    result = {
                        "bvid": video.get("bvid", ""),
                        "title": video.get("title", ""),
                        "play": video.get("play") or stat.get("view", 0),
                        "comment": video.get("comment") or stat.get("reply", 0),
                    }
                    self.logger.debug(
                        f"成功获取用户 {uid} 的最新视频: {result.get('title', 'N/A')} ({result.get('bvid', 'N/A')})"
                    )
                    return result
                self.logger.warning(f"用户 {uid} 没有视频")
            else:
                error_code = data.get("code", 0)
                error_msg = data.get("message", "")
                if error_code in self.BILI_RATE_LIMIT_CODES or "过于频繁" in str(error_msg):
                    self.logger.warning(f"获取用户 {uid} 视频列表被频率限制: code={error_code} msg={error_msg}")
                else:
                    self.logger.warning(f"获取用户 {uid} 视频列表失败: code={error_code} msg={error_msg}")
            return None
        except Exception as e:
            self.logger.error(f"获取用户 {uid} 最新视频异常: {e}", exc_info=True)
            return None

    def like_video(self, bvid: str) -> bool:
        self.logger.debug(f"开始点赞视频: {bvid}")
        aid = self.bvid_to_aid(bvid)
        if not aid:
            self.logger.error(f"点赞视频失败: 无法转换 BVID {bvid} 到 AID")
            return False
        url = "https://app.bilibili.com/x/v2/view/like"
        data = self._app_sign({"aid": aid, "like": "1"})
        try:
            response = self.make_request_with_retry(
                "POST", url, data=data,
                headers=self._make_app_headers(),
            )
            if not response:
                self.logger.warning(f"点赞视频失败: 请求无响应")
                return False
            result = response.json()
            code = result.get("code")
            message = result.get("message", "未知错误")
            if code == 0:
                self.logger.info(f"✓ 成功点赞视频: {bvid}")
                return True
            self.logger.warning(f"点赞视频失败: code={code}, message={message}")
            return False
        except Exception as e:
            self.logger.error(f"点赞视频异常: {e}", exc_info=True)
            return False

    def check_is_follower(self, follower_uid: str, following_uid: str) -> bool:
        self.logger.debug(f"检查用户 {follower_uid} 是否关注 {following_uid}...")
        url = "https://api.bilibili.com/x/relation/same/followers"
        params = {"vmid": following_uid, "mid": follower_uid}
        try:
            response = self.make_request_with_retry("GET", url, params=params, use_cache=False)
            if not response:
                self.logger.warning(f"检查粉丝关系失败: 请求无响应")
                return False
            data = response.json()
            code = data.get("code")
            if code == 0:
                following = data.get("data", {}).get("following", False)
                self.logger.debug(f"用户 {follower_uid} 关注状态: {following}")
                return following
            message = data.get("message", "未知错误")
            self.logger.warning(f"检查粉丝关系失败: code={code}, message={message}")
            return False
        except Exception as e:
            self.logger.error(f"检查粉丝关系异常: {e}", exc_info=True)
            return False

    def reply_comment(
        self,
        bvid: str,
        comment_id: str,
        content: str,
        root_id: str = None,
        parent_id: str = None,
        oid: str = None,
        comment_type: int = 1,
    ) -> bool:
        if self.cookie_manager:
            self.csrf_token = self.cookie_manager._get_csrf_from_cookie()
        if not self.csrf_token:
            self.logger.error("未找到CSRF token")
            return False
        if self.cookie_manager:
            is_valid, result = self.cookie_manager.verify_cookie()
            if not is_valid:
                self.logger.error(f"Cookie无效: {result.get('message')}")
                return False

        url = "https://api.bilibili.com/x/v2/reply/add"
        aid = str(oid or self.bvid_to_aid(bvid))
        if not aid:
            self.logger.error(f"无法确定评论所属稿件: comment_id={comment_id}")
            return False
        prefix = self.config["reply"].get("prefix", "")

        root = root_id if root_id else comment_id
        parent = parent_id if parent_id else comment_id

        data = {
            "type": int(comment_type or 1),
            "oid": aid,
            "root": root,
            "parent": parent,
            "message": f"{prefix}{content}",
            "csrf": self.csrf_token,
        }

        reply_type = "楼中楼回复" if root_id else "主评论回复"
        self.logger.debug(
            f"{reply_type}: bvid={bvid}, oid={aid}, type={comment_type}, "
            f"root={root}, parent={parent}, comment_id={comment_id}"
        )

        try:
            response = self.make_request_with_retry("POST", url, data=data)
            if not response:
                return False
            result = response.json()
            if result.get("code") == 0:
                self.logger.info(f"回复成功: {comment_id} (类型: {reply_type})")
                return True
            self.logger.error(f"回复失败: {result.get('message')}")
            return False
        except Exception as e:
            self.logger.error(f"回复异常: {e}")
            return False

    def refresh_cookie_if_needed(self):
        if not self.cookie_manager or not self.cookie_manager.refresh_token:
            return
        current_time = time.time()
        if current_time - self.last_cookie_refresh_time < self.cookie_refresh_interval:
            return
        need_refresh, result = self.cookie_manager.auto_refresh_if_needed()
        if need_refresh and result.get("success"):
            self.session.cookies.update(self.cookie_manager.session.cookies)
            self.csrf_token = self.cookie_manager._get_csrf_from_cookie()
            new_rt = result.get("new_refresh_token")
            if new_rt:
                self.config["bilibili"]["refresh_token"] = new_rt
                if self.on_config_changed:
                    self.on_config_changed(new_rt)
        self.last_cookie_refresh_time = current_time

    # ── 人工审核处理循环 ──
    def _collect_review_items(self, limit: int, review_since: str = None) -> List[dict]:
        only_bvid = self.config["reply"].get("only_bvid", "").strip()
        context_count = self.config["reply"].get("context_comments_count", 0)
        my_uid = self.config["bilibili"].get("uid", "")
        items = []

        with self._review_lock:
            existing_ids = set(self._review_drafts)

        if not only_bvid:
            since_timestamp = self._parse_review_since(
                self.config["reply"].get("review_since", "")
                if review_since is None
                else review_since
            )
            feed_items = self.get_creator_comment_feed(limit, since_timestamp)
            for feed_item in feed_items:
                if len(items) >= limit:
                    break
                comment = feed_item["comment"]
                if comment.comment_id in self.processed_comments or comment.comment_id in existing_ids:
                    continue
                if comment.replied:
                    continue
                if my_uid and comment.uid == my_uid:
                    continue
                passed, _ = self._check_filters(comment)
                if not passed:
                    continue

                parent_comment = feed_item.get("parent_comment")
                context = [parent_comment] if parent_comment else []
                feed_item["context"] = context
                feed_item["is_follow_up"] = comment.depth > 0
                items.append(feed_item)
            return items

        videos = [{"bvid": only_bvid, "title": f"指定视频({only_bvid})", "desc": ""}]
        for video in videos:
            if len(items) >= limit:
                break
            bvid = video["bvid"]
            comments = self.get_video_comments(bvid)
            for idx, comment in enumerate(comments):
                if len(items) >= limit:
                    break
                if comment.comment_id in self.processed_comments or comment.comment_id in existing_ids:
                    continue
                if my_uid and comment.uid == my_uid:
                    continue
                passed, _ = self._check_filters(comment)
                if not passed:
                    continue

                context = []
                parent_comment = None
                if comment.depth > 0 and comment.parent_id:
                    parent_comment = next(
                        (candidate for candidate in comments if candidate.comment_id == comment.parent_id),
                        None,
                    )
                    if parent_comment:
                        context.append(parent_comment)
                if context_count > 0 and idx > 0:
                    start_idx = max(0, idx - context_count)
                    for previous in comments[start_idx:idx]:
                        if previous.comment_id != comment.parent_id:
                            context.append(previous)

                items.append({
                    "bvid": bvid,
                    "video_title": video.get("title", ""),
                    "video_desc": video.get("desc", ""),
                    "comment": comment,
                    "context": context,
                    "parent_comment": parent_comment,
                    "is_follow_up": comment.depth > 0,
                })
        return items

    def generate_review_drafts(self, limit: int = None, review_since: str = None) -> dict:
        if self.auto_refresh_cookie:
            self.refresh_cookie_if_needed()
        limit = int(limit or self.config["reply"].get("max_process", 10))
        limit = max(1, min(limit, 1000))
        items = self._collect_review_items(limit, review_since=review_since)
        return self._generate_review_drafts(items)

    def _generate_review_drafts(self, items: List[dict]) -> dict:
        if not items:
            return {"generated": 0, "replyable": 0, "skipped": 0}

        # 评论可能很长。按小批次保证 JSON 稳定，不同批次可并发调用豆包。
        batch_size = max(1, min(int(self.config["reply"].get("review_batch_size", 4)), 4))
        batches = [
            items[start:start + batch_size]
            for start in range(0, len(items), batch_size)
        ]
        decisions = {}
        with ThreadPoolExecutor(max_workers=len(batches)) as executor:
            futures = [
                executor.submit(self.generate_reply_decisions_resilient, batch)
                for batch in batches
            ]
            for future in as_completed(futures):
                for decision in future.result():
                    decisions[decision["id"]] = decision

        generated = 0
        replyable = 0
        skipped = 0
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._review_lock:
            for item in items:
                comment = item["comment"]
                decision = decisions.get(str(comment.comment_id), {
                    "should_reply": False,
                    "reply": "",
                    "reason": "豆包未返回该条，暂不回复",
                    "model": self.config.get("ark", {}).get("model", DEFAULT_CONFIG["ark"]["model"]),
                })
                parent = item.get("parent_comment")
                should_reply = bool(decision.get("should_reply") and decision.get("reply"))
                draft = {
                    "comment_id": str(comment.comment_id),
                    "bvid": item["bvid"],
                    "oid": item.get("oid", ""),
                    "comment_type": item.get("comment_type", 1),
                    "video_title": item["video_title"],
                    "author": comment.user,
                    "author_uid": comment.uid,
                    "comment": comment.content,
                    "comment_time": comment.time,
                    "parent_id": comment.parent_id,
                    "root_id": comment.root_id,
                    "depth": comment.depth,
                    "parent_author": parent.user if parent else "",
                    "parent_comment": parent.content if parent else "",
                    "should_reply": should_reply,
                    "reply": decision.get("reply", "") if should_reply else "",
                    "reason": decision.get("reason", ""),
                    "model": decision.get("model", ""),
                    "approved": False,
                    "status": "pending" if should_reply else "skipped",
                    "created_at": now_text,
                }
                self._review_drafts[str(comment.comment_id)] = draft
                generated += 1
                if should_reply:
                    replyable += 1
                else:
                    skipped += 1
            self._save_review_drafts()

        self._emit("review_updated", {
            "generated": generated,
            "replyable": replyable,
            "skipped": skipped,
        })
        return {"generated": generated, "replyable": replyable, "skipped": skipped}

    def send_approved_drafts(self, comment_ids: List[str] = None) -> dict:
        requested = None if comment_ids is None else {str(value) for value in comment_ids}
        with self._review_lock:
            drafts = [
                dict(draft)
                for draft in self._review_drafts.values()
                if draft.get("approved")
                and draft.get("status") == "approved"
                and (requested is None or str(draft["comment_id"]) in requested)
            ]

        sent = 0
        failed = 0
        for draft in drafts:
            comment_id = str(draft["comment_id"])
            ok = self.reply_comment(
                draft["bvid"],
                comment_id,
                draft["reply"],
                root_id=draft.get("root_id") if draft.get("depth", 0) > 0 else None,
                parent_id=draft.get("parent_id") if draft.get("depth", 0) > 0 else None,
                oid=draft.get("oid"),
                comment_type=draft.get("comment_type", 1),
            )
            with self._review_lock:
                current = self._review_drafts[comment_id]
                if ok:
                    current["status"] = "sent"
                    current["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    sent += 1
                    self.processed_comments.add(comment_id)
                    comment = Comment(
                        comment_id=comment_id,
                        content=draft["comment"],
                        user=draft["author"],
                        uid=draft["author_uid"],
                        time=int(draft["comment_time"]),
                        parent_id=draft.get("parent_id"),
                        root_id=draft.get("root_id"),
                        depth=int(draft.get("depth", 0)),
                    )
                    self.save_history(comment, draft["reply"])
                    self.stats["total_replied"] += 1
                else:
                    current["status"] = "failed"
                    current["error"] = "B站回复接口返回失败"
                    failed += 1
                self._save_review_drafts()

            delay = self.config["reply"].get("reply_delay", 2)
            if ok and delay > 0:
                time.sleep(delay)

        self._emit("review_updated", {"sent": sent, "failed": failed})
        return {"sent": sent, "failed": failed}

    def process_comments(self):
        """后台轮询只生成审核草稿，绝不自动发送。"""
        if not self.config["reply"].get("enabled", True):
            return
        result = self.generate_review_drafts()
        self.logger.info(
            "审核草稿更新：生成 %s 条，可回复 %s 条，跳过 %s 条",
            result["generated"],
            result["replyable"],
            result["skipped"],
        )

    def _check_filters(self, comment: Comment) -> tuple:
        """检查评论是否通过所有过滤器。返回 (通过, 跳过原因)"""
        # ── 长度过滤 ──
        lf = self.config["reply"].get("length_filter", {})
        if lf.get("enabled", False):
            min_len = lf.get("min_length", 0)
            max_len = lf.get("max_length", 500)
            content_len = len(comment.content)
            if min_len > 0 and content_len < min_len:
                return False, f"评论长度 {content_len} < {min_len}"
            if max_len > 0 and content_len > max_len:
                return False, f"评论长度 {content_len} > {max_len}"

        # ── 关键词过滤 ──
        kf = self.config["reply"].get("keyword_filter", {})
        if kf.get("enabled", False):
            wl_str = kf.get("whitelist", "").strip()
            bl_str = kf.get("blacklist", "").strip()
            match_case = kf.get("match_case", False)
            content = comment.content if match_case else comment.content.lower()

            # 黑名单
            if bl_str:
                keywords = [k.strip() for k in bl_str.split(",") if k.strip()]
                if not match_case:
                    keywords = [k.lower() for k in keywords]
                for kw in keywords:
                    if kw in content:
                        return False, f"命中黑名单关键词: {kw}"

            # 白名单
            if wl_str:
                keywords = [k.strip() for k in wl_str.split(",") if k.strip()]
                if not match_case:
                    keywords = [k.lower() for k in keywords]
                mode = kf.get("mode", "any")
                if mode == "all":
                    if not all(kw in content for kw in keywords):
                        return False, "未包含所有白名单关键词"
                else:
                    if not any(kw in content for kw in keywords):
                        return False, "未包含任何白名单关键词"

        # ── 用户过滤 ──
        uf = self.config["reply"].get("user_filter", {})
        if uf.get("enabled", False):
            uid = comment.uid
            bl_str = uf.get("blacklist", "").strip()
            wl_str = uf.get("whitelist", "").strip()

            if bl_str:
                blacklist = [u.strip() for u in bl_str.split(",") if u.strip()]
                if uid in blacklist:
                    return False, f"用户 {uid} 在黑名单中"

            if wl_str:
                whitelist = [u.strip() for u in wl_str.split(",") if u.strip()]
                if uid not in whitelist:
                    return False, f"用户 {uid} 不在白名单中"

        return True, ""

    def get_stats(self) -> dict:
        return {
            "running": self._running,
            "total_replied": self.stats["total_replied"],
            "start_time": self.stats["start_time"],
            "last_check": self.stats["last_check"],
            "processed_count": len(self.processed_comments),
            "cached_videos": len(self.cached_videos),
        }

    def verify_login(self) -> dict:
        if not self.cookie_manager:
            return {"valid": False, "message": "未配置Cookie"}
        valid, result = self.cookie_manager.verify_cookie()
        return {"valid": valid, **result}
