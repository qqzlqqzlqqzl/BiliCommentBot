#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站评论人工审核回复工具 — Web 服务入口
"""
import os
import time
import json
import logging
import threading
import webbrowser
import hashlib
import base64
import io
import random
import string
import copy
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from typing import Optional

import requests
import toml
import tomli_w

from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit

from account_manager import AccountBusyError, AccountManager, AccountNotFoundError
from bot import (
    BiliCommentBot,
    DEFAULT_CONFIG,
    REVIEW_READ_DEFAULT,
    REVIEW_TIME_RANGE_SECONDS,
    get_review_read_max,
    ReviewOperationBusyError,
    normalize_review_read_limit,
    CONFIG_FILE,
    HISTORY_FILE,
    COOKIE_FILE,
    VIDEO_CACHE_FILE,
)

# ─────────────────────────────────────────────
#  Flask + SocketIO
# ─────────────────────────────────────────────
SECRET_KEY = os.environ.get("BILI_SECRET_KEY", None)
if not SECRET_KEY:
    # 未设置环境变量时自动生成随机密钥（避免硬编码）
    SECRET_KEY = hashlib.sha256(os.urandom(32)).hexdigest()

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


def get_server_port() -> int:
    raw_port = os.environ.get("BILI_PORT", "5000").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError(f"BILI_PORT 不是有效端口: {raw_port}") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"BILI_PORT 超出范围: {port}")
    return port


def should_auto_start_monitor(cfg: dict) -> bool:
    """环境变量仅用于源码兼容覆盖；产品默认读取当前账号的持久化开关。"""
    override = os.environ.get("BILI_AUTO_START_MONITOR")
    if override is not None:
        return override.strip() == "1"
    return bool(cfg.get("bilibili", {}).get("auto_start_monitor", False))


def get_instance_name() -> str:
    return os.environ.get("BILI_ACCOUNT_NAME", "").strip() or "账号 1"


# ─────────────────────────────────────────────
#  日志 Handler（推送到前端）
# ─────────────────────────────────────────────
class WebSocketLogHandler(logging.Handler):
    def __init__(self, sio: SocketIO, account_id: str = ""):
        super().__init__()
        self.sio = sio
        self.account_id = account_id
        self.log_buffer: list = []
        self.max_buffer = 500

    def emit(self, record: logging.LogRecord):
        entry = {
            "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "msg": self.format(record),
        }
        if self.account_id:
            entry["account_id"] = self.account_id
        self.log_buffer.append(entry)
        if len(self.log_buffer) > self.max_buffer:
            self.log_buffer = self.log_buffer[-self.max_buffer:]
        try:
            self.sio.emit("log", entry)
        except Exception as exc:
            # 连接未就绪时静默忽略
            pass


ws_log_handler = WebSocketLogHandler(socketio)
ws_log_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
)
_account_log_handlers = {}


def get_product_data_root() -> str:
    return os.environ.get("BILI_PRODUCT_DATA_DIR", "").strip()


def is_product_mode() -> bool:
    return bool(get_product_data_root())

# ─────────────────────────────────────────────
#  配置管理
# ─────────────────────────────────────────────
def _load_config_file(config_file: str) -> dict:
    if not os.path.exists(config_file):
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = toml.load(f)

        def merge(base, override):
            result = dict(base)
            for k, v in override.items():
                if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                    result[k] = merge(result[k], v)
                else:
                    result[k] = v
            return result

        return merge(copy.deepcopy(DEFAULT_CONFIG), cfg)
    except Exception as e:
        print(f"加载配置文件失败: {e}，使用默认配置")
        return copy.deepcopy(DEFAULT_CONFIG)


def _save_config_file(config_file: str, cfg: dict) -> bool:
    try:
        directory = os.path.dirname(config_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp_file = f"{config_file}.tmp"
        with open(temp_file, "wb") as f:
            tomli_w.dump(cfg, f)
        os.replace(temp_file, config_file)
        return True
    except Exception as e:
        print(f"保存配置文件失败: {e}")
        return False


def load_config(account_id: str = None) -> dict:
    if is_product_mode():
        manager = get_account_manager()
        account_id = manager.resolve_account_id(account_id)
        return _load_config_file(
            os.path.join(manager.account_dir(account_id), "config.toml")
        )
    return _load_config_file(CONFIG_FILE)


def save_config(cfg: dict, account_id: str = None) -> bool:
    if is_product_mode():
        manager = get_account_manager()
        account_id = manager.resolve_account_id(account_id)
        return _save_config_file(
            os.path.join(manager.account_dir(account_id), "config.toml"),
            cfg,
        )
    return _save_config_file(CONFIG_FILE, cfg)


SENSITIVE_CONFIG_KEYS = {
    "bilibili": {"cookie", "refresh_token"},
    "ark": {"api_key"},
    "auth": {"password"},
}


def config_for_client(cfg: dict) -> dict:
    """返回浏览器可编辑配置，不把本地凭据重新暴露给页面。"""
    safe_cfg = copy.deepcopy(cfg)
    for section, keys in SENSITIVE_CONFIG_KEYS.items():
        section_cfg = safe_cfg.get(section)
        if not isinstance(section_cfg, dict):
            continue
        for key in keys:
            section_cfg.pop(key, None)
    return safe_cfg


def preserve_blank_sensitive_updates(data: dict) -> dict:
    """密钥输入框留空表示保持原值，清除必须走显式接口。"""
    safe_data = copy.deepcopy(data)
    for section, keys in SENSITIVE_CONFIG_KEYS.items():
        section_cfg = safe_data.get(section)
        if not isinstance(section_cfg, dict):
            continue
        for key in keys:
            value = section_cfg.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                section_cfg.pop(key, None)
    return safe_data


# ─────────────────────────────────────────────
#  日志设置
# ─────────────────────────────────────────────
def _setup_logger(
    cfg: dict,
    data_dir: str = None,
    account_id: str = "",
) -> logging.Logger:
    log_cfg = cfg.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = log_cfg.get("file", "logs/bot.log")
    if data_dir is None:
        data_dir = os.environ.get("BILI_DATA_DIR", "")
    if data_dir and not os.path.isabs(log_file):
        log_file = os.path.join(data_dir, log_file)
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    logger_name = f"BiliBot.{account_id}" if account_id else "BiliBot"
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    if log_cfg.get("console", True):
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(ch)

    if account_id:
        handler = WebSocketLogHandler(socketio, account_id=account_id)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        )
        _account_log_handlers[account_id] = handler
        logger.addHandler(handler)
    else:
        logger.addHandler(ws_log_handler)
    return logger


# ─────────────────────────────────────────────
#  全局机器人实例
# ─────────────────────────────────────────────
_bot: Optional[BiliCommentBot] = None
_bot_logger: Optional[logging.Logger] = None
_account_manager: Optional[AccountManager] = None


def _create_account_bot(account: dict) -> BiliCommentBot:
    account_id = account["id"]
    data_dir = account["data_dir"]
    cfg = _load_config_file(os.path.join(data_dir, "config.toml"))
    logger = _setup_logger(cfg, data_dir=data_dir, account_id=account_id)
    return BiliCommentBot(
        cfg,
        logger,
        socketio=socketio,
        on_config_changed=lambda rt, aid=account_id: _save_refresh_token(rt, aid),
        on_identity_changed=lambda uid, name, aid=account_id: _save_identity(
            uid,
            name,
            aid,
        ),
        data_dir=data_dir,
        account_id=account_id,
    )


def get_account_manager() -> AccountManager:
    global _account_manager
    root_dir = get_product_data_root()
    if not root_dir:
        raise RuntimeError("当前不是产品多账号模式")
    if _account_manager is None or _account_manager.root_dir != os.path.abspath(root_dir):
        _account_manager = AccountManager(root_dir, _create_account_bot)
    return _account_manager


def get_bot(account_id: str = None) -> BiliCommentBot:
    global _bot, _bot_logger
    if is_product_mode():
        return get_account_manager().get_bot(account_id)
    if _bot is None:
        cfg = load_config()
        _bot_logger = _setup_logger(cfg)
        _bot = BiliCommentBot(cfg, _bot_logger, socketio=socketio,
                              on_config_changed=lambda rt: _save_refresh_token(rt),
                              on_identity_changed=lambda uid, name: _save_identity(uid, name))
    return _bot


def _save_refresh_token(new_token: str, account_id: str = None):
    """持久化 refresh_token 到配置文件"""
    cfg = load_config(account_id)
    cfg.setdefault("bilibili", {})["refresh_token"] = new_token
    save_config(cfg, account_id)


def _save_identity(uid: str, name: str = "", account_id: str = None):
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("B站账号 UID 为空")
    cfg = load_config(account_id)
    changed = str(cfg.setdefault("bilibili", {}).get("uid") or "") != uid
    cfg["bilibili"]["uid"] = uid
    if changed and not save_config(cfg, account_id):
        raise RuntimeError("B站账号身份保存失败")
    if is_product_mode() and name:
        get_account_manager().rename_account(account_id, name)


# ─────────────────────────────────────────────
#  扫码登录
# ─────────────────────────────────────────────
BILI_QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}

_qr_states = {}


def _gen_qr_image_base64(url: str) -> str:
    import qrcode
    from PIL import Image
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _qr_account_id() -> str:
    return (
        get_account_manager().current_account_id()
        if is_product_mode()
        else "legacy"
    )


def _emit_qr(event: str, account_id: str, payload: dict):
    socketio.emit(event, {"account_id": account_id, **payload})


def _persist_qr_cookie(account_id: str, cookie_str: str):
    target_account_id = None if account_id == "legacy" else account_id
    cfg = load_config(target_account_id)
    cfg.setdefault("bilibili", {})["cookie"] = cookie_str
    if not save_config(cfg, target_account_id):
        raise RuntimeError("Cookie 保存失败")
    if is_product_mode():
        manager = get_account_manager()
        bot = manager.get_loaded_bot(account_id)
        if bot is not None:
            bot.reload_config(cfg)
    elif _bot is not None:
        _bot.reload_config(cfg)


def _poll_qr_login(account_id: str, qr_key: str, session: requests.Session):
    params = {"qrcode_key": qr_key}
    timeout = 180
    start = time.time()
    last_code = None
    while time.time() - start < timeout:
        try:
            resp = session.get(BILI_QR_POLL, params=params, headers=BILI_HEADERS, timeout=10)
            data = resp.json()["data"]
            code = data["code"]
            if code != last_code:
                last_code = code
                msg_map = {86101: "等待扫码...", 86090: "已扫码，请在手机确认", 86038: "二维码已失效", 0: "登录成功！"}
                _emit_qr(
                    "qr_status",
                    account_id,
                    {"code": code, "message": msg_map.get(code, str(code))},
                )
            if code == 0:
                cookies = dict(session.cookies)
                if data.get("url"):
                    qs = parse_qs(urlparse(data["url"]).query)
                    for k, v in qs.items():
                        if k not in cookies:
                            cookies[k] = v[0]
                cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                _persist_qr_cookie(account_id, cookie_str)
                target_account_id = None if account_id == "legacy" else account_id
                identity = get_bot(target_account_id).verify_login()
                if not identity.get("valid"):
                    raise RuntimeError(
                        f"登录成功但账号身份识别失败: {identity.get('message', '未知错误')}"
                    )
                _emit_qr(
                    "qr_cookie",
                    account_id,
                    {"saved": True},
                )
                return
            if code == 86038:
                return
        except Exception as e:
            _emit_qr(
                "qr_status",
                account_id,
                {"code": -1, "message": f"请求错误: {e}"},
            )
        time.sleep(1.5)
    _emit_qr(
        "qr_status",
        account_id,
        {"code": -2, "message": "登录超时"},
    )


# ─────────────────────────────────────────────
#  Flask 路由
# ─────────────────────────────────────────────
@app.route("/")
def index():
    cfg = load_config()
    if is_product_mode():
        manager = get_account_manager()
        current_id = manager.current_account_id()
        current = next(
            account
            for account in manager.list_accounts()
            if account["id"] == current_id
        )
        instance_name = current["name"]
    else:
        instance_name = get_instance_name()
    return render_template(
        "index.html",
        instance_name=instance_name,
        instance_port=get_server_port(),
        product_mode=is_product_mode(),
        review_hard_limit=get_review_read_max(),
    )


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "ok": True,
        "product": "BiliCommentReviewer",
        "product_mode": is_product_mode(),
    })


@app.route("/api/accounts", methods=["GET"])
def api_accounts():
    if not is_product_mode():
        return jsonify({
            "ok": True,
            "product_mode": False,
            "current_account_id": "legacy",
            "accounts": [{
                "id": "legacy",
                "name": (
                    getattr(_bot, "_identity_name", "")
                    if _bot is not None
                    else ""
                ) or get_instance_name(),
                "current": True,
                "running": bool(_bot and _bot.is_running),
                "loaded": _bot is not None,
            }],
        })
    manager = get_account_manager()
    return jsonify({
        "ok": True,
        "product_mode": True,
        "current_account_id": manager.current_account_id(),
        "accounts": manager.list_accounts(),
    })


@app.route("/api/accounts", methods=["POST"])
def api_account_create():
    if not is_product_mode():
        return jsonify({"ok": False, "message": "当前启动方式不支持应用内多账号"}), 409
    data = request.get_json(silent=True) or {}
    try:
        account = get_account_manager().create_account(data.get("name", ""))
        return jsonify({"ok": True, "account": account})
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except AccountBusyError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409


@app.route("/api/accounts/import", methods=["POST"])
def api_account_import():
    if not is_product_mode():
        return jsonify({"ok": False, "message": "当前启动方式不支持账号导入"}), 409
    data = request.get_json(silent=True) or {}
    try:
        account = get_account_manager().import_legacy_account(
            data.get("name", ""),
            data.get("source_dir", ""),
        )
        return jsonify({"ok": True, "account": account})
    except AccountBusyError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return jsonify({"ok": False, "message": f"导入失败：{exc}"}), 400


@app.route("/api/accounts/select", methods=["POST"])
def api_account_select():
    if not is_product_mode():
        return jsonify({"ok": False, "message": "当前启动方式不支持应用内多账号"}), 409
    data = request.get_json(silent=True) or {}
    try:
        account = get_account_manager().select_account(data.get("account_id", ""))
        return jsonify({"ok": True, "account": account})
    except AccountNotFoundError:
        return jsonify({"ok": False, "message": "账号不存在"}), 404
    except AccountBusyError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409


@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = load_config()
    has_ark_api_key = bool(
        os.environ.get("ARK_API_KEY")
        or os.environ.get("VOLCENGINE_ARK_API_KEY")
        or cfg.get("ark", {}).get("api_key", "")
    )
    return jsonify({
        "ok": True,
        "config": config_for_client(cfg),
        "capabilities": {
            "bilibili_cookie_configured": bool(
                cfg.get("bilibili", {}).get("cookie", "")
            ),
            "bilibili_refresh_token_configured": bool(
                cfg.get("bilibili", {}).get("refresh_token", "")
            ),
            "ark_api_key_configured": has_ark_api_key,
        },
    })


@app.route("/api/config", methods=["POST"])
def api_save_config():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "message": "无效数据"})
    data = preserve_blank_sensitive_updates(data)
    cfg = load_config()

    def deep_update(base, upd):
        for k, v in upd.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                deep_update(base[k], v)
            else:
                base[k] = v
    deep_update(cfg, data)
    if save_config(cfg):
        bot = get_bot()
        applied = bot.reload_config(cfg)
        message = (
            "配置已保存并生效"
            if applied
            else "配置已保存；当前任务继续使用启动时配置，结束后自动生效"
        )
        return jsonify({"ok": True, "message": message, "deferred": not applied})
    return jsonify({"ok": False, "message": "保存失败"})


@app.route("/api/config/secrets/clear", methods=["POST"])
def api_clear_config_secret():
    data = request.get_json(silent=True) or {}
    secret = str(data.get("secret", "")).strip()
    cfg = load_config()
    if secret == "bilibili_login":
        cfg.setdefault("bilibili", {})["cookie"] = ""
        cfg.setdefault("bilibili", {})["refresh_token"] = ""
        message = "当前账号保存的 B站登录凭据已清除"
    elif secret == "ark_api_key":
        cfg.setdefault("ark", {})["api_key"] = ""
        message = "当前账号保存的豆包 API Key 已清除"
        if os.environ.get("ARK_API_KEY") or os.environ.get("VOLCENGINE_ARK_API_KEY"):
            message += "；环境变量中的 API Key 仍然生效"
    else:
        return jsonify({"ok": False, "message": "不支持的凭据类型"}), 400

    if not save_config(cfg):
        return jsonify({"ok": False, "message": "清除失败"}), 500
    bot = get_bot()
    applied = bot.reload_config(cfg)
    return jsonify({
        "ok": True,
        "message": message,
        "deferred": not applied,
    })


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    cfg = load_config()
    cfg.setdefault("bilibili", {})["auto_start_monitor"] = True
    if not save_config(cfg):
        return jsonify({"ok": False, "message": "保存自动监控状态失败"}), 500
    bot = get_bot()
    applied = bot.reload_config(cfg)
    started = bot.start()
    return jsonify({
        "ok": True,
        "message": (
            "草稿监控已启动，重启程序后仍会自动恢复"
            if started
            else "草稿监控已在运行，重启程序后仍会自动恢复"
        ),
        "deferred": not applied,
    })


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    cfg = load_config()
    cfg.setdefault("bilibili", {})["auto_start_monitor"] = False
    if not save_config(cfg):
        return jsonify({"ok": False, "message": "保存自动监控状态失败"}), 500
    bot = get_bot()
    applied = bot.reload_config(cfg)
    result = bot.stop()
    if not result:
        return jsonify({
            "ok": True,
            "message": "草稿监控已保持停止，重启程序后不会自动启动",
            "deferred": not applied,
        })
    operations = bot.get_review_operation_status()["active"]
    active = [
        {
            "generating": "生成",
            "regenerating": "重新生成",
            "sending": "发送",
        }.get(name, name)
        for name, count in operations.items()
        if count
    ]
    message = (
        f"草稿监控已停止；当前{'、'.join(active)}任务继续完成"
        if active
        else "草稿监控已停止"
    )
    return jsonify({
        "ok": True,
        "message": f"{message}；重启程序后不会自动启动",
        "deferred": not applied,
    })


@app.route("/api/bot/status", methods=["GET"])
def api_bot_status():
    bot = get_bot()
    return jsonify({"ok": True, **bot.get_stats()})


@app.route("/api/bot/verify", methods=["GET"])
def api_verify():
    bot = get_bot()
    return jsonify({"ok": True, **bot.verify_login()})


@app.route("/api/history", methods=["GET"])
def api_history():
    """从 bot 的内存缓冲区读取历史记录（比读文件快）"""
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    try:
        bot = get_bot()
        history = bot.get_history()
        history.sort(key=lambda x: x.get("reply_time", 0), reverse=True)
        total = len(history)
        start = (page - 1) * per_page
        end = start + per_page
        return jsonify({"ok": True, "total": total, "page": page, "data": history[start:end]})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    try:
        bot = get_bot()
        bot.processed_comments.clear()
        bot._history_buffer = []
        bot._history_dirty = True
        bot._flush_history()
        return jsonify({"ok": True, "message": "历史记录已清除"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/review/drafts", methods=["GET"])
def api_review_drafts():
    bot = get_bot()
    all_drafts = bot.get_review_drafts()
    review_time_range = str(request.args.get("review_time_range") or "").strip()
    review_since = str(request.args.get("review_since") or "").strip()
    try:
        since_timestamp = BiliCommentBot._resolve_review_since(
            review_since,
            review_time_range,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    drafts = all_drafts
    if since_timestamp is not None:
        drafts = [
            draft
            for draft in all_drafts
            if int(draft.get("comment_time") or 0) >= since_timestamp
        ]
    return jsonify({
        "ok": True,
        "total": len(drafts),
        "total_all": len(all_drafts),
        "since_timestamp": since_timestamp,
        "drafts": drafts,
        "operations": bot.get_review_operation_status(),
    })


@app.route("/api/review/generate", methods=["POST"])
def api_review_generate():
    data = request.get_json(silent=True) or {}
    limit = normalize_review_read_limit(data.get("limit", REVIEW_READ_DEFAULT))
    review_since = data.get("review_since") if "review_since" in data else None
    review_time_range = (
        data.get("review_time_range")
        if "review_time_range" in data
        else None
    )
    if review_time_range is not None:
        review_time_range = str(review_time_range or "").strip()
        if review_time_range not in {"", "custom", *REVIEW_TIME_RANGE_SECONDS}:
            return jsonify({"ok": False, "message": "时间范围无效"}), 400
    try:
        result = get_bot().generate_review_drafts(
            limit=limit,
            review_since=review_since,
            review_time_range=review_time_range,
        )
        return jsonify({"ok": True, **result})
    except ReviewOperationBusyError as e:
        return jsonify({"ok": False, "message": str(e)}), 409
    except Exception as e:
        get_bot().logger.exception("生成审核草稿失败")
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/review/preferences", methods=["POST"])
def api_review_preferences():
    data = request.get_json(silent=True) or {}
    limit = normalize_review_read_limit(
        data.get("limit", REVIEW_READ_DEFAULT)
    )
    review_time_range = str(data.get("review_time_range") or "").strip()
    review_since = str(data.get("review_since") or "").strip()
    if review_time_range not in {"", "custom", *REVIEW_TIME_RANGE_SECONDS}:
        return jsonify({"ok": False, "message": "时间范围无效"}), 400
    if review_time_range != "custom":
        review_since = ""
    try:
        BiliCommentBot._resolve_review_since(review_since, review_time_range)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    cfg = load_config()
    reply_cfg = cfg.setdefault("reply", {})
    reply_cfg["max_process"] = limit
    reply_cfg["review_time_range"] = review_time_range
    reply_cfg["review_since"] = review_since
    if not save_config(cfg):
        return jsonify({"ok": False, "message": "保存读取偏好失败"}), 500
    applied = get_bot().reload_config(cfg)
    return jsonify({
        "ok": True,
        "message": (
            "读取偏好已保存"
            if applied
            else "读取偏好已保存，当前任务结束后生效"
        ),
        "deferred": not applied,
    })


@app.route("/api/review/approve", methods=["POST"])
def api_review_approve():
    data = request.get_json(silent=True) or {}
    comment_id = str(data.get("comment_id", "")).strip()
    if not comment_id:
        return jsonify({"ok": False, "message": "缺少 comment_id"}), 400
    if not isinstance(data.get("approved"), bool):
        return jsonify({"ok": False, "message": "approved 必须是布尔值"}), 400
    try:
        draft = get_bot().set_review_approval(comment_id, data["approved"])
        return jsonify({"ok": True, "draft": draft})
    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/review/approve-bulk", methods=["POST"])
def api_review_approve_bulk():
    data = request.get_json(silent=True) or {}
    comment_ids = data.get("comment_ids")
    if not isinstance(comment_ids, list) or not comment_ids:
        return jsonify({
            "ok": False,
            "message": "必须明确提交至少一个 comment_id",
        }), 400
    if not isinstance(data.get("approved"), bool):
        return jsonify({"ok": False, "message": "approved 必须是布尔值"}), 400
    try:
        result = get_bot().set_review_approvals(comment_ids, data["approved"])
        return jsonify({"ok": True, **result})
    except ReviewOperationBusyError as e:
        return jsonify({"ok": False, "message": str(e)}), 409
    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/review/dismiss", methods=["POST"])
def api_review_dismiss():
    data = request.get_json(silent=True) or {}
    comment_id = str(data.get("comment_id", "")).strip()
    if not comment_id:
        return jsonify({"ok": False, "message": "缺少 comment_id"}), 400
    if not isinstance(data.get("dismissed"), bool):
        return jsonify({"ok": False, "message": "dismissed 必须是布尔值"}), 400
    try:
        draft = get_bot().set_review_dismissed(comment_id, data["dismissed"])
        return jsonify({"ok": True, "draft": draft})
    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/review/regenerate", methods=["POST"])
def api_review_regenerate():
    data = request.get_json(silent=True) or {}
    comment_id = str(data.get("comment_id", "")).strip()
    if not comment_id:
        return jsonify({"ok": False, "message": "缺少 comment_id"}), 400
    try:
        draft = get_bot().regenerate_review_draft(comment_id)
        return jsonify({"ok": True, "draft": draft})
    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        get_bot().logger.exception("重新生成审核回复失败")
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/review/send", methods=["POST"])
def api_review_send():
    data = request.get_json(silent=True) or {}
    comment_ids = data.get("comment_ids")
    if not isinstance(comment_ids, list) or not comment_ids:
        return jsonify({"ok": False, "message": "必须明确提交至少一个已批准的 comment_id"}), 400
    try:
        config = load_config()
        reply_config = config.get("reply") or {}
        saved_since = BiliCommentBot._resolve_review_since(
            reply_config.get("review_since", ""),
            reply_config.get("review_time_range", ""),
        )
        submitted_since = None
        if "review_since" in data or "review_time_range" in data:
            submitted_since = BiliCommentBot._resolve_review_since(
                data.get("review_since", ""),
                data.get("review_time_range", ""),
            )
        cutoffs = [
            cutoff
            for cutoff in (saved_since, submitted_since)
            if cutoff is not None
        ]
        since_timestamp = max(cutoffs) if cutoffs else None
        result = get_bot().send_approved_drafts(
            comment_ids=comment_ids,
            since_timestamp=since_timestamp,
        )
        return jsonify({"ok": True, **result})
    except ReviewOperationBusyError as e:
        return jsonify({"ok": False, "message": str(e)}), 409
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/logs", methods=["GET"])
def api_logs():
    if is_product_mode():
        account_id = get_account_manager().current_account_id()
        get_bot(account_id)
        handler = _account_log_handlers.get(account_id)
        logs = handler.log_buffer[-200:] if handler else []
    else:
        logs = ws_log_handler.log_buffer[-200:]
    return jsonify({"ok": True, "logs": logs})


@app.route("/api/qr/generate", methods=["POST"])
def api_qr_generate():
    try:
        account_id = _qr_account_id()
        previous = _qr_states.get(account_id)
        if previous and previous["thread"].is_alive():
            return jsonify({"ok": False, "message": "该账号已有二维码登录正在进行"}), 409
        session = requests.Session()
        resp = session.get(BILI_QR_GENERATE, headers=BILI_HEADERS, timeout=10)
        data = resp.json()
        if data["code"] != 0:
            return jsonify({"ok": False, "message": "获取二维码失败"})
        qr_url = data["data"]["url"]
        qr_key = data["data"]["qrcode_key"]
        qr_b64 = _gen_qr_image_base64(qr_url)
        thread = threading.Thread(
            target=_poll_qr_login,
            args=(account_id, qr_key, session),
            daemon=True,
        )
        _qr_states[account_id] = {
            "session": session,
            "key": qr_key,
            "thread": thread,
        }
        thread.start()
        return jsonify({
            "ok": True,
            "account_id": account_id,
            "qr_image": qr_b64,
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    bot = get_bot()
    bot.cached_videos = []
    bot.last_video_fetch_time = 0
    bot._partial_save_video_cache([], 0)
    return jsonify({"ok": True, "message": "视频缓存已清除"})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    data = request.get_json()
    password = data.get("password", "") if data else ""
    cfg = load_config()
    auth_cfg = cfg.get("auth", {})
    stored_hash = auth_cfg.get("password", "")
    if not auth_cfg.get("enabled", False) or not stored_hash:
        return jsonify({"ok": True, "message": "未启用密码保护"})
    pwd_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    if pwd_hash == stored_hash:
        return jsonify({"ok": True, "message": "验证成功"})
    return jsonify({"ok": False, "message": "密码错误"})


@app.route("/api/auth/password", methods=["POST"])
def api_auth_password():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "message": "无效数据"})
    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")
    action = data.get("action", "")

    cfg = load_config()
    auth_cfg = cfg.get("auth", {})
    stored_hash = auth_cfg.get("password", "")

    if action == "generate":
        chars = string.ascii_letters + string.digits + "!@#$%^&*"
        new_password = "".join(random.choice(chars) for _ in range(16))
        pwd_hash = hashlib.sha256(new_password.encode("utf-8")).hexdigest()
        cfg.setdefault("auth", {})["password"] = pwd_hash
        cfg.setdefault("auth", {})["enabled"] = True
        if save_config(cfg):
            return jsonify({"ok": True, "message": "密码已生成", "password": new_password})
        return jsonify({"ok": False, "message": "保存失败"})

    if action == "change":
        if stored_hash:
            old_hash = hashlib.sha256(old_password.encode("utf-8")).hexdigest()
            if old_hash != stored_hash:
                return jsonify({"ok": False, "message": "旧密码错误"})
        if not new_password or len(new_password) < 4:
            return jsonify({"ok": False, "message": "新密码长度不能少于4位"})
        pwd_hash = hashlib.sha256(new_password.encode("utf-8")).hexdigest()
        cfg.setdefault("auth", {})["password"] = pwd_hash
        cfg.setdefault("auth", {})["enabled"] = True
        if save_config(cfg):
            return jsonify({"ok": True, "message": "密码已修改"})
        return jsonify({"ok": False, "message": "保存失败"})

    if action == "clear":
        cfg.setdefault("auth", {})["password"] = ""
        cfg.setdefault("auth", {})["enabled"] = False
        if save_config(cfg):
            return jsonify({"ok": True, "message": "密码保护已关闭"})
        return jsonify({"ok": False, "message": "保存失败"})

    return jsonify({"ok": False, "message": "未知操作"})


@app.route("/api/videos", methods=["GET"])
def api_videos():
    bot = get_bot()
    return jsonify({"ok": True, "count": len(bot.cached_videos), "videos": bot.cached_videos[:50]})


# ─────────────────────────────────────────────
#  SocketIO 事件
# ─────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    bot = get_bot()
    emit("bot_status", {"running": bot.is_running})
    emit("stats", bot.get_stats())
    if is_product_mode():
        account_id = get_account_manager().current_account_id()
        handler = _account_log_handlers.get(account_id)
        logs = handler.log_buffer[-100:] if handler else []
    else:
        logs = ws_log_handler.log_buffer[-100:]
    emit("log_history", {"logs": logs})


# ─────────────────────────────────────────────
#  入口
# ─────────────────────────────────────────────
def main():
    host = os.environ.get("BILI_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = get_server_port()
    if is_product_mode():
        manager = get_account_manager()
        current_id = manager.current_account_id()
        instance_name = next(
            account["name"]
            for account in manager.list_accounts()
            if account["id"] == current_id
        )
    else:
        instance_name = get_instance_name()
    url = f"http://{host}:{port}"
    browser_url = f"http://127.0.0.1:{port}"
    print(f"""
