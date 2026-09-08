"""session.py — 会话粘性路由：同一会话（conversationId / metadata 键）尽量绑定同一账号。

纯内存 + redisstore 异步镜像：命中走 RLock 快查；未命中/失效走写锁 re-check 后分配；
分配优先"空闲账号"哈希，其次全池哈希（双段策略）；LastActive 滚动续期，TTL 过期由 GC 清理；
每次绑定变更 fire-and-forget 镜像到 redisstore（防重启丢粘性）。

等价于 Go 版 internal/session/session.go。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from wb2api.redisstore import Noop, Store

LOG = logging.getLogger("wb2api.session")

DEFAULT_TTL = timedelta(minutes=30)
DEFAULT_GC_INTERVAL = timedelta(minutes=5)


class _Entry:
    def __init__(self, uid: str, last_active: datetime) -> None:
        self.uid = uid
        self.last_active = last_active


class Config:
    def __init__(self, ttl: Optional[timedelta] = None,
                 gc_interval: Optional[timedelta] = None,
                 store: Optional[Store] = None,
                 available=None) -> None:
        self.ttl = ttl if ttl is not None else DEFAULT_TTL
        self.gc_interval = gc_interval if gc_interval is not None else DEFAULT_GC_INTERVAL
        self.store: Store = store if store is not None else Noop()
        self.available = available  # () -> List[str]


class Router:
    def __init__(self, cfg: Config) -> None:
        if cfg.store is None:
            cfg.store = Noop()
        if cfg.ttl <= timedelta(0):
            cfg.ttl = DEFAULT_TTL
        if cfg.gc_interval <= timedelta(0):
            cfg.gc_interval = DEFAULT_GC_INTERVAL
        self.cfg = cfg
        self._mu = threading.RLock()
        self.entries: Dict[str, _Entry] = {}
        self._stop = None

    # ---- GC ----
    def start_gc(self) -> None:
        with self._mu:
            if self._stop is not None:
                return
            self._stop = threading.Event()
        stop = self._stop

        def run():
            while not stop.wait(self.cfg.gc_interval.total_seconds()):
                self.gc_once(datetime.now())

        threading.Thread(target=run, daemon=True).start()

    def stop_gc(self) -> None:
        with self._mu:
            if self._stop is not None:
                self._stop.set()
                self._stop = None

    def load_from_store(self) -> None:
        binds = self.cfg.store.load_binds()
        if not binds:
            return
        now = datetime.now()
        with self._mu:
            loaded = 0
            for key, uid in binds.items():
                if key in self.entries:
                    continue
                self.entries[key] = _Entry(uid=uid, last_active=now)
                loaded += 1
        if loaded > 0:
            LOG.info("[session] 从 Redis 恢复 %d 条粘性会话绑定", loaded)

    # ---- 解析 / 绑定 ----
    def resolve(self, key: str):
        now = datetime.now()
        available = self._available_set()
        with self._mu:
            e = self.entries.get(key)
        if e is not None and not _expired(e, now, self.cfg.ttl):
            if available.get(e.uid):
                self._touch(key, e.uid, now)
                return e.uid, True
        with self._mu:
            e2 = self.entries.get(key)
            if e2 is not None and not _expired(e2, now, self.cfg.ttl):
                if available.get(e2.uid):
                    self.entries[key] = _Entry(uid=e2.uid, last_active=now)
                    return e2.uid, True
                del self.entries[key]
            uids = self._available_slice()
            if not uids:
                return "", False
            bound = {v.uid for v in self.entries.values()}
            idle = [u for u in uids if u not in bound]
            pool2 = idle if idle else uids
            uid = pool2[hash_index(key, len(pool2))]
            prev = self.entries.get(key)
            self.entries[key] = _Entry(uid=uid, last_active=now)
            if prev is not None and prev.uid != uid:
                self.cfg.store.del_bind(key)
            self.cfg.store.set_bind(key, uid, self.cfg.ttl)
            return uid, True

    def _touch(self, key: str, uid: str, now: datetime) -> None:
        with self._mu:
            self.entries[key] = _Entry(uid=uid, last_active=now)
        self.cfg.store.set_bind(key, uid, self.cfg.ttl)

    def bind(self, key: str, uid: str) -> None:
        if not key or not uid:
            return
        now = datetime.now()
        with self._mu:
            self.entries[key] = _Entry(uid=uid, last_active=now)
        self.cfg.store.set_bind(key, uid, self.cfg.ttl)

    def unbind(self, key: str) -> bool:
        with self._mu:
            found = key in self.entries
            if found:
                del self.entries[key]
        if found:
            self.cfg.store.del_bind(key)
        return found

    def count(self) -> int:
        with self._mu:
            return len(self.entries)

    def gc_once(self, now: datetime) -> int:
        with self._mu:
            expired_keys = [k for k, e in self.entries.items() if (now - e.last_active) > self.cfg.ttl]
            for k in expired_keys:
                del self.entries[k]
        for k in expired_keys:
            self.cfg.store.del_bind(k)
        return len(expired_keys)

    # ---- helpers ----
    def _available_set(self) -> Dict[str, bool]:
        return {u: True for u in self._available_slice()}

    def _available_slice(self) -> List[str]:
        if self.cfg.available is None:
            return []
        return self.cfg.available()


def _expired(e: _Entry, now: datetime, ttl: timedelta) -> bool:
    return (now - e.last_active) > ttl


def extract_key(body) -> str:
    """从请求体提取会话键：metadata.conversation_id → conversation_id → metadata.user_id。"""
    if not body:
        return ""
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, ValueError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        if v := str_or_empty(meta.get("conversation_id")):
            return v
        if v := str_or_empty(meta.get("user_id")):
            return v
    return str_or_empty(obj.get("conversation_id"))


def str_or_empty(v) -> str:
    s, _ = v, None
    if isinstance(v, str):
        return v
    return ""


def hash_index(key: str, n: int) -> int:
    """FNV-1a 哈希取模。"""
    h = 0x811C9DC5
    for ch in key.encode("utf-8"):
        h ^= ch
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h % n
