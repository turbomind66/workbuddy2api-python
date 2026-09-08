"""redisstore.py — 封装 Upstash(Redis) 持久化，并提供内存降级（Noop）。

设计约束：Upstash 走公网 TLS，单次 RTT 可能 50~300ms，因此所有写操作都是
fire-and-forget（后台线程 + 失败仅 debug 日志），读操作只发生在启动时。
未配置 url / 连接失败时降级为 Noop：一切功能照常工作（纯内存模式）。

等价于 Go 版 internal/redisstore/redisstore.go。
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Dict, Optional, Tuple

LOG = logging.getLogger("wb2api.redisstore")

KEY_TTL = timedelta(days=7)
READ_TIMEOUT = timedelta(seconds=3)
WRITE_TIMEOUT = timedelta(seconds=5)

BIND_PREFIX = "wb2api:bind:"
STATE_KEY = "wb2api:state"


class Store:
    def set_bind(self, key: str, uid: str, ttl: timedelta) -> None: ...
    def del_bind(self, key: str) -> None: ...
    def load_binds(self) -> Dict[str, str]: ...
    def save_state(self, data: bytes) -> None: ...
    def load_state(self) -> Tuple[Optional[bytes], bool]: ...


def _normalize_url(url: str, token: str) -> str:
    if len(url) >= 8 and (url[:8] == "rediss:/" or url[:7] == "redis:/"):
        return url
    host = url
    i = host.find("://")
    if i >= 0:
        host = host[i + 3:]
    return "rediss://default:" + token + "@" + host + ":6379"


class Noop(Store):
    def set_bind(self, key: str, uid: str, ttl: timedelta) -> None: pass
    def del_bind(self, key: str) -> None: pass
    def load_binds(self) -> Dict[str, str]: return {}
    def save_state(self, data: bytes) -> None: pass
    def load_state(self) -> Tuple[Optional[bytes], bool]: return None, False


class Upstash(Store):
    def __init__(self, client) -> None:
        self.client = client

    def set_bind(self, key: str, uid: str, ttl: timedelta) -> None:
        if ttl <= timedelta(0):
            ttl = KEY_TTL
        def run():
            try:
                self.client.set(BIND_PREFIX + key, uid, ex=int(ttl.total_seconds()))
            except Exception as e:  # noqa
                LOG.debug("[redisstore] debug: SetBind %s: %s", key, e)
        threading.Thread(target=run, daemon=True).start()

    def del_bind(self, key: str) -> None:
        def run():
            try:
                self.client.delete(BIND_PREFIX + key)
            except Exception as e:  # noqa
                LOG.debug("[redisstore] debug: DelBind %s: %s", key, e)
        threading.Thread(target=run, daemon=True).start()

    def save_state(self, data: bytes) -> None:
        def run():
            try:
                self.client.set(STATE_KEY, data, ex=int(KEY_TTL.total_seconds()))
            except Exception as e:  # noqa
                LOG.debug("[redisstore] debug: SaveState: %s", e)
        threading.Thread(target=run, daemon=True).start()

    def load_state(self) -> Tuple[Optional[bytes], bool]:
        try:
            v = self.client.get(STATE_KEY)
            if v is None:
                return None, False
            return v if isinstance(v, bytes) else v.encode("utf-8"), True
        except Exception:
            return None, False

    def load_binds(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        try:
            for key in self.client.scan_iter(match=BIND_PREFIX + "*"):
                k = key.decode("utf-8") if isinstance(key, bytes) else key
                val = self.client.get(k)
                if val is None:
                    continue
                v = val.decode("utf-8") if isinstance(val, bytes) else val
                out[k[len(BIND_PREFIX):]] = v
        except Exception:
            return out
        return out


def new(url: str, token: str) -> Store:
    """根据 url+token 构建 Store。url 为空 → Noop；连接失败 → Noop 降级。"""
    if not url:
        LOG.info("[redisstore] upstash 未配置，进入纯内存模式（Noop 降级）")
        return Noop()
    try:
        import redis  # 延迟导入：未安装时不强制依赖
    except ImportError:
        LOG.warning("[redisstore] 警告: 未安装 redis 库，降级 Noop（纯内存模式）。pip install redis 可启用")
        return Noop()

    full = _normalize_url(url, token)
    try:
        client = redis.Redis.from_url(full, socket_timeout=READ_TIMEOUT.total_seconds(),
                                      socket_connect_timeout=READ_TIMEOUT.total_seconds())
    except Exception as e:
        LOG.warning("[redisstore] 警告: redis 连接串解析失败 (%s)，降级 Noop", e)
        return Noop()
    try:
        if not client.ping():
            LOG.warning("[redisstore] 警告: upstash ping 失败，降级 Noop（纯内存模式）")
            client.close()
            return Noop()
    except Exception as e:
        LOG.warning("[redisstore] 警告: upstash 连接失败 (%s)，降级 Noop（纯内存模式）", e)
        try:
            client.close()
        except Exception:
            pass
        return Noop()
    LOG.info("[redisstore] upstash 已连接")
    return Upstash(client)
