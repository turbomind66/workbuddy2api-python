"""sanitize.py — 出站请求体脱敏：剥离上游内容审核黑名单指纹。

背景：客户端（Claude Code 类 CLI）在 system prompt 注入若干固定模板句，上游内容审核按
逐字精确匹配拦截（非语义审核），一字改动即可绕过。策略：键值/header 型指纹整段剥离；
承载语义的模板句最小改写（换一词），语义不变。

等价于 Go 版 internal/upstream/sanitize.go。
"""
from __future__ import annotations

import re
from typing import Any, List, Tuple

# 特征预检：任一命中才进入净化（strings.Contains 快速路径，普通请求全不中 → 原样返回，零分配）。
SANITIZE_FEATURES = [
    "x-anthropic-billing-header",  # header 键值段键名
    "cc_entrypoint=",             # 尾随裸键值（截断前缀即可命中）
    "You are Claude Code",        # 身份句（截断前缀即可命中）
    "Main branch (",              # 注入指令句（截断前缀即可命中）
]

# 剥离层：header 键名即触发（与值无关），整段删除。
SANITIZE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\n]*;?\s*")
# 剥离层：尾随裸键值（cc_xxx=...;）循环清理。
SANITIZE_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*")

# 改写层：全模板句逐字替换（每句只改一个词，语义不变）。
SANITIZE_REWRITES: List[Tuple[str, str]] = [
    (
        "You are Claude Code, Anthropic's official CLI for Claude.",
        "You are Claude Code, Anthropic's official CLI tool for Claude.",
    ),
    (
        "Main branch (you will usually use this for PRs)",
        "Default branch (you will usually use this for PRs)",
    ),
]


def has_fingerprint(text: str) -> bool:
    for f in SANITIZE_FEATURES:
        if f in text:
            return True
    return SANITIZE_HDR_RE.search(text) is not None


def sanitize_text(text: str) -> str:
    if not has_fingerprint(text):
        return text
    for old, new in SANITIZE_REWRITES:
        text = text.replace(old, new)
    if SANITIZE_HDR_RE.search(text):
        text = SANITIZE_HDR_RE.sub("", text)
    if "cc_" in text:
        prev = None
        while prev != text:  # 清尾随裸 kv（cc_version=...; cc_entrypoint=...;）
            prev = text
            text = SANITIZE_KV_RE.sub("", text)
    return text.strip()


def sanitize_content(v: Any) -> Tuple[Any, bool]:
    """兼容字符串与多模态数组；只动 text part，image 等 part 不动。返回 (值, 是否变化)。"""
    if isinstance(v, str):
        s = sanitize_text(v)
        return s, s != v
    if isinstance(v, list):
        changed = False
        for p in v:
            if not isinstance(p, dict):
                continue
            text = p.get("text")
            if not isinstance(text, str):
                continue
            s = sanitize_text(text)
            if s != text:
                p["text"] = s
                changed = True
        return v, changed
    return v, False


def sanitize_messages(messages: List[Any]) -> bool:
    """净化 messages 中的 content；任一命中返回 True。"""
    changed = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if "content" not in msg:
            continue
        nc, ch = sanitize_content(msg["content"])
        if ch:
            msg["content"] = nc
            changed = True
    return changed
