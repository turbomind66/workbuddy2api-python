"""headers.py — 构造三类上游请求头（common / chat / billing / refresh）。

等价于 Go 版 internal/upstream/headers.go。返回 dict[str, str] 便于 requests 直接使用。
"""
from __future__ import annotations

from typing import Dict

from wb2api.auth import Auth
from wb2api.upstream.consts import CLIENT_UA, origin_referer_for


def common_headers(a: Auth) -> Dict[str, str]:
    origin = origin_referer_for(a.region())
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": CLIENT_UA,
    }


def chat_headers(a: Auth) -> Dict[str, str]:
    h = common_headers(a)
    if a.access_token:
        h["Authorization"] = "Bearer " + a.access_token
    else:
        h["X-No-Authorization"] = "1"
    if a.uid:
        h["X-User-Id"] = a.uid
    else:
        h["X-No-User-Id"] = "1"
    if a.enterprise_id:
        h["X-Enterprise-Id"] = a.enterprise_id
    else:
        h["X-No-Enterprise-Id"] = "1"
    # 安全红线：绝不在 chat 请求里携带 X-Refresh-Token。
    if a.domain:
        h["X-Domain"] = a.domain
    else:
        h["X-No-Department-Info"] = "1"
    h["X-Product"] = "SaaS"
    return h


def billing_headers(a: Auth) -> Dict[str, str]:
    h = {
        "Authorization": "Bearer " + a.access_token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": CLIENT_UA,
    }
    if a.uid:
        h["X-User-Id"] = a.uid
    if a.enterprise_id:
        h["X-Enterprise-Id"] = a.enterprise_id
        h["X-Tenant-Id"] = a.enterprise_id
    if a.domain:
        h["X-Domain"] = a.domain
    return h


def refresh_headers(a: Auth) -> Dict[str, str]:
    h = common_headers(a)
    h["X-Refresh-Token"] = a.refresh_token
    if a.enterprise_id:
        h["X-Enterprise-Id"] = a.enterprise_id
    h["X-Auth-Refresh-Source"] = "workbuddy"
    return h
