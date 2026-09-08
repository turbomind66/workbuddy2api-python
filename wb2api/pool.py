"""pool.py — 账号池：单一状态机（健康/冷却/熔断）+ 在途租约 + 三因子加权挑选 + state.json 持久化。

每个账号只有三个正交状态维度：
1. 健康维度（唯一权威）：healthy = !disabled && !until 生效 && !breakerUntil 生效
2. 并发维度：inFlight（在途租约，运行态）
3. 统计维度：successCount / errTotal / lastUsed / lastSuccess / lastErr

等价于 Go 版 internal/pool/pool.go。
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from wb2api.auth import Auth

LOG = logging.getLogger("wb2api.pool")

# CoolKind
COOL_HARD = 0  # 余额不足 → 冷却到次日 04:00
COOL_SOFT = 1  # 429 → 短冷却

COOL_KIND_NAME = {COOL_HARD: "hard_credit", COOL_SOFT: "soft_rate"}

# 默认熔断器参数
DEFAULT_BREAKER_THRESHOLD = 3
DEFAULT_BREAKER_COOLDOWN = timedelta(minutes=30)
DEFAULT_BREAKER_COOLDOWN_MAX = timedelta(hours=6)

# 默认闲置补偿参数
DEFAULT_IDLE_WEIGHT_PER_HOUR = 0.5
DEFAULT_IDLE_WEIGHT_MAX = 5.0

FLUSH_INTERVAL = timedelta(seconds=5)
PERSIST_LOG_EVERY = 12

# 防并发撞号窗口（100ms）
MIN_PICK_GAP = timedelta(milliseconds=100)


def _dt_to_json(dt: datetime) -> Optional[str]:
    return dt.isoformat() if dt and not _is_zero(dt) else None


def _is_zero(dt: datetime) -> bool:
    return dt is None or dt.year == 1


def _json_to_dt(s) -> datetime:
    if not s:
        return datetime.min
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s)
    if isinstance(s, str):
        s = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return datetime.min
    return datetime.min


class _Entry:
    def __init__(self, a: Auth) -> None:
        self.a = a
        self.credits = 0
        self.success_count = 0
        self.err_total = 0
        self.last_err = datetime.min
        self.last_success = datetime.min
        self.cool_kind = COOL_HARD
        self.until = datetime.min
        self.disabled = False
        self.reason = ""
        self.last_used = datetime.min

        # 熔断器运行态
        self.breaker_until = datetime.min
        self.fails = 0
        self.retry_count = 0

        # 在途租约
        self.in_flight = 0

    def healthy(self, now: datetime) -> bool:
        if self.disabled:
            return False
        if not _is_zero(self.until) and now < self.until:
            return False
        if not _is_zero(self.breaker_until) and now < self.breaker_until:
            return False
        return True

    def expiry(self, now: datetime) -> datetime:
        """返回当前仍在生效的最近冷却/熔断截止时间；不在冷却期返回 zero。"""
        t = datetime.min
        if not _is_zero(self.until) and now < self.until:
            t = self.until
        if not _is_zero(self.breaker_until) and now < self.breaker_until:
            if _is_zero(t) or self.breaker_until < t:
                t = self.breaker_until
        return t

    def fallback_kind(self, now: datetime) -> str:
        if not _is_zero(self.breaker_until) and now < self.breaker_until:
            if _is_zero(self.until) or not (now < self.until) or self.breaker_until < self.until:
                return "breaker"
        return "soft"


class StoreSnapshotter:
    """池状态快照镜像最小接口（redisstore.Store 满足；Noop 空实现安全）。"""
    def save_state(self, data: bytes) -> None: ...
    def load_state(self) -> (Optional[bytes], bool): ...


class Pool:
    def __init__(self, state_fp: str) -> None:
        self._mu = threading.RLock()
        self.by_uid: Dict[str, _Entry] = {}
        self.state_fp = state_fp
        self._dirty = False

        self.store: Optional[StoreSnapshotter] = None

        self.breaker_threshold = DEFAULT_BREAKER_THRESHOLD
        self.breaker_cooldown = DEFAULT_BREAKER_COOLDOWN
        self.breaker_cooldown_max = DEFAULT_BREAKER_COOLDOWN_MAX

        self.idle_weight_per_hour = DEFAULT_IDLE_WEIGHT_PER_HOUR
        self.idle_weight_max = DEFAULT_IDLE_WEIGHT_MAX

        self.max_in_flight = 0

        self._rng = random.Random()
        self._rand_int64 = None  # 测试注入

        self._persist_fails = 0

        if state_fp:
            self._load()
            self._start_flusher()

    # ---- 熔断器 / 权重 注入 ----
    def set_breaker(self, threshold: int, cooldown: timedelta, cooldown_max: timedelta) -> None:
        with self._mu:
            if threshold > 0:
                self.breaker_threshold = threshold
            if cooldown and cooldown > timedelta(0):
                self.breaker_cooldown = cooldown
            if cooldown_max and cooldown_max > timedelta(0):
                self.breaker_cooldown_max = cooldown_max

    def set_weights(self, idle_per_hour: float, idle_max: float) -> None:
        with self._mu:
            if idle_per_hour > 0:
                self.idle_weight_per_hour = idle_per_hour
            if idle_max > 0:
                self.idle_weight_max = idle_max

    def set_max_in_flight(self, n: int) -> None:
        with self._mu:
            if n >= 0:
                self.max_in_flight = n

    def set_store(self, s: Optional[StoreSnapshotter]) -> None:
        with self._mu:
            self.store = s

    def set_random_source(self, fn) -> None:
        with self._mu:
            self._rand_int64 = fn

    # ---- 快照恢复 ----
    def restore_from_snapshot(self) -> None:
        store = self.store
        if store is None or not self.state_fp:
            return
        local_info = None
        local_err = None
        try:
            local_info = os.stat(self.state_fp)
        except OSError as e:
            local_err = e
        raw, ok = store.load_state()
        if not ok:
            if local_err is None:
                LOG.info("pool: 恢复来源=本地 state.json（无 Redis 快照）")
            return
        snap = json.loads(raw) if raw else {}
        saved_at = _json_to_dt(snap.get("saved_at"))
        if _is_zero(saved_at):
            LOG.info("pool: 恢复来源=本地 state.json（Redis 快照无 saved_at）")
            return
        local_newer = (local_info is not None) and (local_info.st_mtime > saved_at.timestamp())
        if local_info is not None and not local_newer:
            accounts = {k: _state_account_from_dict(v) for k, v in (snap.get("accounts") or {}).items()}
            with self._mu:
                self._apply_accounts_locked(accounts)
                self._dirty = True
            LOG.info("pool: 恢复来源=Redis 快照 (saved_at=%s)", saved_at.isoformat())
            return
        LOG.info("pool: 恢复来源=本地 state.json（较新于 Redis 快照 %s）", saved_at.isoformat())

    # ---- 在途租约 ----
    def acquire(self, uid: str) -> bool:
        with self._mu:
            e = self.by_uid.get(uid)
            if e is None:
                return False
            limit = self.max_in_flight
            if limit <= 0:
                e.in_flight += 1
                return True
            if e.in_flight >= limit:
                return False
            e.in_flight += 1
            return True

    def release(self, uid: str) -> None:
        with self._mu:
            e = self.by_uid.get(uid)
            if e is None:
                return
            if e.in_flight <= 0:
                return
            e.in_flight -= 1

    def _in_flight_full(self, e: _Entry) -> bool:
        if self.max_in_flight <= 0:
            return False
        return e.in_flight >= self.max_in_flight

    # ---- 后台落盘 ----
    def _start_flusher(self) -> None:
        def run():
            while True:
                time.sleep(FLUSH_INTERVAL.total_seconds())
                with self._mu:
                    if self._dirty:
                        self._dirty = False
                        self._save_locked()
        t = threading.Thread(target=run, daemon=True)
        t.start()

    def flush(self) -> None:
        with self._mu:
            if self._dirty:
                self._dirty = False
                self._save_locked()

    # ---- 增删 ----
    def add(self, a: Auth) -> None:
        with self._mu:
            self._upsert_locked(a)

    def sync_to_dir(self, auths: List[Auth]) -> None:
        with self._mu:
            seen = {a.uid: True for a in auths}
            for a in auths:
                self._upsert_locked(a)
            changed = False
            for uid in list(self.by_uid.keys()):
                if uid not in seen:
                    del self.by_uid[uid]
                    changed = True
            if changed:
                self._save_locked()

    def _upsert_locked(self, a: Auth) -> None:
        e = self.by_uid.get(a.uid)
        if e is not None:
            e.a = a  # 保留 credits/cooling 状态
            return
        self.by_uid[a.uid] = _Entry(a)

    # ---- 挑选 ----
    def pick(self) -> Optional[Auth]:
        return self.pick_excluding(None)

    def pick_excluding(self, tried: Optional[Dict[str, bool]]) -> Optional[Auth]:
        return self._pick(tried)

    def _pick(self, tried: Optional[Dict[str, bool]]) -> Optional[Auth]:
        with self._mu:
            now = datetime.now()
            cands: List[_Entry] = []
            for uid, e in self.by_uid.items():
                if tried is not None and tried.get(uid):
                    continue
                if not e.healthy(now):
                    continue
                if self._in_flight_full(e):
                    continue
                cands.append(e)
            if not cands:
                return self._pick_earliest_expiry_locked(tried, now)
            max_credits = 0
            for e in cands:
                if e.credits > max_credits:
                    max_credits = e.credits

            weighted = [(e, self._weight_of(e, max_credits, now)) for e in cands]
            weighted.sort(key=lambda x: (-x[1], x[0].a.uid))
            cands = [w[0] for w in weighted]
            if len(cands) > 5:
                cands = cands[:5]

            eligible: List[_Entry] = []
            for e in cands:
                if (now - e.last_used).total_seconds() >= MIN_PICK_GAP.total_seconds():
                    eligible.append(e)
            if not eligible:
                e = cands[0]
                for c in cands[1:]:
                    if c.last_used < e.last_used:
                        e = c
            else:
                e = self._pick_weighted(eligible)
            e.last_used = datetime.now()
            return e.a

    def _pick_earliest_expiry_locked(self, tried: Optional[Dict[str, bool]], now: datetime) -> Optional[Auth]:
        best: Optional[_Entry] = None
        for uid, e in self.by_uid.items():
            if tried is not None and tried.get(uid):
                continue
            if e.disabled:
                continue
            if e.cool_kind == COOL_HARD and not _is_zero(e.until) and now < e.until:
                continue
            if self._in_flight_full(e):
                continue
            exp = e.expiry(now)
            if _is_zero(exp):
                continue
            if best is None or exp < best.expiry(now):
                best = e
        if best is None:
            return None
        LOG.info("pool: fallback_earliest_expiry uid=%s until=%s kind=%s",
                 best.a.uid, _dt_to_json(best.expiry(now)), best.fallback_kind(now))
        best.last_used = datetime.now()
        return best.a

    def _pick_weighted(self, cands: List[_Entry]) -> _Entry:
        now = datetime.now()
        max_credits = 0
        for e in cands:
            if e.credits > max_credits:
                max_credits = e.credits
        SCALE = 1_000_000
        weights = [int(self._weight_of(e, max_credits, now) * SCALE) for e in cands]
        total = sum(weights)
        rnd = self._rand_int64 if self._rand_int64 else self._rng.randrange
        if total <= 0:
            return cands[rnd(len(cands))]
        r = rnd(total)
        acc = 0
        for i, e in enumerate(cands):
            acc += weights[i]
            if r < acc:
                return e
        return cands[-1]

    def _weight_of(self, e: _Entry, max_credits: int, now: datetime) -> float:
        w = 1.0
        if max_credits > 0:
            w += (float(e.credits) / float(max_credits)) * 10
        if _is_zero(e.last_used):
            w += self.idle_weight_max
        else:
            hours = (now - e.last_used).total_seconds() / 3600.0
            idle_w = hours * self.idle_weight_per_hour
            if idle_w > self.idle_weight_max:
                idle_w = self.idle_weight_max
            if idle_w < 0:
                idle_w = 0
            w += idle_w
        total_req = e.success_count + e.err_total
        if total_req > 0:
            w += float(e.success_count) / float(total_req) * 3
        else:
            w += 1.5
        return w

    # ---- 状态变更 ----
    def set_credits(self, uid: str, credits: int) -> None:
        with self._mu:
            e = self.by_uid.get(uid)
            if e is not None:
                e.credits = credits
                self._dirty = True

    def cooldown(self, uid: str, kind: int, d: timedelta, reason: str) -> None:
        with self._mu:
            e = self.by_uid.get(uid)
            if e is not None:
                e.until = datetime.now() + d
                e.cool_kind = kind
                e.reason = reason
                self._record_breaker_failure_locked(e)
                self._dirty = True

    def _record_breaker_failure_locked(self, e: _Entry) -> None:
        e.fails += 1
        if e.fails < self.breaker_threshold:
            return
        d = self.breaker_cooldown
        i = 0
        while i < e.retry_count:
            d = d * 2
            if d >= self.breaker_cooldown_max:
                d = self.breaker_cooldown_max
                break
            i += 1
        e.fails = 0
        e.retry_count += 1
        e.breaker_until = datetime.now() + d

    def cooldown_until_tomorrow_4am(self, uid: str, reason: str) -> None:
        now = datetime.now()
        self.cooldown(uid, COOL_HARD, _next_day_4am(now) - now, reason)

    def disable(self, uid: str, reason: str) -> None:
        with self._mu:
            e = self.by_uid.get(uid)
            if e is not None:
                e.disabled = True
                e.reason = reason
                self._dirty = True

    def _revive_cooling_locked(self, e: _Entry, credits: int) -> None:
        e.credits = credits
        e.until = datetime.min
        e.cool_kind = COOL_HARD
        e.reason = ""

    def reenable_if_credits(self, uid: str, remain: int) -> None:
        with self._mu:
            e = self.by_uid.get(uid)
            if e is not None:
                if remain > 0 and not e.disabled:
                    self._revive_cooling_locked(e, remain)
                else:
                    e.credits = remain
                self._dirty = True

    def note_error(self, uid: str) -> None:
        with self._mu:
            e = self.by_uid.get(uid)
            if e is not None:
                e.err_total += 1
                e.last_err = datetime.now()
                self._record_breaker_failure_locked(e)
                self._dirty = True

    def note_success(self, uid: str) -> None:
        with self._mu:
            e = self.by_uid.get(uid)
            if e is not None:
                e.success_count += 1
                e.last_success = datetime.now()
                e.fails = 0
                e.retry_count = 0
                e.breaker_until = datetime.min
                self._dirty = True

    # ---- 查询 ----
    def status(self, uid: str):
        with self._mu:
            e = self.by_uid.get(uid)
            if e is None:
                return None, False
            return self._status_of(uid, e), True

    def auth_by_uid(self, uid: str) -> Optional[Auth]:
        with self._mu:
            e = self.by_uid.get(uid)
            return e.a if e else None

    def available_uids(self) -> List[str]:
        with self._mu:
            now = datetime.now()
            uids = [uid for uid, e in self.by_uid.items()
                    if e.healthy(now) and not self._in_flight_full(e)]
            uids.sort()
            return uids

    def pick_by_uid(self, uid: str) -> Optional[Auth]:
        with self._mu:
            e = self.by_uid.get(uid)
            if e is None:
                return None
            now = datetime.now()
            if not e.healthy(now):
                return None
            if self._in_flight_full(e):
                return None
            e.last_used = now
            return e.a

    def counts_detailed(self):
        with self._mu:
            now = datetime.now()
            total = healthy = cooling = disabled = in_flight_full = 0
            for e in self.by_uid.values():
                total += 1
                if e.disabled:
                    disabled += 1
                elif not e.healthy(now):
                    cooling += 1
                else:
                    healthy += 1
                    if self._in_flight_full(e):
                        in_flight_full += 1
            return total, healthy, cooling, disabled, in_flight_full

    def servable_now(self) -> bool:
        with self._mu:
            now = datetime.now()
            for e in self.by_uid.values():
                if e.healthy(now) and not self._in_flight_full(e):
                    return True
            return False

    def list(self) -> List[Dict[str, Any]]:
        with self._mu:
            uids = sorted(self.by_uid.keys())
            return [self._status_of(uid, self.by_uid[uid]) for uid in uids]

    def _status_of(self, uid: str, e: _Entry) -> Dict[str, Any]:
        now = datetime.now()
        cooling = (not _is_zero(e.until) and now < e.until) or (not _is_zero(e.breaker_until) and now < e.breaker_until)
        st: Dict[str, Any] = {
            "uid": uid,
            "nickname": e.a.nickname,
            "credits": e.credits,
            "cooling": cooling,
            "disabled": e.disabled,
            "success_count": e.success_count,
            "err_total": e.err_total,
            "last_success": _dt_to_json(e.last_success),
            "last_err": _dt_to_json(e.last_err),
            "in_flight": e.in_flight,
            "breaker_fails": e.fails,
            "breaker_until": _dt_to_json(e.breaker_until),
        }
        if cooling:
            cool_remaining = int((e.until - now).total_seconds() + 0.999)
            if cool_remaining < 0:
                cool_remaining = 0
            st["cool_remaining_sec"] = cool_remaining
            st["cool_kind"] = COOL_KIND_NAME.get(e.cool_kind, "unknown")
            st["until"] = _dt_to_json(e.until)
            st["reason"] = e.reason
        return st

    # ---- 持久化 ----
    def _load(self) -> None:
        try:
            with open(self.state_fp, "r", encoding="utf-8") as f:
                raw = f.read()
        except (OSError, FileNotFoundError):
            return
        try:
            sf = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        accounts = {k: _state_account_from_dict(v) for k, v in (sf.get("accounts") or {}).items()}
        self._apply_accounts_locked(accounts)

    def _apply_accounts_locked(self, accounts: Dict[str, "_StateAccount"]) -> None:
        for uid, s in accounts.items():
            err_total = s.err_total
            if s.err_count > err_total:
                err_total = s.err_count
            self.by_uid[uid] = _Entry(Auth())
            self.by_uid[uid].a.uid = uid
            self.by_uid[uid].credits = s.credits
            self.by_uid[uid].disabled = s.disabled
            self.by_uid[uid].reason = s.reason
            self.by_uid[uid].until = s.until
            self.by_uid[uid].cool_kind = s.cool_kind
            self.by_uid[uid].success_count = s.success_count
            self.by_uid[uid].err_total = err_total
            self.by_uid[uid].last_err = s.last_err
            self.by_uid[uid].last_success = s.last_success

    def _apply_snapshot_locked(self, snap: Dict[str, Any]) -> None:
        self.by_uid = {}
        accounts = {k: _state_account_from_dict(v) for k, v in (snap.get("accounts") or {}).items()}
        self._apply_accounts_locked(accounts)

    def _save_locked(self) -> None:
        if not self.state_fp:
            return
        sf = self._state_overview_locked()
        try:
            raw = json.dumps(sf, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            self._note_persist_fail(Exception("marshal"))
            return
        d = os.path.dirname(self.state_fp)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.state_fp + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(raw)
            os.replace(tmp, self.state_fp)
        except OSError as e:
            self._note_persist_fail(e)
            return
        if self._persist_fails > 0:
            LOG.info("pool: state.json 落盘恢复（此前连续失败 %d 次）", self._persist_fails)
            self._persist_fails = 0
        if self.store is not None:
            try:
                snap = dict(sf)
                snap["saved_at"] = datetime.now().isoformat()
                self.store.save_state(json.dumps(snap, ensure_ascii=False).encode("utf-8"))
            except Exception:
                pass

    def _note_persist_fail(self, err: Exception) -> None:
        if self._persist_fails == 0:
            LOG.error("pool: state.json 落盘失败: %s", err)
        elif self._persist_fails % PERSIST_LOG_EVERY == 0:
            LOG.error("pool: state.json 连续落盘失败 %d 次: %s", self._persist_fails, err)
        self._persist_fails += 1

    def _state_overview_locked(self) -> Dict[str, Any]:
        accounts: Dict[str, Any] = {}
        for uid, e in self.by_uid.items():
            accounts[uid] = {
                "credits": e.credits,
                "disabled": e.disabled,
                "reason": e.reason,
                "until": _dt_to_json(e.until),
                "cool_kind": e.cool_kind,
                "success_count": e.success_count,
                "err_total": e.err_total,
                "last_success": _dt_to_json(e.last_success),
                "last_err": _dt_to_json(e.last_err),
            }
        return {"accounts": accounts}


class _StateAccount:
    def __init__(self) -> None:
        self.credits = 0
        self.disabled = False
        self.reason = ""
        self.until = datetime.min
        self.cool_kind = COOL_HARD
        self.success_count = 0
        self.err_total = 0
        self.err_count = 0
        self.last_success = datetime.min
        self.last_err = datetime.min


def _state_account_from_dict(v: Any) -> _StateAccount:
    s = _StateAccount()
    if not isinstance(v, dict):
        return s
    s.credits = int(v.get("credits", 0) or 0)
    s.disabled = bool(v.get("disabled", False))
    s.reason = v.get("reason", "") or ""
    s.until = _json_to_dt(v.get("until"))
    s.cool_kind = int(v.get("cool_kind", COOL_HARD) or COOL_HARD)
    s.success_count = int(v.get("success_count", 0) or 0)
    s.err_total = int(v.get("err_total", 0) or 0)
    s.err_count = int(v.get("err_count", 0) or 0)
    s.last_success = _json_to_dt(v.get("last_success"))
    s.last_err = _json_to_dt(v.get("last_err"))
    return s


def _next_day_4am(now: datetime) -> datetime:
    base = now.replace(hour=4, minute=0, second=0, microsecond=0)
    return base + timedelta(days=1)
