"""errors.py — 上游错误分类（驱动 pool 冷却状态机）。

等价于 Go 版 internal/upstream/client.go 中的 ErrKind / Error / Classify。
"""
from __future__ import annotations

from enum import IntEnum
from typing import Optional

try:
    from enum import StrEnum  # py>=3.11
except Exception:  # pragma: no cover
    StrEnum = None  # type: ignore


class ErrKind(IntEnum):
    """错误分类，pool 据此决定冷却时长。值对齐 Go iota 次序。"""

    NONE = 0          # 成功
    HARD_CREDIT = 1   # 余额不足（402 或 body 关键词）→ 长冷却
    SOFT_RATE = 2     # 429 软限流 → 短冷却
    SESSION_DEAD = 3  # 401 + 12153 offline session 失效 → 禁用
    NOT_FOUND = 4     # 404 上游偶发 → 短冷却，不累计错误计数（防雪崩）
    SERVER = 5        # 5xx 上游故障
    CLIENT = 6        # 其他 4xx / 业务错误


_KIND_NAMES = {
    ErrKind.NONE: "none",
    ErrKind.HARD_CREDIT: "hard_credit",
    ErrKind.SOFT_RATE: "soft_rate",
    ErrKind.SESSION_DEAD: "session_dead",
    ErrKind.NOT_FOUND: "not_found",
    ErrKind.SERVER: "server",
    ErrKind.CLIENT: "client",
}


def kind_name(k: ErrKind) -> str:
    return _KIND_NAMES.get(k, "none")


# 余额不足关键词（小写比较 + 中文原文比较双通道）。
HARD_MARKERS = [
    "insufficient credit", "no credit", "credit exhausted", "out of credit",
    "quota exceeded", "quota exhaust", "payment required", "credit not enough",
    "not enough credit",
    "积分不足", "额度不足", "余额不足", "积分用完", "额度用尽", "没有积分",
]

SESSION_DEAD_MARKERS = ["Offline user session not found", "12153"]


class Error(Exception):
    """带分类的上游错误。"""

    def __init__(self, kind: ErrKind, status: int, msg: str) -> None:
        self.kind = kind
        self.status = status
        self.msg = msg
        super().__init__(f"upstream {kind_name(kind)} (http {status}): {msg}")


def classify(status: int, body: str) -> ErrKind:
    """按 HTTP 状态码 + body 判定错误类别。"""
    if status == 402:
        return ErrKind.HARD_CREDIT
    lower = body.lower()
    for m in HARD_MARKERS:
        if m.lower() in lower or m in body:
            return ErrKind.HARD_CREDIT
    for m in SESSION_DEAD_MARKERS:
        if m in body:
            return ErrKind.SESSION_DEAD
    if status == 429:
        return ErrKind.SOFT_RATE
    if status == 404:
        return ErrKind.NOT_FOUND
    if status >= 500:
        return ErrKind.SERVER
    if status >= 400:
        return ErrKind.CLIENT
    # HTTP 200 但业务 code 非 0 且含余额关键词的情况已被上面 HARD_MARKERS 捕获。
    return ErrKind.NONE


def truncate(s: str, n: int) -> str:
    s = s.strip()
    if len(s) > n:
        return s[:n]
    return s


def api_envelope(raw: bytes):
    """解析上游统一信封 {code,msg,data}；返回 (data_obj, code, msg)。"""
    import json

    env = json.loads(raw)
    return env.get("data"), env.get("code", 0), env.get("msg", "")
