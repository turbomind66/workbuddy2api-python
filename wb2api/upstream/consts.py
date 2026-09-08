"""consts.py — 上游常量（User-Agent / 来源 / 各 Region 的 chat & billing Base）。

等价于 Go 版 internal/upstream/headers.go 与 client.go 中的常量。
"""
from __future__ import annotations

CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
ORIGIN_CN = "https://www.codebuddy.cn"
ORIGIN_GLOBAL = "https://www.workbuddy.ai"

# chat / billing 各 region 的 Base（client.go New() 默认值）
CHAT_BASE_CN = "https://copilot.tencent.com"
BILLING_BASE_CN = "https://www.codebuddy.cn"
CHAT_BASE_GLOBAL = "https://www.workbuddy.ai"
BILLING_BASE_GLOBAL = "https://www.workbuddy.ai"


def origin_referer_for(region: str) -> str:
    return ORIGIN_GLOBAL if region == "global" else ORIGIN_CN
