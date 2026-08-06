# 交接文档（HANDOFF）——新会话从这里开始

> 用途：本文件是上一个 Codex 会话的"交接摘要"。新会话请先读本文件 + `README.md`，
> 再继续开发。**所有后续代码决策与改动，一律先查 Hermes Agent 源码对齐**
> （本机源码：`D:\space\hermes-agent-main`）。

## 项目一句话

一个用 Python + DeepSeek 实现的迷你 Agent 骨架，逐步对齐 Hermes Agent 的核心架构
（Agent Loop、工具系统、三层记忆/召回、多轮对话、上下文压缩、可插拔 memory provider）。

## 约定：新增环境变量必须"三同步"

以后每次给代码新增可配置的环境变量（如 `APPROVAL_MODE`、`SESSION_RETENTION_DAYS`），
必须同步完成三件事，缺一不可：

1. **`.env`**：写入本机生效的配置（真实值，含默认值；密钥类变量由用户自行填写）
2. **`.env.example`**：写入模板（同样的键；非密钥用默认值，密钥用占位符如 `你的key`，
   可选变量用注释掉的示例）
3. **`README.md` 的环境变量表**：补充一行（变量、作用、默认值）

例外：仅用于测试的临时变量（monkeypatch 注入）不需要写入环境文件。

## 目录与文件角色

