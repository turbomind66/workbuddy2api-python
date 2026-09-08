# 贡献指南

感谢你愿意为 `workbuddy2api-python` 做出贡献！🎉

## 开始之前

- 本项目是 Go 原版 `workbuddy2api` 的 Python 重写，**核心原则是保持与 Go 原版的逻辑对等**。
- 提交功能性改动时，请说明是否与 Go 原版行为一致；若不一致，请说明理由。
- 涉及账号安全、凭证处理、请求头构造的改动请格外谨慎。

## 开发环境

```bash
git clone https://github.com/turbomind66/workbuddy2api-python.git
cd workbuddy2api-python

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 提交流程

1. **Fork** 本仓库并创建特性分支

   ```bash
   git checkout -b feat/your-feature
   ```

2. **编码**（遵循下方代码规范）

3. **自检**

   ```bash
   # 全量编译检查
   python -m py_compile $(find . -name "*.py" -not -path "./.venv/*")

   # 冒烟导入检查
   python -c "import wb2api, wb2api.upstream, wb2api.server"
   ```

4. **提交**（遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)）

   ```bash
   git commit -m "fix(sse): 修复流式响应 TTFB 类型错误"
   ```

   | 类型 | 用途 |
   | --- | --- |
   | `feat` | 新功能 |
   | `fix` | 缺陷修复 |
   | `docs` | 文档更新 |
   | `refactor` | 重构（不改变行为） |
   | `perf` | 性能优化 |
   | `test` | 测试相关 |
   | `chore` | 构建 / 依赖 / 杂项 |

5. **发起 Pull Request**，填写模板中的每一项。

## 代码规范

- **Python 版本**：兼容 3.9+，不要使用 3.10+ 独有语法（如 `match`、`X | Y` 类型联合）。
- **依赖**：严格保持最小依赖，新增第三方库需先在 Issue 中说明理由。
- **类型注解**：公开函数请标注类型，格式对齐现有代码。
- **日志**：使用 `logging` 模块，不要直接 `print`（CLI 输出除外）。
- **错误处理**：沿用 `wb2api/upstream/errors.py` 的错误分类体系。
- **缩进**：4 空格；行宽不超过 100 字符。

## ⚠️ 提交前务必检查

**以下内容绝对不能出现在提交中**，否则会被直接关闭：

- `auths/` 目录下的任何凭证文件
- `config.json` 中的 `api_key`
- 真实的 access_token / refresh_token
- 你的 uid、手机号、企业 ID 等个人信息
- `.env` 文件

提交前请确认：

```bash
git status          # 检查是否有敏感文件被暂存
git diff --cached   # 检查 diff 内容
```

## 报告问题

提交 Bug 时请附带：

- 操作系统与 Python 版本
- 复现步骤
- 完整错误日志（**记得脱敏**）
- 已尝试的解决方法

## 行为准则

请保持友善、尊重与耐心。任何形式的骚扰、人身攻击、歧视性言论都不可接受。
