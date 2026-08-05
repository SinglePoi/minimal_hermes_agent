---
name: release-check
description: 夜莺项目发版前的检查清单与发布步骤。
platforms: [windows, linux, macos]
---

# 夜莺发版检查清单

内部工具发版前的核对流程，由客服团队负责人执行。

## 何时使用

- 用户询问"夜莺什么时候发版 / 发版流程"
- 发版前逐项核对

## 检查清单

1. 确认 .env 里的 DEEPSEEK_API_KEY 为生产密钥（只允许环境变量注入）。
2. 确认 AGENTS.md 代码规范未改动：中文 docstring、下划线工具名、记忆条目用 § 分隔。
3. 跑一遍回归测试：python tests/test_approval.py 和 tests/test_tool_dispatch.py。
4. 确认 approval_allowlist.json 未误提交（已 gitignore）。
5. 与客服团队确认无阻塞性需求。

## 发版步骤

```powershell
python minimal_agent.py  # 冒烟：查一次天气 + 问一次记忆
```

全部通过后通知团队，更新 HANDOFF.md 的进度记录。

## 参考文件

- references/rollback.md：回滚说明
