"""payload.py — 改写发往上游的 chat 请求体：

1. 强制 stream:true（上游拒绝非流式）
2. tool_choice 归一化（上游该字段是 string，对象形式会 400 code=11101）
3. reasoning_effort 按模型 supportedEfforts 降级

等价于 Go 版 internal/upstream/payload.go。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

# effortRank 档位从低到高。
EFFORT_RANK = {
    "off": 0, "minimal": 1, "low": 2, "medium": 3,
    "high": 4, "xhigh": 5, "max": 6,
}


def prepare_body_opt(src: bytes, sanitize: bool,
                     efforts: Optional[Dict[str, List[str]]] = None) -> bytes:
    """单 pass 改写；sanitize=False 时行为完全还原（仅强制 stream + 归一化 tool_choice）。"""
    if not src:
        return src
    try:
        obj = json.loads(src)
    except (json.JSONDecodeError, ValueError):
        return src
    if not isinstance(obj, dict):
        return src

    obj["stream"] = True
    normalize_tool_choice(obj)
    normalize_reasoning_effort(obj, efforts)
    if sanitize:
        msgs = obj.get("messages")
        if isinstance(msgs, list):
            from wb2api.upstream.sanitize import sanitize_messages

            sanitize_messages(msgs)
    try:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return src


def normalize_reasoning_effort(obj: Dict[str, Any],
                              efforts: Optional[Dict[str, List[str]]]) -> None:
    """按模型 supportedEfforts 降级 reasoning_effort（snake/camel 双字段兼容）。"""
    if not efforts:
        return
    model = obj.get("model")
    if not isinstance(model, str) or not model:
        return
    supported = efforts.get(model)
    if not supported:
        return
    key = ""
    if "reasoning_effort" in obj:
        key = "reasoning_effort"
    elif "reasoningEffort" in obj:
        key = "reasoningEffort"
    else:
        return
    req_str = obj.get(key)
    if not isinstance(req_str, str):
        return
    req_str = req_str.strip().lower()
    req_idx = EFFORT_RANK.get(req_str)
    if req_idx is None:
        return

    # 在 ≤请求档位的支持档里选最高档；命中且与请求不同才改写。
    best, best_idx = "", -1
    for s in supported:
        idx = EFFORT_RANK.get(s.strip().lower())
        if idx is not None and idx <= req_idx and idx > best_idx:
            best, best_idx = s, idx
    if best:
        if best.lower() != req_str:
            obj[key] = best
            logging.getLogger("wb2api.upstream").info(
                "reasoning_effort downgraded model=%s %s -> %s", model, req_str, best)
        return
    # 支持档全部高于请求档：取最低支持档。
    lowest, lowest_idx = "", 1 << 30
    for s in supported:
        idx = EFFORT_RANK.get(s.strip().lower())
        if idx is not None and idx < lowest_idx:
            lowest, lowest_idx = s, idx
    if lowest:
        obj[key] = lowest
        logging.getLogger("wb2api.upstream").info(
            "reasoning_effort floored model=%s %s -> %s", model, req_str, lowest)


def normalize_tool_choice(obj: Dict[str, Any]) -> None:
    """按上游 Go struct（string 类型）改写 OpenAI tool_choice。"""
    def suppress() -> None:
        obj.pop("tools", None)
        obj.pop("functions", None)

    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            obj.pop("tool_choice", None)
            suppress()
    elif isinstance(tc, dict):
        typ = str(tc.get("type", "")).lower().strip()
        if typ == "none":
            obj.pop("tool_choice", None)
            suppress()
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = ""
            fn = tc.get("function")
            if isinstance(fn, dict):
                name = fn.get("name", "")
            if not name:
                name = tc.get("name", "")
            name = name.strip()
            if name:
                obj["tool_choice"] = name
            else:
                obj["tool_choice"] = "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)
