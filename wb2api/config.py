"""config.py — 加载 JSON 配置 + WB2A_* 环境变量覆盖 + 时长解析 + region 校验。

等价于 Go 版 cmd/server/config.go。
"""
from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace
from typing import Optional

_DURATION_UNITS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}
_DURATION_RE = re.compile(r"(-?\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")


def parse_duration(s):
    """解析 Go 风格时长字符串（如 "60s"、"30m"、"6h"、"1h30m"、"500ms"）。

    兼容三种输入，避免配置文件里写法不同导致类型错误：
      - 字符串："60s" / "1h30m"（推荐）
      - 数字：60 → 视为 60 秒（int/float）
      - timedelta：原样返回

    返回 datetime.timedelta。非法输入抛 ValueError。
    """
    from datetime import timedelta

    if isinstance(s, timedelta):
        return s
    if isinstance(s, bool):  # bool 是 int 子类，显式排除
        raise ValueError(f"invalid duration: {s!r}")
    if isinstance(s, (int, float)):
        return timedelta(seconds=float(s))

    s = (s or "").strip()
    if not s:
        raise ValueError("empty duration")
    neg = False
    if s.startswith("-"):
        neg = True
        s = s[1:]
    matches = _DURATION_RE.findall(s)
    if not matches:
        raise ValueError(f"invalid duration: {s!r}")
    total = 0.0
    for val, unit in matches:
        total += float(val) * _DURATION_UNITS[unit]
    if neg:
        total = -total
    return timedelta(seconds=total)


