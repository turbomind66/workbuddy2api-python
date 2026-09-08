"""upstream 包：对 CodeBuddy 上游（chat / billing / auth）的全部 HTTP 封装。"""
from wb2api.upstream.errors import (
    ErrKind, Error, classify, kind_name,
)
from wb2api.upstream.client import Client, ModelInfo
from wb2api.upstream.sse import (
    aggregate, stream, normalize_frame, merge_tool_call_delta,
)
from wb2api.upstream.payload import prepare_body_opt, normalize_tool_choice, normalize_reasoning_effort
from wb2api.upstream.sanitize import sanitize_messages, sanitize_text
from wb2api.upstream.headers import (
    common_headers, chat_headers, billing_headers, refresh_headers,
)

__all__ = [
    "ErrKind", "Error", "classify", "kind_name",
    "Client", "ModelInfo",
    "aggregate", "stream", "normalize_frame", "merge_tool_call_delta",
    "prepare_body_opt", "normalize_tool_choice", "normalize_reasoning_effort",
    "sanitize_messages", "sanitize_text",
    "common_headers", "chat_headers", "billing_headers", "refresh_headers",
]
