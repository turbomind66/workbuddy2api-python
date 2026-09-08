"""sse.py — 处理上游 SSE 流：聚合成单个 OpenAI 响应，或透传给客户端。

等价于 Go 版 internal/upstream/sse.go。Stream 额外返回 (ttfb_ms, toks, has_usage, err)
供请求级日志使用（对应 Go 的 chatStatsReader）。
"""
from __future__ import annotations

import io
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

WHITELIST_TOP = [
    "id", "object", "created", "model", "system_fingerprint", "service_tier",
]


def merge_tool_call_delta(merged: Dict[str, Any], delta: Dict[str, Any]) -> None:
    """把流式 tool_call 片段合并到累计对象。"""
    v = delta.get("id")
    if isinstance(v, str) and v:
        merged["id"] = v
    v = delta.get("type")
    if isinstance(v, str) and v:
        merged["type"] = v
    df = delta.get("function")
    if not isinstance(df, dict):
        return
    mf = merged.get("function")
    if not isinstance(mf, dict):
        mf = {}
        merged["function"] = mf
    v = df.get("name")
    if isinstance(v, str) and v:
        mf["name"] = v
    v = df.get("arguments")
    if isinstance(v, str) and v:
        prev = mf.get("arguments", "")
        if not isinstance(prev, str):
            prev = ""
        mf["arguments"] = (prev + v) if prev else v


