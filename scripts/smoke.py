"""scripts/smoke.py — 无网络、无凭证的纯逻辑冒烟测试。

目标：把「Go→Python 移植中最易错的时间/类型边界」锁进 CI，防止回归。
不发起任何 HTTP 请求，不读取任何真实凭证，可在 CI 裸环境直接运行。

退出码：0 表示全部通过，非 0 表示有失败项。
"""
from __future__ import annotations

import os
import sys
import time
from datetime import timedelta

# 将项目根目录加入 sys.path，保证直接 `python scripts/smoke.py` 可导入 wb2api。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(name: str, cond: bool) -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}")
    if not cond:
        _failures.append(name)


def main() -> int:
    print("== 1. 导入所有模块 ==")
    import wb2api
    import wb2api.config
    import wb2api.auth
    import wb2api.pool
    import wb2api.session
    import wb2api.redisstore
    import wb2api.scheduler
    import wb2api.server
    import wb2api.upstream
    import wb2api.upstream.errors
    import wb2api.upstream.consts
    import wb2api.upstream.headers
    import wb2api.upstream.payload
    import wb2api.upstream.sanitize
    import wb2api.upstream.sse
    import wb2api.upstream.client
    print("  [PASS] import all modules")

    print("== 2. Auth.needs_refresh 入参矩阵（回归：signin 崩溃） ==")
    from wb2api.auth import Auth
    now = int(time.time())
    a = Auth()
    a.expires_at = now + 3 * 3600
    check("int 2*3600，3h 后才过期 -> False", a.needs_refresh(2 * 3600) is False)
    check("timedelta(hours=2)，3h 后才过期 -> False", a.needs_refresh(timedelta(hours=2)) is False)
    b = Auth()
    b.expires_at = now + 3600
    check("int 2*3600，1h 内到期 -> True", b.needs_refresh(2 * 3600) is True)
    c = Auth()
    c.expires_at = 0
    check("expires_at=0 -> True", c.needs_refresh(2 * 3600) is True)
    check("None 入参 -> False（退化为不刷新）", a.needs_refresh(None) is False)
    check("float 入参 7200.0 -> False", a.needs_refresh(7200.0) is False)

    print("== 3. parse_duration 三种入参 ==")
    from wb2api.config import parse_duration
    check("字符串 '60s'", parse_duration("60s") == timedelta(seconds=60))
    check("复合 '1h30m'", parse_duration("1h30m") == timedelta(seconds=5400))
    check("int 60 视为秒", parse_duration(60) == timedelta(seconds=60))
    check("float 1.5 视为秒", parse_duration(1.5) == timedelta(milliseconds=1500))
    check("timedelta 原样返回", parse_duration(timedelta(minutes=5)) == timedelta(minutes=5))
    try:
        parse_duration("abc")
        check("非法字符串 'abc' 抛 ValueError", False)
    except ValueError:
        check("非法字符串 'abc' 抛 ValueError", True)
    try:
        parse_duration(True)
        check("bool 抛 ValueError", False)
    except ValueError:
        check("bool 抛 ValueError", True)

    print("== 4. Auth.parse 双形态 + region 判定 ==")
    flat = '{"accessToken":"t","refreshToken":"r","expiresAt":0,"uid":"u1","domain":"example.workbuddy.ai"}'
    d = Auth.parse(flat)
    check("扁平形 uid", d.uid == "u1")
    check("global region 判定", d.region() == "global")
    nested = ('{"auth":{"accessToken":"t2","refreshToken":"r2","expiresAt":0,"domain":"example.workbuddy.ai"},'
              '"account":{"uid":"u2","enterpriseId":"e","nickname":"n"}}')
    d2 = Auth.parse(nested)
    check("嵌套形 uid", d2.uid == "u2")
    check("嵌套形 nickname", d2.nickname == "n")
    cn = Auth.parse('{"accessToken":"t","refreshToken":"r","expiresAt":0,"uid":"u3","domain":"example.workbuddy.cn"}')
    check("cn region 判定", cn.region() == "cn")
    try:
        Auth.parse('{"refreshToken":"r"}')  # 缺 accessToken
        check("缺 accessToken 抛 ValueError", False)
    except ValueError:
        check("缺 accessToken 抛 ValueError", True)

    print("== 5. Config 默认值加载 ==")
    from wb2api.config import Config
    cfg = Config.default()
    check("SoftRateDur 默认 60s", cfg.SoftRateDur == timedelta(seconds=60))
    check("BreakerCooldownDur 默认 30m", cfg.BreakerCooldownDur == timedelta(minutes=30))
    check("SessionTTL 默认 30m", cfg.SessionTTL == timedelta(minutes=30))

    if _failures:
        print(f"\nSMOKE_FAILED: {len(_failures)} 项未通过 -> {_failures}")
        return 1
    print("\nSMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
