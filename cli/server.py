"""cli/server.py — workbuddy2api 主服务入口：加载配置、构建 pool、起调度器与 HTTP 服务。

等价于 Go 版 cmd/server/main.go。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

# 将项目根目录加入 sys.path，保证 `py cli/server.py` 直接运行时可导入 wb2api 包。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from wb2api.auth import Auth
from wb2api.projpath import resolve_path
from wb2api.config import Config
from wb2api.pool import Pool
from wb2api.redisstore import Noop, new as redis_new
from wb2api.scheduler import Config as SchedConfig, Scheduler
from wb2api.server import Config as SrvConfig, serve
from wb2api.session import Config as SessConfig, Router
from wb2api.upstream import Client

LOG = logging.getLogger("wb2api")


def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy2API — OpenAI 兼容反向代理 (Python)")
    ap.add_argument("-config", default="config.json", help="path to config json")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    # 配置文件：相对路径先看 cwd，找不到再回退项目根。
    config_path = resolve_path(args.config)
    try:
        cfg = Config.load(config_path)
    except FileNotFoundError:
        LOG.warning("config %s not found, using defaults+env", config_path)
        cfg = Config.load("")
    except Exception as e:  # noqa
        LOG.error("load config: %s", e)
        return 1

    # 相对路径统一锚定到项目根（auths / data 目录），避免从 cli/ 启动时加载 0 账号。
    cfg.auth_dir = resolve_path(cfg.auth_dir, is_dir=True)
    cfg.state_file = resolve_path(cfg.state_file)

    auths = Auth.load_dir(cfg.auth_dir, cfg.region)
    LOG.info("loaded %d %s account(s) from %s", len(auths), cfg.region, cfg.auth_dir)
    if not auths:
        LOG.warning("未加载到任何账号：请确认 %s 下存在 workbuddy-*.json（项目根=%s）",
                    cfg.auth_dir, PROJECT_ROOT)

    # redisstore：未配置/连接失败 → Noop（纯内存模式，一切功能照常）。
    store = redis_new(cfg.Upstash.URL, cfg.Upstash.Token)

    p = Pool(cfg.state_file)
    p.set_store(store)
    p.restore_from_snapshot()  # 择新恢复：Redis 快照比本地新才采用，否则本地优先
    p.sync_to_dir(auths)       # 与 auths 目录对齐：新账号加入、已删除文件账号剔除

    p.set_breaker(cfg.Pool.BreakerThreshold, cfg.BreakerCooldownDur, cfg.BreakerCooldownMaxD)
    p.set_max_in_flight(cfg.Pool.MaxInFlight)
    p.set_weights(cfg.Pool.IdleWeightPerHour, cfg.Pool.IdleWeightMax)

    redis_mode = "noop"
    if not isinstance(store, Noop):
        redis_mode = "upstash"

    sess_router: Optional[Router] = None
    if cfg.SessionSticky.Enabled:
        sess_router = Router(SessConfig(
            ttl=cfg.SessionTTL,
            gc_interval=cfg.SessionGCInterval,
            store=store,
            available=p.available_uids,
        ))
        sess_router.load_from_store()
        sess_router.start_gc()

    def sticky_count() -> int:
        return sess_router.count() if sess_router else 0

    up = Client()
    up.http_timeout = cfg.Upstream.TimeoutSeconds
    up.sanitize_fingerprints = cfg.Features.SanitizeBlacklistFingerprints

    sch = Scheduler(SchedConfig(
        pool=p,
        upstream=up,
        checkin_hours=cfg.Schedule.CheckinHours,
        keepalive_hours=cfg.Schedule.KeepaliveHours,
    ))

    srv_cfg = SrvConfig(
        pool=p,
        upstream=up,
        api_key=cfg.api_key,
        session=sess_router,
        sticky_count=sticky_count,
        redis_mode=redis_mode,
        soft_cooldown=cfg.SoftRateDur,
        dump_dir=os.path.dirname(cfg.state_file) or ".",
    )

    stop_event = threading.Event()
    sch_thread = threading.Thread(target=sch.run, args=(stop_event,), daemon=True)
    sch_thread.start()

    def on_stop():
        stop_event.set()
        p.flush()  # 信号触发：先落盘再做优雅停机

    try:
        serve(srv_cfg, cfg.listen, stop_event=stop_event, on_stop=on_stop)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
