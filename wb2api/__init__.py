"""workbuddy2api —— WorkBuddy CN 的 OpenAI 兼容反向代理（Python 移植版）。

包结构（与 Go 原版对齐）：
    wb2api.config     配置加载 + 环境变量覆盖 + 时长解析
    wb2api.auth       账号凭证解析 / region 判定 / 原子写回
    wb2api.upstream  上游 HTTP 封装（chat / billing / auth）+ 错误分类 + SSE
    wb2api.pool      账号池（状态机 + 冷却 + 熔断 + 持久化）
    wb2api.session   会话粘性路由
    wb2api.scheduler 定时签到 / keepalive
    wb2api.redisstore Redis(Uptash) 镜像 + 纯内存降级
    wb2api.server    HTTP handler + 请求级日志
    cli.*            可执行入口（server / login / credit / signin）
"""

__version__ = "1.0.0-python"
