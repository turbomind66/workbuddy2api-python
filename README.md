<div align="center">

# workbuddy2api-python

**把腾讯 Copilot / WorkBuddy 账号能力封装成标准 OpenAI API 的反向代理（Go → Python 3 重写版）**

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey?logo=linux&logoColor=white)](https://github.com/turbomind66/workbuddy2api-python)
[![Dependencies](https://img.shields.io/badge/dependencies-requests%20%2B%20redis-brightgreen)](./requirements.txt)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)

[English](./README.md) · 简体中文

</div>

---

> **⚠️ 免责声明**
> 本项目为**个人学习与技术研究用途**的第三方工具，与腾讯官方无任何关联，非官方出品、不受官方支持。
> 本项目不破解、不绕过任何付费机制，仅复用你本人已登录账号的既有权益，把官方客户端能力转换为标准 OpenAI 协议接口。
> 使用本项目即表示你承诺遵守腾讯 Copilot / WorkBuddy 的[服务条款](https://www.codebuddy.cn/)。因使用本项目产生的任何账号风险或损失，由使用者自行承担。
> 若收到官方的停止使用要求，请立即停止使用并删除本项目。

---

## ✨ 功能特性

| 能力 | 说明 |
| --- | --- |
| **OpenAI 兼容 API** | `/v1/models`、`/v1/chat/completions`（同步 + SSE 流式），零改造接入任意 OpenAI 客户端 |
| **多账号池轮换** | `auths/` 下放多个凭证，三因子加权挑选 + 防惊群 + 在途租约限流 |
| **熔断与冷却** | 连续失败指数退避熔断（30m → 6h 封顶）；余额不足冷却至次日 04:00；429 短冷却 |
| **粘性会话** | 同一对话尽量路由到同一账号（TTL + 自动 GC），避免上下文频繁切换 |
| **定时签到** | 每日 09:00 / 21:00 自动签到，22:00 保活，随服务进程常驻 |
| **Redis 镜像** | 账号池状态可选镜像到 Upstash；未配置自动降级为纯内存模式 |
| **健壮性** | SSE 断连优雅处理、请求体指纹脱敏、tool_call 流式增量合并、凭证原子写回 |
| **积分查询** | 一键查询所有账号套餐余额 / 签到积分（已适配官方网关校验） |

### 与 Go 原版的差异

本仓库是 `workbuddy2api-master`（Go）**逐模块对等的 Python 重写**，业务逻辑一一对应（见[模块映射表](#-与-go-原版的对应关系)），并额外修复了若干运行时问题（流式 TTFB 类型、租约泄漏、客户端断连、`User-Agent` 网关校验等）。

---

## 📦 目录结构

```
workbuddy2api-python/
├── cli/                      # 命令行入口
│   ├── server.py             # 启动代理主服务
│   ├── login.py              # OAuth 登录，生成 auth 凭证
│   ├── credit.py             # 查询所有账号余额
│   └── signin.py             # 立即执行每日签到
├── wb2api/                   # 核心包
│   ├── config.py             # 配置加载（JSON + 环境变量覆盖）
│   ├── auth.py               # 凭证解析 / 原子写回
│   ├── pool.py               # 账号池（状态机 / 熔断 / 租约 / 加权挑选）
│   ├── session.py            # 粘性会话路由
│   ├── redisstore.py         # Upstash 镜像 / Noop 降级
│   ├── scheduler.py          # 定时签到 + 保活
│   ├── server.py             # HTTP 服务与端点
│   └── upstream/             # 上游客户端（SSE / 请求改写 / 脱敏 / 错误分类）
├── scripts/                  # 便捷启动脚本
├── .github/                  # Issue / PR 模板 + CI 工作流
├── config.example.json       # 配置模板
├── requirements.txt          # 依赖清单（requests + redis）
├── Dockerfile                # 容器化部署
└── docker-compose.yml        # 一键容器编排
```

---

## 🚀 快速开始

### 环境要求

- Python **3.9+**（推荐 3.11）
- 一个已注册的腾讯 Copilot / WorkBuddy 账号

### 1. 安装依赖

```bash
git clone https://github.com/turbomind66/workbuddy2api-python.git
cd workbuddy2api-python

python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -r requirements.txt
```

仅两个第三方依赖：`requests`、`redis`，其余全部使用标准库。

### 2. 准备配置

```bash
copy config.example.json config.json   # Windows
# cp config.example.json config.json   # Linux/macOS
```

编辑 `config.json`，**至少修改 `api_key` 为你自己的密钥**（默认值仅供体验）。

### 3. 登录账号

```bash
# 第一步：获取登录链接
py cli/login.py url

# 第二步：用浏览器打开上一步打印的链接，完成登录授权

# 第三步：轮询换取 token 并落盘到 auths/
py cli/login.py poll --save ./auths
```

成功后会在 `auths/` 下生成 `workbuddy-<uid>.json`。**需要多账号轮换就重复上述流程**，每个账号生成独立文件。

> ⚠️ `auths/` 已在 `.gitignore` 中排除，**请勿将凭证提交到任何公开仓库**。

### 4. 启动服务

```bash
py cli/server.py -config config.json
```

看到 `loaded N cn account(s)` 且 `listening on :7863` 即启动成功。

---

## 🔌 客户端接入

任意 OpenAI 兼容客户端（Cherry Studio、ChatBox、LobeChat、TurboMind、NextChat 等），按下表填写：

| 配置项 | 值 |
| --- | --- |
| Base URL / API 地址 | `http://127.0.0.1:7863/v1` |
| API Key | `config.json` 里的 `api_key` |
| 模型 | `/v1/models` 返回的任意 id，或填 `auto` 自动路由 |

命令行验证：

```bash
# 同步（非流式）
curl http://127.0.0.1:7863/v1/chat/completions \
  -H "Authorization: Bearer 你的api_key" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"

# 流式（SSE）
curl -N http://127.0.0.1:7863/v1/chat/completions \
  -H "Authorization: Bearer 你的api_key" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}],\"stream\":true}"
```

---

## 🌐 HTTP 端点

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/healthz` | 否 | 健康检查，返回 `healthy` / `total` |
| GET | `/status` | 是 | 完整账号池状态（uid / credits / cooling / breaker / in_flight） |
| GET | `/v1/models` | 是 | OpenAI 兼容模型列表（动态拉取 + 静态兜底） |
| POST | `/v1/chat/completions` | 是 | OpenAI 兼容对话（`stream:true/false` 均可） |

---

## 🛠️ CLI 命令

| 命令 | 说明 |
| --- | --- |
| `py cli/server.py -config config.json` | 启动代理主服务 |
| `py cli/login.py url` | 获取 OAuth 登录链接 |
| `py cli/login.py poll --save ./auths` | 轮询登录结果并落盘凭证 |
| `py cli/credit.py` | 查询所有账号余额（JSON 输出） |
| `py cli/credit.py -pretty` | 查询余额（美化输出） |
| `py cli/signin.py [auth_dir]` | 立即批量签到（默认 `auths` 目录） |

> `credit.py` 优先查询账号**套餐余额**（个人体验版 / 权益赠送包等，对应 App 里「我的积分」）；
> 仅当账号确实没有套餐时，才降级为查询签到活动积分（`total_credits`）。

> **路径说明**：所有 CLI 的相对路径（`config.json`、`auths/`、`data/`、`--save`）都由
> `wb2api/projpath.py` 统一解析 —— **先按当前目录找，找不到自动回退到项目根**。
> 因此 `cd cli` 后直接 `server.py` / `credit.py` / `signin.py` 也能正确定位账号，不会加载 0 个账号。

---

## ⚙️ 配置说明

`config.json` 完整字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `listen` | `:7863` | 监听地址（`host:port`） |
| `api_key` | — | API 鉴权密钥，客户端需带 `Authorization: Bearer <key>` |
| `auth_dir` | `./auths` | 凭证目录 |
| `state_file` | `./data/state.json` | 账号池状态持久化文件 |
| `region` | `cn` | 区域 |
| `cooldown.soft_rate` | `60s` | 429 短冷却时长 |
| `schedule.checkin_hours` | `[9, 21]` | 每日签到时间点 |
| `schedule.keepalive_hours` | `[22]` | 每日保活时间点 |
| `upstream.timeout_seconds` | `120` | 上游请求超时 |
| `features.sanitize_blacklist_fingerprints` | `true` | 请求体指纹脱敏 |
| `upstash.url` / `upstash.token` | 空 | Upstash Redis 镜像（留空则纯内存模式） |
| `pool.max_in_flight` | `3` | 单账号最大并发租约 |
| `pool.breaker_threshold` | `3` | 熔断触发失败次数 |
| `pool.breaker_cooldown` | `30m` | 熔断基础冷却时长 |
| `pool.breaker_cooldown_max` | `6h` | 熔断冷却上限（指数退避封顶） |
| `pool.idle_weight_per_hour` | `0.5` | 闲置补偿权重 / 小时 |
| `pool.idle_weight_max` | `5.0` | 闲置补偿权重上限 |
| `session_sticky.enabled` | `true` | 启用粘性会话 |
| `session_sticky.ttl` | `30m` | 粘性绑定有效期 |
| `session_sticky.gc_interval` | `5m` | 粘性会话 GC 间隔 |

> 时长字段支持 `s` / `m` / `h` 后缀（如 `60s`、`30m`、`6h`）。

### 环境变量覆盖

以下环境变量可覆盖 `config.json` 对应字段（优先级更高）：

| 环境变量 | 对应字段 |
| --- | --- |
| `WB2A_LISTEN` | `listen` |
| `WB2A_API_KEY` | `api_key` |
| `WB2A_AUTH_DIR` | `auth_dir` |
| `WB2A_STATE_FILE` | `state_file` |
| `WB2A_REGION` | `region` |
| `WB2A_SOFT_RATE` | `cooldown.soft_rate` |
| `WB2A_TIMEOUT_SECONDS` | `upstream.timeout_seconds` |
| `WB2A_SANITIZE_FINGERPRINTS` | `features.sanitize_blacklist_fingerprints` |

环境变量也可写入 `.env`（参考 `.env.example`）。

---

## 🐳 Docker 部署

```bash
# 1. 生成配置
cp config.example.json config.json && vim config.json

# 2. 先在宿主机完成登录（生成 auths/ 凭证）
py cli/login.py url && py cli/login.py poll --save ./auths

# 3. 启动容器
docker compose up -d

# 查看日志
docker compose logs -f
```

---

## 🔄 与 Go 原版的对应关系

| Go 包 | Python 模块 |
| --- | --- |
| `cmd/server` | `cli/server.py` |
| `cmd/login` | `cli/login.py` |
| `cmd/credit` | `cli/credit.py` |
| `cmd/signin` | `cli/signin.py` |
| `internal/config` | `wb2api/config.py` |
| `internal/auth` | `wb2api/auth.py` |
| `internal/pool` | `wb2api/pool.py` |
| `internal/session` | `wb2api/session.py` |
| `internal/scheduler` | `wb2api/scheduler.py` |
| `internal/redisstore` | `wb2api/redisstore.py` |
| `internal/server` | `wb2api/server.py` |
| `internal/upstream` | `wb2api/upstream/{errors,consts,headers,payload,sanitize,sse,client}.py` |

---

## ❓ 常见问题

<details>
<summary><b>启动提示 <code>loaded 0 cn account(s)</code></b></summary>

`auths/` 下没有有效凭证。请先执行 `py cli/login.py url` 并在浏览器完成授权，再执行 `py cli/login.py poll --save ./auths`。
</details>

<details>
<summary><b>客户端一直转圈不结束</b></summary>

请确认已用最新代码启动服务 —— 流式响应结束后服务会主动关闭 TCP 连接以通知客户端结束（Python `http.server` 不会自动 chunked）。
</details>

<details>
<summary><b>返回 <code>503</code> / <code>uid=-</code></b></summary>

说明本次请求没能挑到任何可用账号。返回的 `message` 会直接给出具体原因，按提示处理即可：

| `message` 前缀 | 含义 | 处理 |
|---|---|---|
| `no accounts loaded` | **账号池为空**，一个账号都没加载进来 | 检查 `config.json` 的 `auth_dir` 与 `auths/workbuddy-*.json` 是否存在 |
| `all accounts disabled` | 全部账号已被禁用（凭证失效） | 重新执行 `login` 登录 |
| `all accounts cooling` | 全部账号处于冷却 / 熔断中 | 访问 `/status` 看 `cool_remaining_sec`，等待自动恢复 |
| `all accounts in-flight full` | 全部可用账号的并发已达上限 | 稍后重试，或调大 `pool.max_in_flight` |
| `all accounts unavailable` | 其他混合原因 | 访问 `/status` 逐个排查 |

> 提示：`config.json` 与 `auth_dir` 中的**相对路径均基于项目根目录**解析，因此从 `cli/` 等子目录启动服务也能正确定位。
</details>

<details>
<summary><b>返回 <code>400</code>，<code>message</code> 里带 <code>model_param_invalid</code> / <code>code=11133</code></b></summary>

这是**上游模型拒绝了请求参数**（参数名/取值不被该模型接受），不是本服务的问题，也不是账号问题。

返回体会原样透传上游的 `code`、`msg`、`extError` 与 `requestId`，例如：

```json
{"error": {"message": "code=11133 | Invalid request parameters | ext=model_param_invalid | the request parameters were rejected by the model | requestId=...",
           "type": "api_error", "code": "11133"}}
```

处理方向：

- 换一个模型试试（如 `hy3`、`hy4-preview` 对参数的支持范围不同）；
- 检查请求体里是否带了上游不支持的字段（如某些 `temperature`/`top_p` 组合、`response_format`、工具定义等）；
- 需要向上游反馈时，提供 `requestId` 即可。

**定位是哪个字段**：上游的 `param` 字段经常为空（不告诉是哪个参数），所以服务会在每次 `400/415/422` 时：

1. 打一行 `转发体摘要`，列出 `model`、全部顶层字段、**非标准字段**、消息角色、内容形态、`tools`/`stream_options` 等：

   ```
   upstream 400 转发体摘要: model='hy4-preview' | keys=[...] | ⚠非标准字段=['service_tier']
   | roles=['developer','user'] | 形态=['content:string','content:text'] | stream_options={...}
   ```

2. 把完整转发体写到 `data/last_bad_request.json`（`data/` 已在 `.gitignore` 中，不会被提交）。

先看摘要里的 **⚠非标准字段** 和 **roles** —— 前者是上游大概率不认识的字段，后者若出现 `developer` 等角色需要考虑归一化。
</details>

<details>
<summary><b><code>credit.py</code> 显示 <code>code=10085 请求不合法</code></b></summary>

官方计费网关会校验 `User-Agent`，`python-requests` 默认 UA 会被拒绝。代码已统一设置合法 UA，正常会返回套餐余额。
仅当账号确实没有套餐时，才会降级为查询签到积分。
</details>

<details>
<summary><b>提示 <code>ModuleNotFoundError: No module named 'requests'</code></b></summary>

说明当前终端未激活虚拟环境。请先执行 `.venv\Scripts\activate`（Windows）或 `source .venv/bin/activate`（Linux/macOS）。
</details>

更多问题欢迎提交 [Issue](https://github.com/turbomind66/workbuddy2api-python/issues)。

---

## 🤝 贡献

欢迎提 Issue 和 PR！提交前请先阅读 [贡献指南](./CONTRIBUTING.md) 与 [安全策略](./SECURITY.md)。

```bash
# 开发模式
pip install -r requirements.txt
python -m py_compile $(find . -name "*.py" -not -path "./.venv/*")
```

---

## 📄 许可证

本项目基于 [MIT License](./LICENSE) 开源。

- 你可以自由使用、修改、分发本项目，包括商业用途
- 需在副本中包含原始版权声明与许可声明
- 软件按「原样」提供，不含任何明示或默示担保

---

## 🙏 致谢

- 原版 Go 实现 `workbuddy2api` 的作者与贡献者
- 腾讯 Copilot / WorkBuddy 提供的平台能力
- 所有提交 Issue、PR 与反馈的社区成员

---

<div align="center">

如果这个项目对你有帮助，欢迎点个 ⭐ Star！

Made with ❤️ by the community

</div>