```text
minimal_agent.py            主程序：Agent Loop + REPL 多轮 + 记忆审查 + 工具系统
approval.py                 危险命令审批：模式检测 + 会话/永久批准 + 交互提示（对齐 tools/approval.py）
approval_allowlist.json     永久允许列表（已 gitignore，运行时生成）
tool_dispatch.py            工具并行执行：批分段规划 + 并发执行 + 路径重叠检测（对齐 tool_dispatch_helpers.py）
skills.py                   Skills：frontmatter 解析 + 发现 + 技能索引 + skills_list/skill_view（对齐 skills_tool.py）
skills/                     示例技能包：weather-answer（播报规范）、release-check（发版清单），含 references/
file_tools.py               文件工具：read_file/write_file/search_files + 敏感路径保护（对齐 file_tools.py）
redact.py                   敏感文本脱敏：前缀密钥/赋值/JSON/YAML/请求头/JWT/私钥/URL userinfo（对齐 redact.py）
tirith.py                   内容级安全扫描：终端注入/隐形字符/同形字域名/管道到解释器（tirith 的 Python 简化版）
demo_file_tools.py          文件工具可视化演示（离线，python demo_file_tools.py 直接跑）
tests/test_approval.py      审批回归测试（零依赖，python tests/test_approval.py 直接跑）
tests/test_tool_dispatch.py 并行执行回归测试（零依赖，python tests/test_tool_dispatch.py 直接跑）
tests/test_skills.py        Skills 回归测试（零依赖，python tests/test_skills.py 直接跑）
tests/test_file_tools.py    文件工具回归测试（零依赖，python tests/test_file_tools.py 直接跑）
tests/test_redact.py        脱敏回归测试（零依赖，python tests/test_redact.py 直接跑）
tests/test_approval_smart.py 审批增强回归测试（零依赖，python tests/test_approval_smart.py 直接跑）
tests/test_memory_sync.py   记忆异步同步回归测试（零依赖，python tests/test_memory_sync.py 直接跑）
tests/test_session_prompt.py 系统提示词持久化回归测试（零依赖，python tests/test_session_prompt.py 直接跑）
tests/test_session_cleanup.py 会话清理回归测试（零依赖，python tests/test_session_cleanup.py 直接跑）
tests/test_memory_nudge.py   记忆 nudge 回归测试（零依赖，python tests/test_memory_nudge.py 直接跑）
tests/test_skills_compression.py 技能压缩联动回归测试（零依赖，python tests/test_skills_compression.py 直接跑）
tests/test_approval_deny.py   用户 deny 规则回归测试（零依赖，python tests/test_approval_deny.py 直接跑）
tests/test_tirith.py          内容级扫描回归测试（零依赖，python tests/test_tirith.py 直接跑）
tests/test_gateway_approval.py 网关审批队列回归测试（零依赖，python tests/test_gateway_approval.py 直接跑）
tests/test_server.py          HTTP 服务化回归测试（零依赖，python tests/test_server.py 直接跑）
server.py                    HTTP 服务化：/chat + /approvals/pending + /approvals/resolve + /health（零新依赖）
context_compressor.py       上下文压缩（阈值 50%、protect_last_n、交接摘要）
memory_provider.py          MemoryProvider 抽象基类 + LLM 事实提取助手
memory_manager.py           外部 provider 编排（加载/召回/同步/工具路由）
providers/keyword/          示例 provider：本地 JSON + 关键词召回
providers/vector/           向量检索 provider：Qwen embedding + 余弦相似度
AGENTS.md                   项目上下文（常驻注入，示例：Agent 骨架）
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
   - 记忆审查 → `MEMORY.md` / `USER.md`（常驻注入，每 `MEMORY_NUDGE_INTERVAL` 轮
     默认 10、后台异步 + 会话结束）
4. **多轮对话**：REPL 连续问答、增量落库、`--resume <session_id>` 恢复
5. **上下文压缩**：`context_compressor.py`，中间轮次摘要化 + 保留最近 N 条 + merge-into-tail
6. **外部 memory provider 插件**：ABC + 动态加载 + 工具路由；`MEMORY_PROVIDER=keyword|vector`
7. **向量检索**：Qwen `qwen3.7-text-embedding`（阿里云百炼，OpenAI 兼容），`EMBEDDING_BACKEND=tfidf|local|api`
8. **危险命令审批**：新增 `terminal` 工具（先审批后执行）；硬性禁止地板 + 危险模式检测
   （删除/提权/SQL/git 破坏性操作/覆盖 .env 等）+ once/session/always/deny 交互选择 +
   会话级与永久级允许列表持久化（`approval_allowlist.json`，对齐 `tools/approval.py` 的
   DANGEROUS_PATTERNS / HARDLINE_PATTERNS / prompt_dangerous_approval / command_allowlist）
9. **工具并行执行**：`tool_dispatch.py` 对齐 Hermes `agent/tool_dispatch_helpers.py` 的
   `_plan_tool_batch_segments` + `agent/tool_executor.py` 的 `execute_tool_calls_segmented`；
   只读工具（get_weather / session_search / memory_search / vector_search）并发，
   memory / terminal 顺序屏障，路径重叠逻辑预留（读者↔读者可并行、含写者重叠关闭并行段），
   结果按原始顺序回填；vector provider 本地嵌入懒加载已加锁保证线程安全
10. **Skills（按需加载）**：`skills.py` + `skills/` 目录，对齐 Hermes 的渐进披露设计——
    系统提示词只注入技能索引（name + description），`skills_list` 列元信息、
    `skill_view` 加载 SKILL.md 全文或 references/ 子文件；frontmatter 零依赖解析
    （BOM 剥离、引号、列表、platforms 平台过滤），名字/路径穿越校验防越界；
    skill_view / skills_list 已加入并行白名单
11. **文件工具 + 敏感路径保护**：`file_tools.py` 对齐 Hermes `tools/file_tools.py`——
    read_file（分页+行号+字符预算截断+二进制拒绝）、write_file（先过
    _check_sensitive_path 再写）、search_files（文件名/内容递归，跳过敏感与排除目录）；
    拒绝清单：系统目录、.env、approval_allowlist.json、~/.ssh、凭据/启动文件、
    docker.sock——与 terminal 审批组成配对门；read_file/search_files 已接入
    并行规划器 _PATH_SCOPED_READERS、write_file 接入 _PATH_SCOPED_WRITERS
    （写同一路径排队、读写同路径顺序，接口零改动生效）
12. **敏感文本脱敏**：`redact.py` 对齐 Hermes `agent/redact.py`——mask_secret 头4尾4、
    _mask_token 头6尾4、file_read 用不可复用哨兵 `«redacted:sk-…»`（防写回损坏密钥）；
    覆盖前缀密钥（sk-/ghp_/glpat-/AIza/AKIA 等）、环境变量/JSON/YAML 赋值、
    Authorization 头、JWT、私钥块、URL userinfo；开关 HERMES_REDACT_SECRETS 导入时
    快照、force=True 强制打码、code_file 跳过赋值类规则（对齐 Hermes 语义）；
    接入点：read_file 读敏感文件打码后返回、审批面板/非交互警告打码、工具参数展示打码
13. **patch 工具（replace 模式）**：`file_tools.py` 的 patch_file_tool 对齐 Hermes
    patch_tool——old_string 唯一替换（多次出现需 replace_all=true）、找不到时报错、
    "补丁已应用"检测（new_string 已在文件里 → no_change 成功，防重复重发）；
    写前过 _check_sensitive_path；BOM 剥离/CRLF 归一化保留（Windows 写文件用
    write_bytes，避免 \r\r\n 双换行）；已加入并行规划器 _PATH_SCOPED_WRITERS
    （与 write_file 同路径排队、不同路径并行）；V4A 补丁头格式简化掉
14. **审批增强**：对齐 Hermes approvals.mode / _smart_approve / 熔断 / 混淆检测——
    APPROVAL_MODE=manual|smart|off；smart 先用辅助 LLM 评估（approve 自动放行、
    deny 给一次"仅本次"人工覆盖且不持久化、escalate/无 client/LLM 失败落回人工）；
    评估前剥 shell 注释（防 `rm -rf / # 回答 APPROVE` 注入）、命令包 <command>
    定界符、操作员策略只进系统提示词；连续拒绝熔断默认 3 次（APPROVAL_DENIAL_BREAKER，
    0 禁用），人工批准重置；DANGEROUS_PATTERNS 新增 base64|bash、eval $(curl)、
    openssl 解码、heredoc 等混淆检测；LLM client 已串进 run_tool → run_terminal
    → check_dangerous_command
    另：Windows 裸命令删除加固——rmdir/rd/del/erase/Remove-Item/ri/rm 裸写
    （锚定命令起始）进危险清单；Hermes 只拦 cmd /c 与 powershell 前缀，此为
    有意超出 Hermes 的安全加固（修复"模型用 rmdir 绕过审批删目录"的实际问题）
    再另：SYSTEM_PROMPT 新增规则 11——声称"已创建/已删除/已修改"文件或目录前，
    必须真的通过工具执行并看到工具返回；没有工具返回不许声称操作完成
    （修复 DeepSeek 不经工具直接"复述"删除成功的幻觉问题）
