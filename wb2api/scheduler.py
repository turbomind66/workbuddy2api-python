"""scheduler.py — 定时任务：每日签到（09/21 点）+ token keepalive（22 点）。

签到成功后重新查余额，余额 > 0 的冷却账号自动解冻。

等价于 Go 版 internal/scheduler/scheduler.go。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import List, Optional

from wb2api.pool import Pool
from wb2api.upstream import Client, ErrKind

LOG = logging.getLogger("wb2api.scheduler")


class Config:
    def __init__(self, pool=None, upstream=None,
                 checkin_hours: Optional[List[int]] = None,
                 keepalive_hours: Optional[List[int]] = None) -> None:
        self.pool: Optional[Pool] = pool
        self.upstream: Optional[Client] = upstream
        self.checkin_hours: List[int] = list(checkin_hours) if checkin_hours else [9, 21]
        self.keepalive_hours: List[int] = list(keepalive_hours) if keepalive_hours else [22]


class Scheduler:
    def __init__(self, cfg: Config) -> None:
        if not cfg.checkin_hours:
            cfg.checkin_hours = [9, 21]
        if not cfg.keepalive_hours:
            cfg.keepalive_hours = [22]
        self.cfg = cfg

    def next_fire(self, now: datetime, hours: List[int]) -> datetime:
        earliest = None
        for h in hours:
            t = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if t <= now:
                t = t + timedelta(days=1)
            if earliest is None or t < earliest:
                earliest = t
        return earliest

    def run(self, stop_event: threading.Event) -> None:
        all_hours = self.cfg.checkin_hours + self.cfg.keepalive_hours
        while not stop_event.is_set():
            nxt = self.next_fire(datetime.now(), all_hours)
            delay = (nxt - datetime.now()).total_seconds()
            if delay <= 0:
                delay = 1
            if stop_event.wait(delay):
                return
            h = datetime.now().hour
            if h in self.cfg.checkin_hours:
                self.run_checkin_now()
            if h in self.cfg.keepalive_hours:
                self.run_keepalive_now()

    def run_checkin_now(self) -> None:
        pool = self.cfg.pool
        up = self.cfg.upstream
        for st in pool.list():
            if st.get("disabled"):
                continue
            a = pool.auth_by_uid(st["uid"])
            if a is None or not a.refresh_token:
                continue
            err = up.daily_checkin(a)
            if err is not None:
                LOG.warning("checkin %s: %s", st["uid"], err)
                # 已签到等业务错误也继续走余额查询
            remain, err = up.user_resource(a)
            if err is not None:
                LOG.warning("user-resource %s: %s", st["uid"], err)
                continue
            pool.reenable_if_credits(st["uid"], remain)

    def run_keepalive_now(self) -> None:
        pool = self.cfg.pool
        up = self.cfg.upstream
        for st in pool.list():
            if st.get("disabled"):
                continue
            a = pool.auth_by_uid(st["uid"])
            if a is None or not a.refresh_token:
                continue
            err = up.refresh_token(a)
            if err is not None:
                LOG.warning("keepalive %s: %s", st["uid"], err)
                if getattr(err, "kind", None) == ErrKind.SESSION_DEAD:
                    pool.disable(st["uid"], "12153 session dead")
                continue
            try:
                a.save_atomic()
            except Exception as e:  # noqa
                LOG.warning("keepalive %s save: %s", st["uid"], e)
