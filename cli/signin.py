"""cli/signin.py — 一次性批量签到工具：遍历 auths 下全部账号，自动 Refresh，逐个 daily-checkin，顺手查余额。

等价于 Go 版 cmd/signin/main.go。
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from datetime import timedelta
from typing import List, Optional

import requests

# 将项目根目录加入 sys.path，保证 `py cli/signin.py` 直接运行时可导入 wb2api 包。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb2api.auth import Auth
from wb2api.upstream import Client, ErrKind

AUTH_DIR = "auths"


def _is_already(msg: str) -> bool:
    s = (msg or "").lower()
    return "已签到" in s or "already" in s or "checkin" in s or "code=400" in s


def _trunc(s: str, n: int) -> str:
    return s[:n] if len(s) > n else s


def _short(s: str) -> str:
    s = s.replace("\n", " ")
    return s[:60] if len(s) > 60 else s


def main() -> int:
    directory = sys.argv[1] if len(sys.argv) > 1 else AUTH_DIR
    files = sorted(glob.glob(os.path.join(directory, "workbuddy-*.json")))
    if not files:
        print(f"no auth files in {directory}", file=sys.stderr)
        sys.exit(1)

    up = Client()

    rows: List[dict] = []
    ok_n = already_n = fail_n = 0
    for f in files:
        r = {"file": os.path.basename(f)}
        try:
            with open(f, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as e:
            r["status"] = "LOAD_ERR"
            r["detail"] = str(e)
            rows.append(r)
            fail_n += 1
            continue
        try:
            a = Auth.parse(raw)
        except Exception as e:  # noqa
            r["status"] = "LOAD_ERR"
            r["detail"] = str(e)
            rows.append(r)
            fail_n += 1
            continue
        a.file_path = f
        r["uid"] = a.uid
        r["nick"] = a.nickname

        # refresh 过期 token
        if a.needs_refresh(timedelta(hours=2)):
            err = up.refresh_token(a)
            if err is not None:
                if getattr(err, "kind", None) == ErrKind.SESSION_DEAD:
                    r["status"] = "AUTH_INVALID"
                else:
                    r["status"] = "FAIL"
                r["detail"] = "refresh: " + _short(str(err))
                rows.append(r)
                fail_n += 1
                continue
            try:
                a.save_atomic()
            except Exception as e:  # noqa
                print(f"signin {a.uid} save: {e}", file=sys.stderr)

        err = up.daily_checkin(a)
        if err is None:
            r["status"] = "OK"
            ok_n += 1
        else:
            msg = str(err)
            if _is_already(msg):
                r["status"] = "ALREADY"
                r["detail"] = _short(msg)
                already_n += 1
            else:
                r["status"] = "FAIL"
                r["detail"] = _short(msg)
                fail_n += 1

        remain, qerr = up.user_resource(a)
        if qerr is None:
            r["remain"] = remain
            r["has_quota"] = True

        rows.append(r)

    # 报告
    print("uid                                  | nick        | status       | remain | detail")
    print("-------------------------------------+-------------+--------------+--------+------------------------------")
    for r in rows:
        remain = "-"
        if r.get("has_quota"):
            remain = str(r.get("remain", ""))
        print(f"{_trunc(r.get('uid',''),36):<36} | {_trunc(r.get('nick',''),11):<11} | "
              f"{r.get('status',''):<12} | {remain:<6} | {r.get('detail','')}")
    print(f"\ntotal={len(rows)} ok={ok_n} already={already_n} fail={fail_n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