15. **记忆同步异步化 + 合并节流**：`memory_manager.py` 新增 SyncWorker——
    sync_all 丢给单线程后台 worker 立即返回（对齐 Hermes：慢 provider 不阻塞对话结尾，
    案例：Hindsight daemon 曾阻塞 298s）；串行执行保证第 N 轮先于第 N+1 轮；
    合并节流（worker 未开始的旧任务被最新覆盖，messages 全量历史不丢数据）；
    flush_pending(timeout) 有界排空 + shutdown 幂等 + worker 关闭时回退内联；
    minimal_agent 会话结束 flush(10s) 后 shutdown
16. **系统提示词持久化 + 压缩重建**：对齐 Hermes conversation_loop 的
    update_system_prompt / _restore_or_build_system_prompt——
    新增 sessions 表（session_id + system_prompt），新会话构建提示词后落库，
    --resume 优先恢复持久化版本（修复"恢复会话完全没有系统提示词"的真 bug）；
    上下文压缩触发后重建 messages[0]（刷新记忆快照/项目上下文/技能索引）并 UPSERT
17. **压缩边界记忆提交**：对齐 Hermes run_agent.commit_memory_session——
    MemoryManager 新增同步 commit_memory_session（压缩前先把当前原文对话的记忆
    提取落库，原文即将被摘要掉）；与后台异步的 sync_all 分工明确：
    常规每轮同步走后台，压缩边界必须同步先抢救；传入 messages 快照副本防就地改写
18. **会话历史清理策略**：minimal_agent.py 新增 prune_sessions()（对齐 Hermes
    SessionDB.prune_sessions）——启动时清理不活跃超过 SESSION_RETENTION_DAYS
    （默认 90，0 禁用）的旧会话；活跃度 = MAX(messages.created_at)，无消息会话
    回退 sessions.updated_at；连 messages + messages_fts 一起删；当前会话
    （protect_session_id）受保护；孤儿消息（无 sessions 行）与空会话也覆盖
