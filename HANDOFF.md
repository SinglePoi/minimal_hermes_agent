# 交接文档（HANDOFF）——新会话从这里开始

> 用途：本文件是上一个 Codex 会话的"交接摘要"。新会话请先读本文件 + `README.md`，
> 再继续开发。**所有后续代码决策与改动，一律先查 Hermes Agent 源码对齐**
> （本机源码：`D:\space\hermes-agent-main`）。

## 项目一句话

一个用 Python + DeepSeek 实现的迷你 Agent 骨架，逐步对齐 Hermes Agent 的核心架构
（Agent Loop、工具系统、三层记忆/召回、多轮对话、上下文压缩、可插拔 memory provider）。

## 目录与文件角色

```text
minimal_agent.py            主程序：Agent Loop + REPL 多轮 + 记忆审查 + 工具系统
approval.py                 危险命令审批：模式检测 + 会话/永久批准 + 交互提示（对齐 tools/approval.py）
approval_allowlist.json     永久允许列表（已 gitignore，运行时生成）
tests/test_approval.py      审批回归测试（零依赖，python tests/test_approval.py 直接跑）
context_compressor.py       上下文压缩（阈值 50%、protect_last_n、交接摘要）
memory_provider.py          MemoryProvider 抽象基类 + LLM 事实提取助手
memory_manager.py           外部 provider 编排（加载/召回/同步/工具路由）
providers/keyword/          示例 provider：本地 JSON + 关键词召回
providers/vector/           向量检索 provider：Qwen embedding + 余弦相似度
AGENTS.md                   项目上下文（常驻注入，示例：夜莺项目）
.env                        DeepSeek key + Qwen embedding 配置（勿提交）
.env.example                配置模板
requirements.txt            openai / python-dotenv / rich（sentence-transformers 注释备用）
sessions.db                 SQLite 会话历史（FTS5 全文索引）
MEMORY.md / USER.md         模型写入的核心记忆（§ 分隔，有占用率提示）
```

## 已完成功能（均对齐 Hermes）

1. **Agent Loop**：调模型 → 工具调用 → 结果回传 → 循环（`run_agent_turn`）
2. **工具系统**：`get_weather`（演示）+ `memory`（模型主动写记忆）+ `session_search`（FTS5 历史检索）+ provider 自带工具（`memory_search` / `vector_search`）
3. **三层记忆**：
   - 会话历史 → `sessions.db`（原始档案，FTS5 检索）
   - 外部同步 → LLM 提取事实 → 向量库（`sync_turn`，对齐 mem0 `infer=True`）
   - 记忆审查 → `MEMORY.md` / `USER.md`（常驻注入，每 3 轮 + 会话结束）
4. **多轮对话**：REPL 连续问答、增量落库、`--resume <session_id>` 恢复
5. **上下文压缩**：`context_compressor.py`，中间轮次摘要化 + 保留最近 N 条 + merge-into-tail
6. **外部 memory provider 插件**：ABC + 动态加载 + 工具路由；`MEMORY_PROVIDER=keyword|vector`
7. **向量检索**：Qwen `qwen3.7-text-embedding`（阿里云百炼，OpenAI 兼容），`EMBEDDING_BACKEND=tfidf|local|api`
8. **危险命令审批**：新增 `terminal` 工具（先审批后执行）；硬性禁止地板 + 危险模式检测
   （删除/提权/SQL/git 破坏性操作/覆盖 .env 等）+ once/session/always/deny 交互选择 +
   会话级与永久级允许列表持久化（`approval_allowlist.json`，对齐 `tools/approval.py` 的
   DANGEROUS_PATTERNS / HARDLINE_PATTERNS / prompt_dangerous_approval / command_allowlist）

## 运行方式

```powershell
# 一次性问答
python minimal_agent.py "夜莺项目什么时候发版？"
# 多轮 REPL
python minimal_agent.py
# 恢复会话
python minimal_agent.py --resume session-xxx
```

- 环境变量在 `.env`（DeepSeek 用 `DEEPSEEK_API_KEY`；向量用 `EMBEDDING_*`，已配好）
- 常用开关：`MEMORY_PROVIDER=vector`（语义召回）、`CONTEXT_WINDOW`、`PROTECT_LAST_N`
- Windows 中文乱码先设 `$env:PYTHONIOENCODING="utf-8"`
- 本机测试可用内置 Python：`C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`

## 已验证的测试

- 多轮对话跨轮次回答、`--resume` 恢复
- Qwen embedding 真调成功（1024 维）、向量召回 → 模型引用召回回答
- LLM 提取事实后入库（不存原话）、疑问句过滤
- 上下文压缩强制触发 + 压缩后早期信息仍可回答
- 工具误选修复：提示词规则明确"先用注入上下文，session_search 只搜对话历史"
- 危险命令检测：`rm -rf`、`git reset --hard`、`DROP TABLE`、`curl | bash`、覆盖 `.env`、
  `powershell -Command "Remove-Item ..."` 等命中；带 WHERE 的 DELETE 放行；`echo shutdown` 不误报
- 硬性禁止：`rm -rf /`、`shutdown`、`mkfs`、`dd` 写裸设备无条件阻止（会话批准也绕不过）
- 审批流：deny 返回 BLOCKED 且不执行；session 记忆同类模式；always 写盘并重启后仍生效；
  terminal 安全命令正常执行（退出码 + 输出）
- 回归测试脚本 `tests/test_approval.py`：41 条断言全过（检测/硬性禁止/审批分支/终端工具）

## 已知限制 / 下一步候选

- 工具并行执行（对齐 `_should_parallelize_tool_batch`）
- Skills（`SKILL.md` 按需加载）
- `sync_turn` 是同步 LLM 调用（Hermes 用后台异步 + 节流）
- 会话历史无清理策略（磁盘会增长，运维问题）
- 审批增强：Smart Approval（辅助 LLM）/ 连续拒绝熔断 / 命令混淆检测（base64、$() 等）/
  cron 与 gateway 审批上下文（对齐 `tools/approval.py` 剩余部分）
- 敏感文本脱敏（Hermes 用 `agent/redact.py`，审批面板与日志会显示原始命令）

## 给新会话的起始指令（可直接粘贴）

> 请先阅读 `C:\Users\Administrator\Documents\Codex\2026-08-03\ru\outputs\minimal_agent\HANDOFF.md`
> 和该目录的 `README.md`，了解这个迷你 Agent 骨架的进度与约定。
> 之后所有代码决策与改动一律参考 `D:\space\hermes-agent-main` 的 Hermes 源码对齐。
> 我们上次停在这里：危险命令审批（已完成：`approval.py` + `terminal` 工具）。
> 下一步候选：工具并行执行（对齐 `_should_parallelize_tool_batch`）。
