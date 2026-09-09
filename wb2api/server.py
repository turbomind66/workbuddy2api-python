"""server.py — 暴露 OpenAI 兼容 HTTP 接口，内部驱动 pool 挑号 + upstream 转发。

等价于 Go 版 internal/server/{handler,logging}.go（http.server 实现，无需第三方 Web 框架）。
"""
from __future__ import annotations

import json
import logging
import os
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

# 这些上游状态码属于「请求体本身不合法」，换号重试不会改变结果，
# 直接透传上游错误，避免白烧 3 次额度且把真实原因（如 model_param_invalid）吞掉。
_NO_ROTATE_STATUS = (400, 415, 422)

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
                 soft_cooldown=None, refresh_skew=None, max_rotate=None,
                 dump_dir="") -> None:
        self.pool: Optional[Pool] = pool
        self.upstream: Optional[Client] = upstream
        self.api_key = api_key
        self.max_rotate = max_rotate if max_rotate is not None else DEFAULT_MAX_ROTATE
        self.session: Optional[Router] = session
        self.sticky_count = sticky_count  # () -> int
        self.redis_mode = redis_mode if redis_mode is not None else "noop"
        self.soft_cooldown = soft_cooldown if soft_cooldown is not None else DEFAULT_SOFT_COOLDOWN
        self.refresh_skew = refresh_skew if refresh_skew is not None else DEFAULT_REFRESH_SKEW
        # 上游 4xx 时把转发体落盘的目录（空=只打日志不落盘）
        self.dump_dir = dump_dir


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


# 上游 chat 接口预期支持的顶层参数。出现名单外的字段，通常就是被上游拒绝的原因
# （上游报错 extError.param 经常为空，不给任何线索，只能靠对比名单自查）。
UPSTREAM_PARAM_WHITELIST = {
    "model", "messages", "stream", "temperature", "top_p", "max_tokens",
    "stop", "n", "user", "tools", "tool_choice", "response_format",
    "stream_options", "reasoning_effort", "reasoningEffort",
    "frequency_penalty", "presence_penalty", "seed", "top_k",
}


