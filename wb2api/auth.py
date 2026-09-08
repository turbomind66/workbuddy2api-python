"""auth.py — WorkBuddy auth 文件解析（嵌套形/扁平形双形态）、region 判定、refresh 后原子写回。

等价于 Go 版 internal/auth/auth.go。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

GLOBAL_SUFFIX = ".workbuddy.ai"


class Auth:
    """归一化后的账号凭证（来源可以是插件 OAuth 嵌套形或 CPA 面板扁平形）。"""

    def __init__(self) -> None:
        # mu 串行化 RefreshToken 写与 SaveAtomic 读，防止并发写回半更新 token。
        self.mu = threading.Lock()

        self.access_token: str = ""
        self.refresh_token: str = ""
        self.expires_at: int = 0  # Unix 秒
        self.domain: str = ""
        self.uid: str = ""
        self.enterprise_id: str = ""
        self.nickname: str = ""
        self.file_path: str = ""  # 来源文件；refresh 后原子写回此处

    # ---- region 判定 ----
    def region(self) -> str:
        """返回 "cn" 或 "global"。domain 为空视为 CN（向后兼容）。"""
        d = (self.domain or "").strip().lower()
        if d == GLOBAL_SUFFIX.lstrip(".") or d.endswith(GLOBAL_SUFFIX):
            return "global"
        return "cn"

    # ---- refresh 判定 ----
    def needs_refresh(self, within) -> bool:
        """token 是否将在 within 内过期（或已过期/无 expiry）。

        within 兼容三种写法，避免调用方类型不一致：
          - timedelta（推荐，如 timedelta(hours=2)）
          - int/float 秒数（如 2 * 3600，对应 Go 原版 2 * time.Hour）
          - None / 非法值 → 视为 0，退化为「已过期才刷新」
        """
        if self.expires_at <= 0:
            return True
        if isinstance(within, timedelta):
            secs = within.total_seconds()
        elif isinstance(within, (int, float)):
            secs = float(within)
        else:
            secs = 0.0
        return int(time.time()) + int(secs) >= self.expires_at

    # ---- 解析 ----
    @staticmethod
    def parse(raw) -> "Auth":
        """兼容两种磁盘形态：
        嵌套形 {"auth":{...},"account":{...}}（插件 OAuth 输出）
        扁平形 {"accessToken":...,"uid":...}（CPA 面板手建）
        """
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if not raw or not raw.strip():
            raise ValueError("empty auth storage")
        try:
            probe = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"storage_parse_error: {e}")
        if not isinstance(probe, dict):
            raise ValueError("storage_parse_error: top-level not object")

        a = Auth()
        if "auth" in probe:
            n = probe
            auth_obj = n.get("auth", {}) or {}
            acct_obj = n.get("account", {}) or {}
            a.access_token = auth_obj.get("accessToken", "")
            a.refresh_token = auth_obj.get("refreshToken", "")
            a.expires_at = int(auth_obj.get("expiresAt", 0) or 0)
            a.domain = auth_obj.get("domain", "")
            a.uid = acct_obj.get("uid", "")
            a.enterprise_id = acct_obj.get("enterpriseId", "")
            a.nickname = acct_obj.get("nickname", "")
        else:
            f = probe
            a.access_token = f.get("accessToken", "")
            a.refresh_token = f.get("refreshToken", "")
            a.expires_at = int(f.get("expiresAt", 0) or 0)
            a.domain = f.get("domain", "")
            a.uid = f.get("uid", "")
            a.enterprise_id = f.get("enterpriseId", "")
            a.nickname = f.get("nickname", "")

        if not (a.access_token or "").strip():
            raise ValueError("parse_error: missing accessToken")
        return a

    # ---- 原子写回 ----
    def save_atomic(self) -> None:
        """以嵌套形原子写回 file_path（tmp + rename），保持 CPA 插件可读格式。

        全程持 self.mu：防止与 RefreshToken 修改 token 字段并发，杜绝写回半更新。
        防御：access_token 为空时拒绝写回。
        """
        with self.mu:
            if not (self.access_token or "").strip():
                raise ValueError(f"save refused: empty accessToken (uid={self.uid})")
            if not self.file_path:
                raise ValueError("no file_path set")
            doc = {
                "auth": {
                    "accessToken": self.access_token,
                    "refreshToken": self.refresh_token,
                    "expiresAt": self.expires_at,
                    "domain": self.domain,
                },
                "account": {
                    "uid": self.uid,
                    "enterpriseId": self.enterprise_id,
                    "nickname": self.nickname,
                },
            }
            raw = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")
            tmp = self.file_path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, self.file_path)

    # ---- 批量扫描 ----
    @staticmethod
    def load_dir(directory: str, want_region: str) -> List["Auth"]:
        """扫描 dir 下 workbuddy*.json，只收 want_region（"cn"/"global"）。

        解析失败与 region 不符的文件静默跳过（启动日志由调用方统计）。
        """
        out: List[Auth] = []
        for fp in sorted(Path(directory).glob("workbuddy*.json")):
            try:
                raw = fp.read_bytes()
            except OSError:
                continue
            try:
                a = Auth.parse(raw)
            except ValueError:
                continue
            if a.region() != want_region:
                continue
            a.file_path = str(fp)
            out.append(a)
        return out
