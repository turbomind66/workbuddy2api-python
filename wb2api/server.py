"""server.py — 暴露 OpenAI 兼容 HTTP 接口，内部驱动 pool 挑号 + upstream 转发。

等价于 Go 版 internal/server/{handler,logging}.go（http.server 实现，无需第三方 Web 框架）。
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import signal
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests  # noqa: F401  (upstream 依赖)
from wb2api import upstream
from wb2api.pool import Pool
from wb2api.session import Router, extract_key
from wb2api.upstream import Client, ErrKind, classify

LOG = logging.getLogger("wb2api.server")

DEFAULT_SOFT_COOLDOWN = timedelta(seconds=60)
DEFAULT_REFRESH_SKEW = timedelta(minutes=10)
DEFAULT_MAX_ROTATE = 3

# 静态 CN 模型表（API reference §5，动态接口失败时的回退）。
STATIC_MODELS = [
    {"id": "glm-5.2", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "glm-5.1", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "glm-5v-turbo", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "kimi-k2.7", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "minimax-m3", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "hy3", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "hy3-preview", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "hy3-preview-agent", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "deepseek-v4-pro", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
    {"id": "deepseek-v4-flash", "object": "model", "created": 1753600000, "owned_by": "workbuddy", "context_length": 131072},
]

# 动态模型缓存（模块级，跨请求共享）。
_DYNAMIC_TTL = timedelta(hours=1)
_MODELS_FETCH_FAIL_COOLDOWN = timedelta(minutes=5)
_models_cache_lock = threading.RLock()
_models_cache: List[upstream.ModelInfo] = []
_models_fetched: Optional[datetime] = None
_models_last_fail: Optional[datetime] = None


class Config:
    def __init__(self, pool=None, upstream=None, api_key="",
                 session=None, sticky_count=None, redis_mode=None,
                 soft_cooldown=None, refresh_skew=None, max_rotate=None) -> None:
        self.pool: Optional[Pool] = pool
        self.upstream: Optional[Client] = upstream
        self.api_key = api_key
        self.max_rotate = max_rotate if max_rotate is not None else DEFAULT_MAX_ROTATE
        self.session: Optional[Router] = session
        self.sticky_count = sticky_count  # () -> int
        self.redis_mode = redis_mode if redis_mode is not None else "noop"
        self.soft_cooldown = soft_cooldown if soft_cooldown is not None else DEFAULT_SOFT_COOLDOWN
        self.refresh_skew = refresh_skew if refresh_skew is not None else DEFAULT_REFRESH_SKEW


# ---------------------------------------------------------------------------
# 请求级表格日志
# ---------------------------------------------------------------------------
_chat_seq = 0
_chat_seq_lock = threading.Lock()
_chat_log_enabled = True


class ChatStat:
    def __init__(self, start: datetime, model: str, mode: str) -> None:
        self.start = start
        self.model = model
        self.mode = mode
        self.uid = ""
        self.ttfb = timedelta(0)
        self.toks = -1
        self.status = 0
        self._logged = False

    def done(self) -> None:
        if self._logged:
            return
        self._logged = True
        _log_chat_row(self.ttfb, datetime.now() - self.start, self.model, self.mode,
                      self.uid, self.status, self.toks)


def _log_chat_row(ttfb: timedelta, total: timedelta, model: str, mode: str,
                  uid: str, status: int, toks: int) -> None:
    if not _chat_log_enabled:
        return
    global _chat_seq
    with _chat_seq_lock:
        _chat_seq += 1
        seq = _chat_seq
    if len(model) > 11:
        model = model[:11]
    tok_field = "-"
    tokps_field = "-"
    if toks >= 0:
        tok_field = str(toks)
        tokps_field = f"{toks / total.total_seconds():.1f}" if total.total_seconds() > 0 else "0.0"
    ttfb_ms = f"{int(ttfb.total_seconds() * 1000)}ms" if ttfb.total_seconds() > 0 else "-"
    line = (f"| #{seq:03d} | {datetime.now().strftime('%H:%M:%S')} | {model} | {mode} | "
            f"{status} | uid={uid[:8] if uid else '-'} | TTFB={ttfb_ms} | tok={tok_field} | "
            f"{tokps_field}tok/s | total={total.total_seconds():.1f}s |")
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _parse_model_from_body(body: bytes) -> str:
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return "-"
    if isinstance(obj, dict):
        m = obj.get("model")
        if isinstance(m, str) and m:
            return m
    return "-"


def _completion_tokens(resp: Dict[str, Any]) -> int:
    u = resp.get("usage")
    if isinstance(u, dict):
        v = u.get("completion_tokens")
        if isinstance(v, (int, float)):
            return int(v)
    return -1


def _uid_prefix(uid: str) -> str:
    if not uid:
        return "-"
    return uid[:8] if len(uid) > 8 else uid


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # type: ignore
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

    def handle(self):
        # 客户端在请求任意阶段断开（WinError 10053/10054 等）时静默关闭连接，
        # 避免 socketserver 框架层打印整段 traceback 噪音。
        try:
            super().handle()
        except ConnectionError:
            self.close_connection = True

    # ---- 路由 ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/models":
            if not self._check_auth():
                return
            self.models()
        elif path == "/status":
            if not self._check_auth():
                return
            self.status()
        elif path == "/healthz":
            self.healthz()
        else:
            self._send_json(404, {"error": {"message": "not found", "type": "api_error", "code": "not_found"}})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/chat/completions":
            if not self._check_auth():
                return
            self.chat_completions()
        else:
            self._send_json(404, {"error": {"message": "not found", "type": "api_error", "code": "not_found"}})

    def _check_auth(self) -> bool:
        cfg = self.cfg
        if not cfg.api_key:
            return True
        authz = self.headers.get("Authorization", "")
        if not authz.startswith("Bearer ") or authz[len("Bearer "):] != cfg.api_key:
            self._send_json(401, {"error": {
                "message": "missing or invalid API key", "type": "api_error", "code": "invalid_api_key"}})
            return False
        return True

    def _send_json(self, status: int, v: Any) -> None:
        raw = json.dumps(v, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_openai_error(self, status: int, code: str, msg: str) -> None:
        self._send_json(status, {"error": {"message": msg, "type": "api_error", "code": code}})

    # ---- 端点 ----
    def healthz(self):
        total, healthy, _, _, _ = self.cfg.pool.counts_detailed()
        status = 200
        if not self.cfg.pool.servable_now():
            status = 503
        self._send_json(status, {"healthy": healthy, "total": total})

    def status(self):
        total, healthy, cooling, disabled, in_flight_full = self.cfg.pool.counts_detailed()
        sticky = 0
        if self.cfg.sticky_count is not None:
            sticky = self.cfg.sticky_count()
        redis_mode = self.cfg.redis_mode or "noop"
        self._send_json(200, {
            "accounts": self.cfg.pool.list(),
            "total": total,
            "healthy": healthy,
            "cooling": cooling,
            "disabled": disabled,
            "in_flight_full": in_flight_full,
            "sticky_sessions": sticky,
            "redis_mode": redis_mode,
        })

    def models(self):
        self._send_json(200, {"object": "list", "data": self._model_list()})

    def _model_list(self) -> List[Dict[str, Any]]:
        infos = self._fetch_dynamic_models()
        if infos:
            out = []
            for mi in infos:
                entry = {
                    "id": mi.id,
                    "object": "model",
                    "created": 1753600000,
                    "owned_by": "workbuddy",
                    "context_length": mi.context_window or 131072,
                    "max_output_tokens": mi.max_tokens,
                }
                out.append(entry)
            return out
        return STATIC_MODELS

    def _fetch_dynamic_models(self) -> List[upstream.ModelInfo]:
        global _models_cache, _models_fetched, _models_last_fail
        with _models_cache_lock:
            if _models_cache and _models_fetched and (datetime.now() - _models_fetched) < _DYNAMIC_TTL:
                return _models_cache
            if _models_last_fail and (datetime.now() - _models_last_fail) < _MODELS_FETCH_FAIL_COOLDOWN:
                return []
        acct = self.cfg.pool.pick()
        if acct is None:
            return []
        infos, err = self.cfg.upstream.fetch_models(acct)
        with _models_cache_lock:
            if err is not None or not infos:
                self.cfg.pool.note_error(acct.uid)
                _models_last_fail = datetime.now()
                return []
            _models_cache[:] = infos
            _models_fetched = datetime.now()
            _models_last_fail = None
            return list(infos)

    def chat_completions(self):
        clen = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(clen) if clen > 0 else b""
        try:
            peek = json.loads(body)
            stream = bool(peek.get("stream", False))
        except (json.JSONDecodeError, ValueError):
            stream = False
            peek = {}

        st = ChatStat(datetime.now(), _parse_model_from_body(body), "stream" if stream else "sync")
        try:
            self._chat_loop(st, body, stream)
        finally:
            st.done()

    def _chat_loop(self, st: ChatStat, body: bytes, stream: bool) -> None:
        cfg = self.cfg
        pool = cfg.pool
        up = cfg.upstream

        tried: Dict[str, bool] = {}
        last_err: Optional[Exception] = None

        sess_key = ""
        sticky_uid = ""
        if cfg.session is not None:
            sess_key = extract_key(body)
            if sess_key:
                r = cfg.session.resolve(sess_key)
                if r[1]:
                    sticky_uid = r[0]

        held_uid = ""

        def release_held():
            nonlocal held_uid
            if held_uid:
                pool.release(held_uid)
                held_uid = ""

        def fail(uid):
            release_held()
            if sticky_uid and uid == sticky_uid and cfg.session is not None:
                cfg.session.unbind(sess_key)
                sticky_uid = ""

        max_rotate = cfg.max_rotate
        for _ in range(max_rotate):
            acct = None
            if sticky_uid:
                acct = pool.pick_by_uid(sticky_uid)
                if acct is None:
                    if cfg.session is not None:
                        cfg.session.unbind(sess_key)
                    sticky_uid = ""
            if acct is None:
                acct = pool.pick_excluding(tried)
            if acct is None:
                st.status = 503
                break
            st.uid = acct.uid
            tried[acct.uid] = True

            if not pool.acquire(acct.uid):
                if sticky_uid and acct.uid == sticky_uid and cfg.session is not None:
                    cfg.session.unbind(sess_key)
                    sticky_uid = ""
                continue
            held_uid = acct.uid

            if acct.needs_refresh(cfg.refresh_skew):
                err = up.refresh_token(acct)
                if err is not None:
                    last_err = err
                    if getattr(err, "kind", None) == ErrKind.SESSION_DEAD:
                        pool.disable(acct.uid, "refresh session dead")
                    else:
                        pool.note_error(acct.uid)
                    fail(acct.uid)
                    continue
                try:
                    acct.save_atomic()
                except Exception as e:  # noqa
                    LOG.warning("chat refresh uid=%s: save auth failed: %s", acct.uid, e)

            rc, status, resp_body, terr = up.chat_stream(acct, body)
            if terr is not None:
                st.status = 503
                last_err = terr
                fail(acct.uid)
                continue
            if status >= 400:
                st.status = status
                kind = classify(status, resp_body.decode("utf-8", "replace") if resp_body else "")
                last_err = upstream.Error(kind, status, resp_body.decode("utf-8", "replace")[:200]
                                          if resp_body else "")
                self._apply_error_policy(acct.uid, kind)
                fail(acct.uid)
                continue

            pool.note_success(acct.uid)
            if sess_key and cfg.session is not None:
                cfg.session.bind(sess_key, acct.uid)

            try:
                if stream:
                    st.status = 200
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    ttfb_ms, toks, has_usage, serr = upstream.stream(self.wfile, rc.raw, start_time=st.start)
                    st.ttfb = timedelta(milliseconds=ttfb_ms)
                    st.toks = toks if has_usage else -1
                    # Python http.server 不会自动 chunked；HTTP/1.1 keep-alive 下若
                    # 不关闭连接，部分客户端会 hang 在 "等响应结束"。SSE 发送完
                    # [DONE] 后主动关闭连接，确保 TurboMind 等客户端正确结束。
                    self.close_connection = True
                    return

                resp, err = upstream.aggregate(rc.raw)
                if err is not None:
                    self._send_openai_error(502, "upstream_parse", err)
                    st.status = 502
                    return
                self._send_json(200, resp)
                st.status = 200
                st.toks = _completion_tokens(resp)
                return
            finally:
                rc.close()
                release_held()

        msg = "all accounts unavailable (cooling/disabled)"
        if last_err is not None:
            msg += ": " + str(last_err)
        self._send_openai_error(503, "no_healthy_account", msg)
        st.status = 503

    def _apply_error_policy(self, uid: str, kind: ErrKind) -> None:
        cfg = self.cfg
        pool = cfg.pool
        if kind == ErrKind.HARD_CREDIT:
            pool.cooldown_until_tomorrow_4am(uid, "余额不足")
        elif kind == ErrKind.SOFT_RATE:
            pool.cooldown(uid, 1, cfg.soft_cooldown, "429 rate limit")
        elif kind == ErrKind.SESSION_DEAD:
            pool.disable(uid, "12153 session dead")
        elif kind == ErrKind.NOT_FOUND:
            pool.cooldown(uid, 1, cfg.soft_cooldown, "upstream 404")
        elif kind == ErrKind.SERVER:
            pool.note_error(uid)
        # 其余（CLIENT/NONE）：只换号不罚（防雪崩），不喂熔断。


def make_handler(cfg: Config):
    class _H(Handler):
        pass
    _H.cfg = cfg
    return _H


def serve(cfg: Config, listen: str, stop_event: Optional[threading.Event] = None,
          on_stop: Optional[Callable[[], None]] = None):
    host, port = _split_listen(listen)
    handler = make_handler(cfg)
    httpd = ThreadingHTTPServer((host, port), handler)
    if stop_event is None:
        stop_event = threading.Event()

    LOG.info("workbuddy2api listening on %s (api_key=%s)", listen, bool(cfg.api_key))

    def _sig(signum, frame):
        LOG.info("bye")
        stop_event.set()
        if on_stop:
            on_stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    try:
        httpd.serve_forever()
    finally:
        if on_stop:
            on_stop()
        httpd.server_close()


def _split_listen(listen: str):
    # 支持 ":7863" / "0.0.0.0:7863" / "7863" / "localhost:7863"
    s = listen
    if s.startswith(":"):
        s = "0.0.0.0" + s
    if ":" not in s:
        s = "0.0.0.0:" + s
    host, _, port = s.rpartition(":")
    if not host:
        host = "0.0.0.0"
    return host, int(port)