╔══════════════════════════════════════════╗
║       B站评论人工审核回复工具 Web UI        ║
╠══════════════════════════════════════════╣
║  当前实例: {instance_name:<31}║
║  访问地址: {url:<31}║
║  按 Ctrl+C 停止服务                      ║
╚══════════════════════════════════════════╝
""")
    # 初始化机器人（预加载）
    bot = get_bot()

    # 检测配置是否完整，自动启动机器人
    cfg = load_config()
    cookie = cfg.get("bilibili", {}).get("cookie", "")
    api_key = (
        os.environ.get("ARK_API_KEY")
        or os.environ.get("VOLCENGINE_ARK_API_KEY")
        or cfg.get("ark", {}).get("api_key", "")
    )
    if cookie:
        try:
            identity = bot.verify_login()
            if identity.get("valid"):
                user_info = identity.get("user_info") or {}
                print(f"✓ 已识别当前 B站账号: {user_info.get('name') or '未知昵称'}")
            else:
                print(f"⚠️  B站账号身份识别失败: {identity.get('message', '未知错误')}")
        except Exception as exc:
            print(f"⚠️  B站账号身份识别失败: {exc}")

    auto_start_monitor = should_auto_start_monitor(cfg)
    if cookie and api_key and auto_start_monitor:
        print("检测到有效配置，自动启动机器人...")
        if bot.start():
            print("✓ 机器人已自动启动")
        else:
            print("✗ 机器人启动失败")
    elif cookie and api_key:
        print("Web 服务已启动；草稿监控保持停止，请在页面中手动启动")
    else:
        print("提示: 请在 Web UI 中完成配置后启动")

    # 延迟打开浏览器（Docker 环境下不打开）
    if (
        os.getenv("DOCKER_ENV") != "true"
        and os.getenv("BILI_OPEN_BROWSER", "1").strip() != "0"
    ):
        threading.Timer(1.5, lambda: webbrowser.open(browser_url)).start()
    try:
        socketio.run(
            app,
            host=host,
            port=port,
            debug=False,
            use_reloader=False,
            log_output=False,
            allow_unsafe_werkzeug=True,
        )
    finally:
        if is_product_mode():
            get_account_manager().shutdown_all()
        else:
            bot.prepare_shutdown()


if __name__ == "__main__":
    main()