def _summarize_body(obj: Dict[str, Any]) -> str:
    """把转发体压成一行摘要：非标准字段 / 消息角色 / 内容形态 / 工具。"""
    keys = sorted(obj.keys())
    unknown = [k for k in keys if k not in UPSTREAM_PARAM_WHITELIST]

    roles: List[str] = []
    shapes: set = set()
    msgs = obj.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                shapes.add("msg:非对象")
                continue
            roles.append(str(m.get("role", "?")))
            c = m.get("content")
            if isinstance(c, str):
                shapes.add("content:string")
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict):
                        shapes.add("content:" + str(part.get("type", "?")))
                    else:
                        shapes.add("content:非对象")
            elif c is None:
                shapes.add("content:null")
            else:
                shapes.add("content:" + type(c).__name__)
            for extra in ("tool_calls", "tool_call_id", "name", "reasoning_content"):
                if extra in m:
                    shapes.add("msg:" + extra)

    parts = [f"model={obj.get('model')!r}", f"keys={keys}"]
    if unknown:
        parts.append(f"⚠非标准字段={unknown}")
    parts.append(f"roles={roles}")
    parts.append(f"形态={sorted(shapes)}")
    tools = obj.get("tools")
    if tools is not None:
        parts.append(f"tools={len(tools) if isinstance(tools, list) else type(tools).__name__}")
    if "tool_choice" in obj:
        parts.append(f"tool_choice={obj['tool_choice']!r}")
    if "stream_options" in obj:
        parts.append(f"stream_options={obj['stream_options']!r}")
    if "reasoning_effort" in obj:
        parts.append(f"reasoning_effort={obj['reasoning_effort']!r}")
    if "reasoningEffort" in obj:
        parts.append(f"reasoningEffort={obj['reasoningEffort']!r}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # type: ignore
    protocol_version = "HTTP/1.1"
    _headers_sent = False  # 是否已发出响应头（决定异常时能否补一个 500）

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

    def send_response(self, code, message=None):
        self._headers_sent = True
        super().send_response(code, message)

    def handle(self):
        # 客户端在请求任意阶段断开（WinError 10053/10054 等）时静默关闭连接，
        # 避免 socketserver 框架层打印整段 traceback 噪音。
        try:
            super().handle()
        except ConnectionError:
            self.close_connection = True
        except Exception:
            # 未预期异常：补一个 500 JSON，而不是直接掐断连接让客户端只看到
            # "连接被重置"；traceback 仍打日志便于定位。
            LOG.exception("unhandled error serving %s", self.path)
            self.close_connection = True
            try:
                if not self._headers_sent:
                    self._send_openai_error(500, "internal_error", "internal server error")
            except Exception:  # noqa
                pass

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

    def _dump_bad_request(self, prepared: bytes, status: int) -> None:
        """上游 4xx 时打印转发体摘要并落盘，用于定位被拒绝的参数。

        上游 extError.param 经常为空（不告诉是哪个字段），只能靠对比
        UPSTREAM_PARAM_WHITELIST 自查 + 人工查看完整转发体。
        """
        try:
            obj = json.loads(prepared)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return
        LOG.warning("upstream %d 转发体摘要: %s", status, _summarize_body(obj))

        d = getattr(self.cfg, "dump_dir", "") or ""
        if not d:
            return
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "last_bad_request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            LOG.warning("完整转发体已写入: %s", path)
        except OSError as e:
            LOG.warning("dump bad request failed: %s", e)

    def _send_upstream_error(self, status: int, body_txt: str) -> None:
        """把上游业务错误以 OpenAI 错误格式透传，尽量保留 code/msg/ext/requestId。"""
        code = "upstream_error"
        msg = (body_txt or "").strip()[:2000] or f"upstream http {status}"
        try:
            env = json.loads(body_txt)
        except (ValueError, TypeError):
            env = None
        if isinstance(env, dict):
            parts: List[str] = []
            if env.get("code") is not None:
                code = str(env["code"])
                parts.append(f"code={env['code']}")
            if env.get("msg"):
                parts.append(str(env["msg"]))
            ext = env.get("extError")
            if isinstance(ext, dict):
                if ext.get("code"):
                    parts.append(f"ext={ext['code']}")
                if ext.get("message"):
                    parts.append(str(ext["message"]))
            if env.get("requestId"):
                parts.append(f"requestId={env['requestId']}")
            if parts:
                msg = " | ".join(parts)
        self._send_openai_error(status, code, msg)

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
            # sticky_uid 在下面被重新赋值，必须声明 nonlocal，否则它会被视为 fail()
            # 的局部变量，上一行的读取将抛 UnboundLocalError（Python 闭包作用域陷阱）。
            nonlocal sticky_uid
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
                body_txt = resp_body.decode("utf-8", "replace") if resp_body else ""
                kind = classify(status, body_txt)
                last_err = upstream.Error(kind, status, body_txt[:200])
                self._apply_error_policy(acct.uid, kind)
                # 请求体级别的参数错误（400/415/422）换号重试毫无意义：请求体不变，
                # 换任何账号都会被同一条上游校验规则拒绝。直接把上游原始错误透传，
                # 既省额度也让客户端看到真实原因（如 model_param_invalid）。
                if kind == ErrKind.CLIENT and status in _NO_ROTATE_STATUS:
                    LOG.warning("chat uid=%s: upstream %d 参数错误，停止换号重试并透传：%s",
                                _uid_prefix(acct.uid), status, body_txt[:300])
                    self._dump_bad_request(up.prepare_body(body), status)
                    release_held()
                    self._send_upstream_error(status, body_txt)
                    return
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

        msg = self._diagnose_no_account(last_err)
        self._send_openai_error(503, "no_healthy_account", msg)
        st.status = 503

    def _diagnose_no_account(self, last_err) -> str:
        """选号失败时给出可操作的诊断信息，避免一律报 "cooling/disabled" 误导排查。"""
        total, healthy, cooling, disabled, in_flight_full = self.cfg.pool.counts_detailed()
        if total == 0:
            msg = ("no accounts loaded: 账号池为空，检查 config.json 的 auth_dir "
                   "（当前相对路径基于项目根）与 auths/workbuddy-*.json 是否存在")
        elif disabled == total:
            msg = "all accounts disabled: 全部账号已被禁用（凭证失效），需重新 login"
        elif cooling == total:
            msg = "all accounts cooling: 全部账号处于冷却/熔断中，稍后自动恢复，或访问 /status 查看剩余时间"
        elif healthy > 0 and in_flight_full >= healthy:
            msg = "all accounts in-flight full: 全部可用账号的并发已达上限，稍后重试"
        else:
            msg = "all accounts unavailable (cooling/disabled)"
        if last_err is not None:
            msg += ": " + str(last_err)
        return msg

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