19. **记忆 nudge**：对齐 Hermes memory.nudge_interval（默认 10）——
    MEMORY_NUDGE_INTERVAL 按用户轮次计数，达到间隔后台触发记忆审查（复用
    SyncWorker，不阻塞对话）；恢复会话时用"历史用户轮次 % 间隔"水合计数，
    跨会话连续；会话结束仍有一次收尾审查；helper 纯函数
    should_run_memory_nudge / hydrate_nudge_counter 可单测
20. **Skills 与压缩联动（prune/reinject）**：context_compressor.py 对齐 Hermes
    的 skill prune——`_skill_pruned_marker` 生成 `[SKILL_PRUNED: ...]` 标记、
    `_skill_view_call_sites` 识别加载过的技能、`_collect_ghosted_skill_names`
    收集"幽灵技能"（大结果 >5000 字符 + 已有标记）、`_reinject_pruned_skill_markers`
    摘要后把丢失标记补回 "## Pruned Skills" 区块（上限 20 个防膨胀）；
    _summarize 的输入里大技能裁成标记、小技能保留原文；SYSTEM_PROMPT 新增规则 10
    （看到标记用 skill_view 重载，每个技能一次）
21. **用户自定义 deny 规则**：approval.py 对齐 Hermes approvals.deny——
    APPROVAL_DENY（; 分隔的 fnmatch glob，大小写不敏感）命中即无条件拦截；
    检查位置在硬性禁止之后、永久允许列表与 mode=off 之前（用户说"永不"就是
    永不，连旁路都绕不过）；返回 user_deny=True + BLOCKED + "不要重试"
22. **内容级扫描 tirith.py**：Hermes tools/tirith_security.py 的 Python 简化版——
    检测正则认不出的语义威胁：ANSI 转义/控制字符/单独回车（终端注入）、零宽/
    双向覆盖符（隐形文字）、同形字域名（西里尔等易混淆字符）、管道到解释器；
    返回契约对齐 Hermes {"action": allow|warn|block, "findings", "summary"}；
    发现合并进审批门卫（block 需审批、warn 带警告），TIRITH_ENABLED /
    TIRITH_FAIL_OPEN 可配，mode=off 旁路仍优先（对齐 Hermes 顺序）
23. **HTTP 服务化 + gateway 审批通知**：server.py 零新依赖（标准库
    ThreadingHTTPServer）——POST /chat / GET /approvals/pending /
    POST /approvals/resolve / GET /health；approval.py 新增网关队列（对齐 Hermes
    register_gateway_notify / _await_gateway_decision / resolve_gateway_approval：
    _ApprovalEntry 带 threading.Event 当"门铃"，agent 线程阻塞、HTTP resolve 唤醒、
    FIFO、超时失败关闭、unregister 唤醒为拒绝）；非交互自动放行对网关会话豁免；
    主循环抽取 process_turn 供 REPL 与服务器共用；_lock 补齐线程安全
