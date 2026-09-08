# 安全策略

## 支持的版本

| 版本 | 是否受支持 |
| --- | --- |
| main 分支（最新版） | ✅ |
| 历史 tag | ❌ |

## 报告安全漏洞

**请勿通过公开 Issue 报告安全漏洞。**

请通过 GitHub 的 [Private Vulnerability Reporting](https://github.com/turbomind66/workbuddy2api-python/security/advisories/new) 私密提交，并包含：

- 漏洞类型与影响范围
- 复现步骤与 PoC（如有）
- 潜在危害评估
- 建议的修复方案（可选）

我们会在 **72 小时内**响应，确认后协调修复并发布公告。

## 使用者安全须知

本项目会处理你的账号凭证，**请务必注意**：

1. **不要提交凭证**：`auths/`、`config.json`、`.env` 均已在 `.gitignore` 中排除，请勿强制添加。
2. **修改默认 API Key**：`config.example.json` 中的 `api_key` 仅供体验，部署前必须更换。
3. **不要暴露到公网**：默认监听 `:7863`，如需对外访问请配合反向代理 + HTTPS + 强鉴权。
4. **妥善保管 refresh_token**：泄露等同于账号被长期控制。
5. **定期检查**：通过 `py cli/credit.py -pretty` 关注积分异常消耗。

## 已知非安全类限制

以下属于上游平台限制，非本项目安全问题：

- 计费网关校验 `User-Agent`，非官方客户端 UA 会被拒绝（代码已适配）
- 个人免费账号无企业套餐时，套餐接口返回 `code=10085`