def _bool_env(v: Optional[str], default: bool) -> bool:
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    """顶层配置。嵌套子结构用 SimpleNamespace 对齐 Go 的 c.Cooldown.SoftRate 等访问方式。"""

    def __init__(self) -> None:
        # 顶层
        self.listen = ":7863"
        self.api_key = ""
        self.auth_dir = "./auths"
        self.state_file = "./data/state.json"
        self.region = "cn"

        # 嵌套子结构
        self.Cooldown = SimpleNamespace(SoftRate="60s")
        self.Schedule = SimpleNamespace(CheckinHours=[9, 21], KeepaliveHours=[22])
        self.Upstream = SimpleNamespace(TimeoutSeconds=120)
        self.Features = SimpleNamespace(SanitizeBlacklistFingerprints=True)
        self.Upstash = SimpleNamespace(URL="", Token="")
        self.Pool = SimpleNamespace(
            MaxInFlight=3,
            BreakerThreshold=3,
            BreakerCooldown="30m",
            BreakerCooldownMax="6h",
            IdleWeightPerHour=0.5,
            IdleWeightMax=5.0,
        )
        self.SessionSticky = SimpleNamespace(Enabled=True, TTL="30m", GCInterval="5m")

        # 解析后（等价于 Go 的 json:"-" 字段）
        self.SoftRateDur = parse_duration("60s")
        self.BreakerCooldownDur = parse_duration("30m")
        self.BreakerCooldownMaxD = parse_duration("6h")
        self.SessionTTL = parse_duration("30m")
        self.SessionGCInterval = parse_duration("5m")

    # ---- 默认值工厂 ----
    @classmethod
    def default(cls) -> "Config":
        return cls()

    # ---- 加载 ----
    @classmethod
    def load(cls, path: str) -> "Config":
        c = cls.default()
        if path:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"parse config: {e}")
            c._apply_dict(data)
        c._apply_env()
        c._normalize()
        return c

    def _apply_dict(self, data: dict) -> None:
        if "listen" in data:
            self.listen = data["listen"]
        if "api_key" in data:
            self.api_key = data["api_key"]
        if "auth_dir" in data:
            self.auth_dir = data["auth_dir"]
        if "state_file" in data:
            self.state_file = data["state_file"]
        if "region" in data:
            self.region = data["region"]
        cd = data.get("cooldown")
        if isinstance(cd, dict) and "soft_rate" in cd:
            self.Cooldown.SoftRate = cd["soft_rate"]
        sch = data.get("schedule")
        if isinstance(sch, dict):
            if "checkin_hours" in sch:
                self.Schedule.CheckinHours = sch["checkin_hours"]
            if "keepalive_hours" in sch:
                self.Schedule.KeepaliveHours = sch["keepalive_hours"]
        up = data.get("upstream")
        if isinstance(up, dict) and "timeout_seconds" in up:
            self.Upstream.TimeoutSeconds = up["timeout_seconds"]
        ft = data.get("features")
        if isinstance(ft, dict) and "sanitize_blacklist_fingerprints" in ft:
            self.Features.SanitizeBlacklistFingerprints = ft["sanitize_blacklist_fingerprints"]
        us = data.get("upstash")
        if isinstance(us, dict):
            if "url" in us:
                self.Upstash.URL = us["url"]
            if "token" in us:
                self.Upstash.Token = us["token"]
        pl = data.get("pool")
        if isinstance(pl, dict):
            if "max_in_flight" in pl:
                self.Pool.MaxInFlight = pl["max_in_flight"]
            if "breaker_threshold" in pl:
                self.Pool.BreakerThreshold = pl["breaker_threshold"]
            if "breaker_cooldown" in pl:
                self.Pool.BreakerCooldown = pl["breaker_cooldown"]
            if "breaker_cooldown_max" in pl:
                self.Pool.BreakerCooldownMax = pl["breaker_cooldown_max"]
            if "idle_weight_per_hour" in pl:
                self.Pool.IdleWeightPerHour = pl["idle_weight_per_hour"]
            if "idle_weight_max" in pl:
                self.Pool.IdleWeightMax = pl["idle_weight_max"]
        ss = data.get("session_sticky")
        if isinstance(ss, dict):
            if "enabled" in ss:
                self.SessionSticky.Enabled = ss["enabled"]
            if "ttl" in ss:
                self.SessionSticky.TTL = ss["ttl"]
            if "gc_interval" in ss:
                self.SessionSticky.GCInterval = ss["gc_interval"]

    def _apply_env(self) -> None:
        if v := os.environ.get("WB2A_LISTEN"):
            self.listen = v
        if v := os.environ.get("WB2A_API_KEY"):
            self.api_key = v
        if v := os.environ.get("WB2A_AUTH_DIR"):
            self.auth_dir = v
        if v := os.environ.get("WB2A_STATE_FILE"):
            self.state_file = v
        if v := os.environ.get("WB2A_REGION"):
            self.region = v
        if v := os.environ.get("WB2A_SOFT_RATE"):
            self.Cooldown.SoftRate = v
        if v := os.environ.get("WB2A_TIMEOUT_SECONDS"):
            try:
                self.Upstream.TimeoutSeconds = int(v)
            except ValueError:
                pass
        if v := os.environ.get("WB2A_SANITIZE_FINGERPRINTS"):
            self.Features.SanitizeBlacklistFingerprints = _bool_env(v, True)

    def _normalize(self) -> None:
        self.SoftRateDur = parse_duration(self.Cooldown.SoftRate)
        self.BreakerCooldownDur = parse_duration(self.Pool.BreakerCooldown)
        self.BreakerCooldownMaxD = parse_duration(self.Pool.BreakerCooldownMax)
        self.SessionTTL = parse_duration(self.SessionSticky.TTL)
        self.SessionGCInterval = parse_duration(self.SessionSticky.GCInterval)

        if self.Pool.BreakerThreshold <= 0:
            self.Pool.BreakerThreshold = 3
        if self.Pool.IdleWeightPerHour <= 0:
            self.Pool.IdleWeightPerHour = 0.5
        if self.Pool.IdleWeightMax <= 0:
            self.Pool.IdleWeightMax = 5.0
        if self.Upstream.TimeoutSeconds <= 0:
            self.Upstream.TimeoutSeconds = 120
        if not self.region:
            self.region = "cn"
        self.region = self.region.lower()
        if self.region not in ("cn", "global"):
            raise ValueError(f"region must be cn or global, got {self.region!r}")
        if not (self.listen.startswith(":") or ":" in self.listen):
            self.listen = ":" + self.listen
