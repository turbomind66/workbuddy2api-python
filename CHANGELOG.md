# 更新日志

本项目的所有重要变更都会记录在此文件中。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [1.0.0] - 2026-09-07

由 Go 原版 `workbuddy2api` 完整重写为 Python 3 的首个正式版本。

### 新增

- **OpenAI 兼容 API**：`/v1/models`、`/v1/chat/completions`（同步 + SSE 流式）
- **多账号池**：三因子加权挑选、防惊群、在途租约限流、状态持久化
- **熔断与冷却**：失败指数退避熔断、余额不足冷却、429 短冷却
- **粘性会话路由**：同会话优先复用同一账号（TTL + GC）
- **定时调度**：每日 09:00 / 21:00 自动签到，22:00 保活
- **Redis 镜像**：Upstash 存储，未配置自动降级为纯内存模式
- **CLI 工具**：`server` / `login` / `credit` / `signin` 四件套
- **请求体指纹脱敏**、**tool_call 流式增量合并**、**凭证原子写回**

### 修复

- `sse.py` 中 `start_time` 类型不一致导致流式请求 `TypeError: float - datetime`
- `_log_chat_row` 误用 `timedelta.milliseconds` 导致日志崩溃（`AttributeError`）
- `_chat_loop` 成功路径未释放租约，导致 `in_flight` 泄漏、连续请求后全部 `503`
- 客户端中途断开时 `ConnectionAbortedError (10053)` 未捕获，抛出大量 traceback
- 流式响应结束未关闭连接，导致部分客户端一直等待响应结束
- 计费网关校验 `User-Agent`，`python-requests` 默认 UA 导致 `code=10085` 请求不合法
- `cli/` 入口缺少项目根路径注入，导致 `ModuleNotFoundError: No module named 'wb2api'`
- 多处 `Config.__init__` 缺少关键字参数默认值，导致 `TypeError: unexpected keyword argument`
- `login.py` 在账号端点返回空 `uid` 时无法落盘（新增 JWT `sub` 兜底提取）

### 变更

- 凭证文件统一为嵌套格式（`auth` / `account` 两级），同时兼容扁平下划线格式
- `credit.py` 优先查询套餐余额，无套餐时降级为查询签到活动积分