def aggregate(src: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """读取完整 SSE 流，聚合 delta.content 为单个 OpenAI chat.completion 响应。"""
    reader = io.TextIOWrapper(src, encoding="utf-8", errors="replace", newline="")
    id_ = ""
    model = ""
    created = 0.0
    content: List[str] = []
    reasoning: List[str] = []
    role = "assistant"
    finish_reason = "stop"
    usage: Any = None
    got_any_content = False
    valid_events = 0
    tool_calls: Dict[int, Dict[str, Any]] = {}
    tool_order: List[int] = []

    for line in reader:
        line = line.rstrip("\r\n")
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(chunk, dict):
            continue
        valid_events += 1
        v = chunk.get("id")
        if isinstance(v, str) and not id_:
            id_ = v
        v = chunk.get("model")
        if isinstance(v, str) and not model:
            model = v
        v = chunk.get("created")
        if isinstance(v, (int, float)) and created == 0:
            created = float(v)
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for c in chunk.get("choices", []) or []:
            if not isinstance(c, dict):
                continue
            fr = c.get("finish_reason")
            if isinstance(fr, str) and fr:
                finish_reason = fr
            delta = c.get("delta")
            if isinstance(delta, dict):
                rv = delta.get("role")
                if isinstance(rv, str) and rv:
                    role = rv
                ct = delta.get("content")
                if isinstance(ct, str):
                    content.append(ct)
                    got_any_content = True
                rc = delta.get("reasoning_content")
                if isinstance(rc, str):
                    reasoning.append(rc)
                for tc in delta.get("tool_calls", []) or []:
                    if not isinstance(tc, dict):
                        continue
                    idx = int(tc.get("index", 0) or 0)
                    merged = tool_calls.setdefault(idx, {"index": idx})
                    if idx not in tool_order:
                        tool_order.append(idx)
                    merge_tool_call_delta(merged, tc)
            # 有的上游把完整消息放在 message 里（非 delta）
            msg = c.get("message")
            if isinstance(msg, dict) and not got_any_content:
                ct = msg.get("content")
                if isinstance(ct, str):
                    content.append(ct)

    if valid_events == 0:
        return None, "upstream stream contained no valid data events"
    if not id_:
        id_ = "chatcmpl-%d" % int(time.time() * 1e9)
    if created == 0:
        created = float(int(time.time()))

    message: Dict[str, Any] = {"role": role, "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_order:
        tool_order.sort()
        message["tool_calls"] = [tool_calls[i] for i in tool_order]

    resp: Dict[str, Any] = {
        "id": id_,
        "object": "chat.completion",
        "created": int(created),
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ],
    }
    if usage is not None:
        resp["usage"] = usage
    return resp, None


def normalize_frame(obj: Dict[str, Any]) -> Dict[str, Any]:
    """以 OpenAI 流式规范白名单重建帧。"""
    out: Dict[str, Any] = {}
    for k in WHITELIST_TOP:
        v = obj.get(k)
        if v is not None:
            out[k] = v
    if "object" not in out:
        out["object"] = "chat.completion.chunk"
    if "id" not in out:
        out["id"] = "chatcmpl-wb2api"
    chs = obj.get("choices")
    if isinstance(chs, list):
        nchs = []
        for ci in chs:
            if not isinstance(ci, dict):
                continue
            nc: Dict[str, Any] = {}
            if "index" in ci:
                nc["index"] = ci["index"]
            delta: Dict[str, Any] = {}
            d = ci.get("delta")
            if isinstance(d, dict):
                for fk in ("role", "content", "reasoning_content", "refusal"):
                    v = d.get(fk)
                    if isinstance(v, str) and v:
                        delta[fk] = v
                tcs = d.get("tool_calls")
                if isinstance(tcs, list) and len(tcs) > 0:
                    delta["tool_calls"] = tcs
                fc = d.get("function_call")
                if fc is not None:
                    keep = False
                    if isinstance(fc, dict):
                        n = fc.get("name", "")
                        a = fc.get("arguments", "")
                        keep = bool(n) or bool(a)
                    else:
                        keep = True
                    if keep:
                        delta["function_call"] = fc
            nc["delta"] = delta
            fr = ci.get("finish_reason")
            nc["finish_reason"] = fr if isinstance(fr, str) and fr else None
            nchs.append(nc)
        out["choices"] = nchs
    if "usage" in obj:
        out["usage"] = obj["usage"]
    else:
        out["usage"] = None
    return out


def stream(out: Any, src: Any, start_time: Optional[float] = None):
    """透传上游 SSE 到 out（逐帧规范化后 flush），保证至少写一个 [DONE]。

    out 需支持 .write(bytes) 与 .flush()（如 socket/file）。返回 (ttfb_ms, toks, has_usage, err)。
    """
    if start_time is None:
        start_time = time.time()
    elif isinstance(start_time, datetime):
        start_time = start_time.timestamp()
    ttfb_ms = 0.0
    seen = False
    has_usage = False
    toks = 0
    valid_frames = 0
    client_gone = False

    reader = io.TextIOWrapper(src, encoding="utf-8", errors="replace", newline="")

    def write_raw(payload: str) -> None:
        nonlocal client_gone
        if client_gone:
            return
        try:
            out.write(("data: " + payload + "\n\n").encode("utf-8"))
            out.flush()
        except OSError:
            client_gone = True

    def write_frame(payload: str) -> None:
        nonlocal valid_frames, client_gone
        if client_gone:
            return
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            write_raw(payload)
            return
        norm = normalize_frame(obj)
        try:
            out.write(("data: " + json.dumps(norm, ensure_ascii=False) + "\n\n").encode("utf-8"))
            out.flush()
        except OSError:
            client_gone = True
            return
        valid_frames += 1

    for line in reader:
        if client_gone:
            break
        trimmed = line.rstrip("\r\n")
        if trimmed.startswith("data: [DONE]"):
            break
        if trimmed.startswith("data: "):
            payload = trimmed[len("data: "):]
            if not seen:
                seen = True
                ttfb_ms = (time.time() - start_time) * 1000.0
            try:
                obj = json.loads(payload)
                u = obj.get("usage")
                if isinstance(u, dict) and "completion_tokens" in u:
                    has_usage = True
                    toks = u["completion_tokens"]
            except (json.JSONDecodeError, ValueError):
                pass
            write_frame(payload)
        elif trimmed.strip() != "":
            try:
                out.write((trimmed + "\n").encode("utf-8"))
                out.flush()
            except OSError:
                client_gone = True
                break

    if client_gone:
        return ttfb_ms, toks, has_usage, None
    if valid_frames == 0:
        write_raw('{"error":{"message":"empty upstream stream","type":"upstream_error"}}')
    try:
        out.write(b"data: [DONE]\n\n")
        out.flush()
    except OSError:
        client_gone = True
    if valid_frames == 0:
        return ttfb_ms, toks, has_usage, "upstream stream contained no valid data events"
    return ttfb_ms, toks, has_usage, None
