"""client.py — 封装对 CodeBuddy 上游（chat / billing / auth）的全部 HTTP 调用，及错误分类。

等价于 Go 版 internal/upstream/client.go。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

from wb2api.auth import Auth
from wb2api.upstream import errors, sse
from wb2api.upstream.consts import (
    BILLING_BASE_CN, BILLING_BASE_GLOBAL, CHAT_BASE_CN, CHAT_BASE_GLOBAL, CLIENT_UA,
)
from wb2api.upstream import headers as H
from wb2api.upstream import payload as P

LOG = logging.getLogger("wb2api.upstream")


@dataclass
class ModelInfo:
    id: str = ""
    name: str = ""
    context_window: int = 0   # = maxInputTokens
    max_tokens: int = 0       # = maxOutputTokens
    efforts: List[str] = field(default_factory=list)  # reasoning.supportedEfforts


class Client:
    """上游 HTTP 客户端。Base 字段可覆盖便于测试。"""

    def __init__(self) -> None:
        self.http_timeout = 120
        self.sanitize_fingerprints = True

        self.chat_base_cn = CHAT_BASE_CN
        self.billing_base_cn = BILLING_BASE_CN
        self.chat_base_global = CHAT_BASE_GLOBAL
        self.billing_base_global = BILLING_BASE_GLOBAL

        self._efforts: Dict[str, List[str]] = {}
        self._efforts_lock = threading.RLock()

        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20, pool_maxsize=20, max_retries=0
        )
        self.http = requests.Session()
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    # ---- base 选择 ----
    def chat_base(self, a: Optional[Auth]) -> str:
        return self.chat_base_global if (a is not None and a.region() == "global") else self.chat_base_cn

    def billing_base(self, a: Optional[Auth]) -> str:
        return self.billing_base_global if (a is not None and a.region() == "global") else self.billing_base_cn

    # ---- effort 缓存 ----
    def efforts_snapshot(self) -> Optional[Dict[str, List[str]]]:
        with self._efforts_lock:
            if not self._efforts:
                return None
            return dict(self._efforts)

    def _set_efforts(self, cache: Dict[str, List[str]]) -> None:
        with self._efforts_lock:
            self._efforts = cache

    # ---- 请求体改写 ----
    def prepare_body(self, body: bytes) -> bytes:
        return P.prepare_body_opt(body, self.sanitize_fingerprints, self.efforts_snapshot())

    # ---- 信封请求 ----
    def do_json(self, method: str, url: str, hdr: Dict[str, str],
                json_body: Any = None) -> Tuple[Optional[Any], Optional[errors.Error]]:
        """发请求并解信封；HTTP 非 2xx 或业务 code != 0 时返回带 body 片段的 Error。"""
        try:
            resp = self.http.request(
                method, url, headers=hdr, json=json_body,
                timeout=self.http_timeout,
            )
        except requests.RequestException as e:
            return None, errors.Error(errors.ErrKind.NONE, 0, f"transport: {e}")
        raw = resp.content[:1 << 20]
        text = raw.decode("utf-8", "replace")
        if resp.status_code >= 400:
            kind = errors.classify(resp.status_code, text)
            return None, errors.Error(kind, resp.status_code, errors.truncate(text, 200))
        try:
            env = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None, errors.Error(
                errors.ErrKind.CLIENT, resp.status_code,
                f"parse failed: body {errors.truncate(text, 120)}",
            )
        code = env.get("code", 0)
        if code != 0:
            msg = env.get("msg", "")
            kind = errors.classify(resp.status_code, msg)
            if kind == errors.ErrKind.NONE:
                kind = errors.ErrKind.CLIENT
            return None, errors.Error(kind, resp.status_code, f"code={code} msg={errors.truncate(msg, 160)}")
        return env.get("data"), None

    # ---- refresh ----
    def refresh_token(self, a: Auth) -> Optional[errors.Error]:
        """刷新 access token；成功时更新 a 的字段（缺省值保留旧值）。调用方负责 save_atomic。"""
        with a.mu:
            if not a.refresh_token or not a.refresh_token.strip():
                return errors.Error(errors.ErrKind.CLIENT, 0, "no refreshToken")
            url = self.chat_base(a) + "/v2/plugin/auth/token/refresh"
            data, err = self.do_json("POST", url, H.refresh_headers(a))
            if err is not None:
                return err
            if not isinstance(data, dict):
                return errors.Error(errors.ErrKind.CLIENT, 0, "refresh_failed: unexpected response")
            tok = data
            at = tok.get("accessToken")
            if not at:
                return errors.Error(errors.ErrKind.CLIENT, 0, "refresh_failed: no accessToken in response — re-login required")
            a.access_token = at
            if tok.get("refreshToken"):
                a.refresh_token = tok["refreshToken"]
            if tok.get("domain"):
                a.domain = tok["domain"]
            # preserveExpiry：响应缺 expiresIn 时保留旧过期时间，避免刷新风暴。
            ei = tok.get("expiresIn")
            if isinstance(ei, (int, float)) and ei > 0:
                a.expires_at = int(time.time()) + int(ei)
            return None

    # ---- chat stream ----
    def chat_stream(self, a: Auth, body: bytes):
        """发 chat 请求并返回原始 SSE 流（调用方负责关闭）。

        返回 (resp, status, resp_body, err)：
          - 非 2xx：resp=None, status=状态码, resp_body=上游响应体, err=None
          - 传输层失败：resp=None, status=0, resp_body=None, err=异常
          - 成功：resp=requests.Response(stream), status=200, resp_body=None, err=None
        """
        url = self.chat_base(a) + "/v2/chat/completions"
        hdr = H.chat_headers(a)
        try:
            resp = self.http.request(
                "POST", url, headers=hdr, data=self.prepare_body(body),
                stream=True, timeout=self.http_timeout,
            )
        except requests.RequestException as e:
            LOG.error("chat_stream uid=%s: transport error: %s", a.uid, e)
            return None, 0, None, e
        if resp.status_code >= 400:
            raw = resp.content[:1 << 20]
            resp.close()
            kind = errors.classify(resp.status_code, raw.decode("utf-8", "replace"))
            LOG.error("chat_stream uid=%s: upstream %d %s body=%s",
                      a.uid, resp.status_code, errors.kind_name(kind),
                      errors.truncate(raw.decode("utf-8", "replace"), 200))
            return None, resp.status_code, raw, None
        try:
            resp.raw.decode_content = True
        except Exception:
            pass
        return resp, resp.status_code, None, None

    # ---- fetch models ----
    def fetch_models(self, a: Auth) -> Tuple[List[ModelInfo], Optional[errors.Error]]:
        url = self.chat_base(a) + "/console/enterprises/personal/models"
        hdr = {
            "Authorization": "Bearer " + a.access_token,
            "Accept": "application/json",
        }
        origin = "https://www.workbuddy.ai" if a.region() == "global" else "https://www.codebuddy.cn"
        hdr["Origin"] = origin
        hdr["Referer"] = origin + "/"
        hdr["User-Agent"] = H.CLIENT_UA if hasattr(H, "CLIENT_UA") else "CLI/2.63.2 CodeBuddy/2.63.2"
        try:
            resp = self.http.get(url, headers=hdr, timeout=self.http_timeout)
        except requests.RequestException as e:
            return [], errors.Error(errors.ErrKind.NONE, 0, f"transport: {e}")
        raw = resp.content[:1 << 20]
        text = raw.decode("utf-8", "replace")
        if resp.status_code != 200:
            return [], errors.Error(errors.ErrKind.CLIENT, resp.status_code,
                                    f"models api status {resp.status_code}: {errors.truncate(text, 120)}")
        try:
            env = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return [], errors.Error(errors.ErrKind.CLIENT, resp.status_code, "models parse error")
        if env.get("code", 0) != 0:
            return [], errors.Error(errors.ErrKind.CLIENT, resp.status_code,
                                    f"models api code={env.get('code')}")
        data = env.get("data") or {}
        models = data.get("models") or []
        agents = data.get("agents") or []

        cli_ids: List[str] = []
        for ag in agents:
            if ag.get("name") == "cli":
                cli_ids = ag.get("models") or []
                break
        if not cli_ids:
            return [], errors.Error(errors.ErrKind.CLIENT, resp.status_code, "no cli agent models found")

        dyn_map = {m.get("id"): m for m in models}
        out: List[ModelInfo] = []
        for mid in cli_ids:
            m = dyn_map.get(mid)
            if not m or m.get("disabled"):
                continue
            reasoning = m.get("reasoning") or {}
            out.append(ModelInfo(
                id=m.get("id", ""),
                name=m.get("name", ""),
                context_window=int(m.get("maxInputTokens", 0) or 0),
                max_tokens=int(m.get("maxOutputTokens", 0) or 0),
                efforts=list(reasoning.get("supportedEfforts") or []),
            ))
        if not out:
            return [], errors.Error(errors.ErrKind.CLIENT, resp.status_code, "models api returned empty list")

        cache = {mi.id: mi.efforts for mi in out if mi.efforts}
        self._set_efforts(cache)
        return out, None

    # ---- user resource ----
    def user_resource(self, a: Auth) -> Tuple[int, Optional[errors.Error]]:
        """查询账号当前可花费积分余额。

        优先查企业套餐（get-user-resource，所有套餐 CycleCapacity 聚合，负值钳 0）；
        个人免费账号无套餐（上游返回 code=10085），退化为查询签到活动积分
        （checkin-activity-status 的 total_credits）。
        """
        url = self.billing_base(a) + "/v2/billing/meter/get-user-resource"
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        future = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 365 * 101 * 24 * 3600))
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": now,
            "PackageEndTimeRangeEnd": future,
        }
        data, err = self.do_json("POST", url, H.billing_headers(a), json_body=body)
        if err is not None:
            # 只有上游明确返回「无套餐」(code=10085) 时才降级到签到积分；
            # 其他错误（网络、鉴权、过期等）仍按原错误返回，避免误导。
            if err.status == 403 and "10085" in err.msg:
                total, cerr = self.checkin_total_credits(a)
                if cerr is None:
                    return total, None
            return 0, err
        if not isinstance(data, dict):
            return 0, errors.Error(errors.ErrKind.CLIENT, 0, "resource: bad data")
        resp = data.get("Response", {})
        d = resp.get("Data", {})
        accounts = d.get("Accounts", []) or []
        remain = 0
        for acct in accounts:
            cycle_size = int(acct.get("CycleCapacitySize", 0) or 0)
            cycle_remain = int(acct.get("CycleCapacityRemain", 0) or 0)
            cycle_used = int(acct.get("CycleCapacityUsed", 0) or 0)
            cap_remain = int(acct.get("CapacityRemain", 0) or 0)
            if cycle_size > 0:
                r = cycle_remain
            elif cycle_remain > 0 or cycle_used > 0:
                r = cycle_remain
            else:
                r = cap_remain
            if r < 0:
                r = 0
            remain += r
        return remain, None

    def checkin_total_credits(self, a: Auth) -> Tuple[int, Optional[errors.Error]]:
        """查询签到活动累计积分（个人免费账号的主要积分来源）。"""
        url = self.billing_base(a) + "/v2/billing/meter/checkin-activity-status"
        data, err = self.do_json("POST", url, H.billing_headers(a), json_body={})
        if err is not None:
            return 0, err
        if not isinstance(data, dict):
            return 0, errors.Error(errors.ErrKind.CLIENT, 0, "checkin: bad data")
        return int(data.get("total_credits", 0) or 0), None

    # ---- daily checkin ----
    def daily_checkin(self, a: Auth) -> Optional[errors.Error]:
        url = self.billing_base(a) + "/v2/billing/meter/daily-checkin"
        _, err = self.do_json("POST", url, H.billing_headers(a), json_body={})
        return err
