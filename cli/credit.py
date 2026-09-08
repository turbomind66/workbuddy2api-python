"""cli/credit.py — WorkBuddy 积分查询（全部账号 + 总计），JSON 或美化输出到 stdout。

等价于 Go 版 cmd/credit/main.go。接口逻辑移植自 billing.go fetchUserResource。
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

BILLING_BASE_CN = "https://www.codebuddy.cn"
AUTH_DIR = os.environ.get("WB2A_AUTH_DIR", "./auths")


def _billing_headers(af: dict) -> dict:
    h = {
        "Authorization": "Bearer " + af.get("auth", {}).get("accessToken", "") if "auth" in af else "Bearer " + af.get("accessToken", ""),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
    }
    uid = af.get("account", {}).get("uid") if "account" in af else af.get("uid")
    eid = af.get("account", {}).get("enterpriseId") if "account" in af else af.get("enterpriseId")
    domain = af.get("auth", {}).get("domain") if "auth" in af else af.get("domain")
    if uid:
        h["X-User-Id"] = uid
    if eid:
        h["X-Enterprise-Id"] = eid
        h["X-Tenant-Id"] = eid
    if domain:
        h["X-Domain"] = domain
    return h


def package_remain_used(a: dict):
    if int(a.get("CycleCapacitySize", 0) or 0) > 0:
        remain = int(a.get("CycleCapacityRemain", 0) or 0)
        size = int(a.get("CycleCapacitySize", 0) or 0)
        if remain < 0:
            remain = 0
        if remain > size:
            remain = size
        used = size - remain
        if int(a.get("CycleCapacityUsed", 0) or 0) > used:
            used = int(a.get("CycleCapacityUsed", 0) or 0)
            if size >= used:
                remain = size - used
        return remain, used, size
    remain = int(a.get("CapacityRemain", 0) or 0)
    used = int(a.get("CapacityUsed", 0) or 0)
    size = int(a.get("CapacitySize", 0) or 0)
    if used == 0 and size > remain:
        used = size - remain
    return remain, used, size


def fetch_user_resource(af: dict) -> Tuple[int, int, int, int, Optional[str]]:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    future = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 365 * 101 * 24 * 3600))
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now,
        "PackageEndTimeRangeEnd": future,
    }
    try:
        resp = requests.post(
            BILLING_BASE_CN + "/v2/billing/meter/get-user-resource",
            headers=_billing_headers(af), json=body, timeout=20,
        )
    except requests.RequestException as e:
        return 0, 0, 0, 0, str(e)
    if resp.status_code >= 400:
        # 透传上游业务错误码与 msg（如 code=10085 请求不合法），便于排查。
        try:
            env = resp.json()
            return 0, 0, 0, 0, f"code={env.get('code')} {env.get('msg', '')}".strip()
        except (json.JSONDecodeError, ValueError):
            return 0, 0, 0, 0, f"http {resp.status_code}"
    try:
        env = resp.json()
    except (json.JSONDecodeError, ValueError):
        return 0, 0, 0, 0, "parse failed"
    if env.get("code", 0) != 0:
        return 0, 0, 0, 0, f"code={env.get('code')} {env.get('msg','')}"
    data = (env.get("data") or {}).get("Response", {}).get("Data", {})
    accounts = data.get("Accounts", []) or []
    remain = used = size = 0
    for a in accounts:
        r, u, s = package_remain_used(a)
        remain += r
        used += u
        size += s
    packs = len(accounts)
    if size > 0:
        if (size - remain) > used:
            used = size - remain
    total_dosage = int(data.get("TotalDosage", 0) or 0)
    if total_dosage > size:
        size = total_dosage
        if (size - remain) > used:
            used = size - remain
    return remain, used, size, packs, None


def fetch_checkin_status(af: dict):
    """查询签到活动状态，获取个人免费账号的积分（total_credits 等）。

    get-user-resource 只查「企业套餐」余额，个人免费账号（enterpriseId 为空）
    无套餐，会返回 code=10085；个人账号的积分来自签到活动，须用本接口查询。
    返回 (data_dict, err)。
    """
    try:
        resp = requests.post(
            "https://copilot.tencent.com/v2/billing/meter/checkin-activity-status",
            headers=_billing_headers(af), data="{}", timeout=20,
        )
    except requests.RequestException as e:
        return None, str(e)
    if resp.status_code >= 400:
        try:
            env = resp.json()
            return None, f"code={env.get('code')} {env.get('msg', '')}".strip()
        except (json.JSONDecodeError, ValueError):
            return None, f"http {resp.status_code}"
    try:
        env = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None, "parse failed"
    if env.get("code", 0) != 0:
        return None, f"code={env.get('code')} {env.get('msg','')}"
    return (env.get("data") or {}), None


def main() -> int:
    pretty = len(sys.argv) > 1 and sys.argv[1] in ("-pretty", "--pretty")
    files = sorted(glob.glob(os.path.join(AUTH_DIR, "workbuddy-*.json")))

    accounts: List[dict] = []
    for f in files:
        try:
            raw = open(f, "r", encoding="utf-8").read()
            af = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        res = {"uid": (af.get("account", {}).get("uid") if "account" in af else af.get("uid")),
               "nickname": (af.get("account", {}).get("nickname") if "account" in af else af.get("nickname"))}
        tok = af.get("auth", {}).get("accessToken") if "auth" in af else af.get("accessToken")
        if not tok:
            res["error"] = "no accessToken"
            accounts.append(res)
            continue
        remain, used, size, packs, err = fetch_user_resource(af)
        if err:
            # 只有上游明确返回「无套餐」(code=10085) 时才降级到签到积分。
            if "10085" in err:
                data, cerr = fetch_checkin_status(af)
                if cerr is None and data:
                    tc = int(data.get("total_credits", 0) or 0)
                    res["remain"] = tc
                    res["used"] = 0
                    res["size"] = tc
                    res["packages"] = 0
                    res["source"] = "checkin"
                    res["streak_days"] = data.get("streak_days")
                    res["daily_credit"] = data.get("daily_credit")
                    res["today_checked_in"] = data.get("today_checked_in")
                    res["activity_name"] = data.get("activity_name")
                    res["ok"] = True
                else:
                    res["error"] = err
                    if cerr:
                        res["checkin_error"] = cerr
            else:
                res["error"] = err
        else:
            res["remain"] = remain
            res["used"] = used
            res["size"] = size
            res["packages"] = packs
            res["ok"] = True
        accounts.append(res)
        time.sleep(0.2)

    total_remain = total_used = total_size = ok_count = 0
    for a in accounts:
        if a.get("ok"):
            ok_count += 1
            total_remain += int(a.get("remain", 0) or 0)
            total_used += int(a.get("used", 0) or 0)
            total_size += int(a.get("size", 0) or 0)

    if pretty:
        _print_pretty(accounts, total_remain, total_used, total_size, ok_count)
        return 0

    out = {
        "service": "workbuddy",
        "ts": int(time.time()),
        "total": {
            "remain": total_remain,
            "used": total_used,
            "size": total_size,
            "accounts": len(accounts),
            "ok": ok_count,
            "failed": len(accounts) - ok_count,
        },
        "accounts": accounts,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _print_pretty(accounts, total_remain, total_used, total_size, ok_count):
    with_balance = 0
    failed = []
    rows = []
    for a in accounts:
        if not a.get("ok"):
            name = a.get("nickname") or (a.get("uid") or "")[:8]
            failed.append(f"{name} {a.get('error','')}")
            continue
        remain = int(a.get("remain", 0) or 0)
        if remain > 0:
            with_balance += 1
        name = a.get("nickname") or (a.get("uid") or "")[:8]
        if a.get("source") == "checkin":
            streak = a.get("streak_days", 0)
            daily = a.get("daily_credit", 0)
            act = a.get("activity_name") or ""
            extra = f" | {act}" if act else ""
            rows.append(f"  · {name}: 签到积分 {remain} | 连续 {streak} 天 | 每日 +{daily}{extra}")
        else:
            rows.append(
                f"  · {name}: 剩余 {remain}/{a.get('size', 0)} "
                f"(已用 {a.get('used', 0)}, 套餐 {a.get('packages', 0)})"
            )
    pct = int(total_remain * 100 / total_size) if total_size > 0 else 0
    print("📊 WorkBuddy 积分日报")
    print(f"账号: {with_balance}/{len(accounts)}")
    if total_size > 0:
        print(f"总计: {total_remain}/{total_size} ({pct}%)")
    else:
        print(f"总计积分: {total_remain}")
    for r in rows:
        print(r)
    for f in failed:
        print(f"⚠️ {f}")


if __name__ == "__main__":
    sys.exit(main())