24. **前端 Web 页面（web/ 静态站点）**：对齐 Hermes dashboard 交互契约（参考
    Hermes `web/` 的 Vite + React 设计，按骨架惯例简化为原生 HTML/CSS/JS，
    零构建、零新依赖）——server.py 托管 `web/`（GET / 与 /web/*，含路径穿越
    防护），同一 origin 免跨域；布局参考 Codex 首页（毛玻璃侧栏 + 标题栏 +
    hero/建议卡片 + 底部输入框，首条消息后切会话线程）；品牌为通用 Agent
    （不再叫夜莺，也不限定客服天气等业务范围）：
    - 对话：POST /chat、Markdown 子集渲染（先转义防 XSS）、建议卡片一键发送、
      会话 ID 落 localStorage、可粘贴旧 ID 恢复、新对话按钮
    - 审批弹窗：/chat 阻塞期间每 800ms 轮询 pending，按钮 允许一次/本会话/
      永久允许/拒绝（拒绝可填理由）；allow_permanent=false（smart deny）时
      隐藏"永久允许"（对齐 Hermes api_server 的 _approval_event_choices）
    - /health 探活指示器；请求失败/离线有提示
    - 服务端配套：list_pending_approvals 暴露 allow_permanent；同一会话 /chat
      加串行锁（对齐 Hermes turn lease）
    - 回归测试：tests/test_server.py 新增静态端点、路径穿越拒绝、allow_permanent
      断言；另用 Node DOM 桩冒烟验证 发送→回复→审批弹窗→once/always/deny 全流程

## 运行方式

```powershell
# 一次性问答
python minimal_agent.py "这个项目是做什么的？"
# 多轮 REPL
python minimal_agent.py
# 恢复会话
python minimal_agent.py --resume session-xxx
# HTTP 服务化（/chat + 审批轮询/resolve）
python server.py                    # 默认 127.0.0.1:8000；浏览器打开 http://127.0.0.1:8000/
                                    # 即进入 Web 页面（对话 + 审批按钮）；端点见 README
# 文件工具离线可视化演示
python demo_file_tools.py
```

- 环境变量在 `.env`（DeepSeek 用 `DEEPSEEK_API_KEY`；向量用 `EMBEDDING_*`，已配好）
- 常用开关：`MEMORY_PROVIDER=vector`（语义召回）、`CONTEXT_WINDOW`、`PROTECT_LAST_N`
- Windows 中文乱码先设 `$env:PYTHONIOENCODING="utf-8"`
- 本机测试可用内置 Python：`C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`
- ⚠️ 当前工作树有大量未提交改动（整个骨架从零开始累积），新会话先看 `git status`

## 已验证的测试

> 注：下面各测试文件记录的"条数"是当时统计，可能随用例增改漂移；
> 以直接运行 `python tests/test_xxx.py` 的输出为准（当前共 15 套）。

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
- 回归测试脚本 `tests/test_tool_dispatch.py`：21 条断言全过（分段规划/整批可并行语义/
  路径重叠/并发真实发生/结果顺序回填）+ 假 client 端到端冒烟（2 个只读工具并行峰值=2）
- 回归测试脚本 `tests/test_skills.py`：28 条断言全过（frontmatter/BOM/平台过滤/发现/索引/
  加载/路径穿越拒绝）+ 假 client 端到端冒烟（skill_view 加载后模型据此回答）
- 回归测试脚本 `tests/test_file_tools.py`：25 条断言全过（分页/截断/二进制拒绝/新建覆盖/
  敏感路径拒绝/搜索跳过）+ 真实工具名路径重叠用例 + 假 client 端到端冒烟
  （write->read 顺序执行、.env 写入被拒）
- 回归测试脚本 `tests/test_redact.py`：26 条断言全过（打码格式/前缀密钥/赋值/JSON/YAML/
  请求头/私钥/JWT/userinfo/file_read 哨兵/force 与开关）+ 端到端冒烟
  （read_file 读 .env 显示 `«redacted:sk-…»`、审批命令 Bearer 打码）
- 回归测试脚本 `tests/test_approval_smart.py`：35 条断言全过（模式读取/三种 verdict/
  失败安全/注释剥离/smart 全流程/off 旁路/熔断阈值与重置/混淆检测）+ 端到端冒烟
  （smart APPROVE 后危险命令真实执行、全程无人工提示）
- 真实 DeepSeek 验证（2026-08-06）：修复"判决带解释尾巴导致误判 ESCALATE"——
  改为取回答中首个 APPROVE/DENY/ESCALATE 关键词；提示词改为果断判决版；
  实测 rm -rf build / Remove-Item build / cmd rd build -> approve，
  rm -rf / 与 /etc -> deny
- 真实 DeepSeek 验证（2026-08-06）：用户反馈"删文件夹仍提示风险"——定位为
  主模型（DeepSeek）自己用文字问"是否确认删除"，而非审批系统弹窗；
  SYSTEM_PROMPT 新增规则 9（危险操作直接调 terminal，审批交给系统，不要
  文字询问）；验证模型改为直接调用 terminal 工具；另新增 APPROVAL_DEBUG=1
  调试开关（打印辅助 LLM 原始回答与判决）、非交互自动放行文案改为中性 ℹ️
- 回归测试脚本 `tests/test_memory_sync.py`：9 条断言全过（异步不阻塞/串行顺序/
  合并节流只跑最新/flush 超时/shutdown 内联回退/无 provider 直接返回）
- 回归测试脚本 `tests/test_session_prompt.py`：6 条断言全过（落库往返/UPSERT 覆盖/
  压缩后 messages[0] 重建并持久化）
- 压缩边界 commit 测试：同步执行、拿到压缩前全量消息快照（49 条）、
  压缩路径先 commit 后重建（tests/test_memory_sync.py + tests/test_session_prompt.py）
- 回归测试脚本 `tests/test_session_cleanup.py`：11 条断言全过（旧会话删除 +
  FTS 清理、新会话保留、当前会话保护、孤儿/空会话清理、禁用与默认 90 天）
- 回归测试脚本 `tests/test_memory_nudge.py`：11 条断言全过（间隔触发/清零/禁用、
  恢复水合、后台 worker 立即返回与排空）
- 回归测试脚本 `tests/test_skills_compression.py`：14 条断言全过（标记往返/
  调用点识别/幽灵技能收集/补标记不重复/端到端：大技能裁标记 + 小技能留原文 +
  摘要后补回）
- 回归测试脚本 `tests/test_approval_deny.py`：11 条断言全过（glob/大小写/多条、
  deny 先于 allowlist 与 off、返回结构）
- 回归测试脚本 `tests/test_tirith.py`：16 条断言全过（终端注入/隐形字符/同形字/
  管道检测、开关与 fail-open、审批集成含 off 旁路）
- 回归测试脚本 `tests/test_gateway_approval.py`：11 条断言全过（阻塞/唤醒/FIFO/
  超时失败关闭/理由传递/session 持久化/unregister 唤醒为拒绝）
- 回归测试脚本 `tests/test_server.py`：9 条断言全过（health/pending 空与有挂起/
  resolve 唤醒阻塞线程/chat 正常对话与 400 校验）
- 回归测试脚本 `tests/test_file_tools.py` 新增 patch 组：唯一替换/多次报错/replace_all/
  已应用 no-change/.env 拒绝/CRLF 保留（修复了 Windows write_text 双换行 bug）；
  并行测试补 patch+write 同路径顺序、不同路径并行

## 已知限制 / 下一步候选

- 审批增强已完成（smart/熔断/混淆/deny/tirith）；剩余仅 cron 审批上下文
  （`approvals.cron_mode`，对齐 `tools/approval.py` 剩余部分）
- 前端页面已完成（对话 + 审批轮询/按钮）；剩余服务增强：SSE 流式响应 / 请求鉴权
  （token）/ 会话列表管理（Hermes web/ 有完整 dashboard，骨架只做最小聊天页）
- 文件工具简化：patch 只做 replace 模式（无 V4A 补丁头/模糊匹配/语法检查），
  无陈旧检测/文件锁、无文档抽取；
  搜索仍跳过敏感文件（Hermes 也过滤敏感路径的搜索结果）
- 并行执行的中断语义与 turn 级 budget 收尾（Hermes executor 有，骨架简化掉了）
- Skills 增强：prune/reinject 已完成；剩余前置条件检查、技能 hub 同步
  （Hermes 有，骨架简化掉了）
- 脱敏简化：URL 查询参数（Hermes 默认关闭）、手机号、DB 连接串专项未做

## 下一阶段（前端 / 服务增强）

- 服务增强：SSE 流式响应 / 请求鉴权（token）/ 会话列表管理
  （前端页面已完成，可直接在此基础上扩展）
- 审批剩余：cron 审批上下文（`approvals.cron_mode`）
- 运维：服务化下的日志、进程守护（Hermes 用 systemd/gateway daemon）

## 给新会话的起始指令（可直接粘贴）

> 请先阅读 `C:\Users\Administrator\Documents\Codex\2026-08-03\ru\outputs\minimal_agent\HANDOFF.md`
> 和该目录的 `README.md`，了解这个迷你 Agent 骨架的进度与约定。
> 之后所有代码决策与改动一律参考 `D:\space\hermes-agent-main` 的 Hermes 源码对齐。
> 我们上次停在这里：前端 Web 页面（已完成：server.py 托管 web/ 静态站点 +
  对话面板 + 审批轮询/按钮 + allow_permanent 暴露 + 会话串行锁；全套测试通过）。
> 下一步候选：服务增强（SSE 流式 / 鉴权 / 会话列表）或 cron 审批上下文。
