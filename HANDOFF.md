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
skills.py                   Skills：frontmatter 解析（含嵌套前置条件）+ 发现 + 条件激活过滤 +
                            技能索引 + skills_list/skill_view + readiness 检查（对齐 skill_utils/skills_tool）
skills/                     示例技能包：release-check（发版清单），含 references/（weather-answer 已随
                             get_weather 删除，2026-08-07）
file_tools.py               文件工具：read_file/write_file/search_files + patch（replace + V4A 双模式，
                            模糊匹配/陈旧检测/语法提示）+ 敏感路径保护（对齐 file_tools.py + patch_parser.py
                            + fuzzy_match.py）
read_extract.py             文档抽取：.docx/.xlsx/.ipynb → 文本（zipfile+XML+JSON，零依赖，对齐
                            tools/read_extract.py；read_file 自动接入）
todo_tool.py                todo 工具：会话级内存任务清单 + 压缩重注入 + 历史水合
                            （对齐 tools/todo_tool.py；含 TODO_SCHEMA / get_todo_store）
redact.py                   敏感文本脱敏：前缀密钥/赋值/JSON/YAML/请求头/JWT/私钥/URL userinfo（对齐 redact.py）
tirith.py                   内容级安全扫描：终端注入/隐形字符/同形字域名/管道到解释器（tirith 的 Python 简化版）
demo_file_tools.py          文件工具可视化演示（离线，python demo_file_tools.py 直接跑）
tests/test_approval.py      审批回归测试（零依赖，python tests/test_approval.py 直接跑）
tests/test_tool_dispatch.py 并行执行回归测试（零依赖，python tests/test_tool_dispatch.py 直接跑）
tests/test_skills.py        Skills 回归测试（零依赖，python tests/test_skills.py 直接跑）
tests/test_skills_preconditions.py Skills 前置条件回归测试（零依赖，直接跑）
tests/test_file_tools.py    文件工具回归测试（零依赖，python tests/test_file_tools.py 直接跑）
tests/test_redact.py        脱敏回归测试（零依赖，python tests/test_redact.py 直接跑）
tests/test_read_extract.py  文档抽取回归测试（零依赖，python tests/test_read_extract.py 直接跑）
tests/test_todo_tool.py     todo 工具回归测试（零依赖，python tests/test_todo_tool.py 直接跑）
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
server.py                    HTTP 服务化：/chat + /chat/stream(SSE) + /approvals/* +
                             /sessions* + /skills /plugins /tools + 静态托管 web/（零新依赖）
dashboard_auth.py            用户名密码登录 + 无状态 session cookie（scrypt 哈希 + HMAC 签名，
                             对齐 Hermes plugins/dashboard_auth/basic；附 hash-password CLI）
web/                         前端静态站点（原生 HTML/CSS/JS，零构建）：index.html + login.html + app.js + style.css
web_tools.py                  联网工具：web_search（DuckDuckGo HTML，零依赖）+ web_fetch（抓正文），
                              SSRF 防护（对齐 Hermes plugins/web 思路简化）
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
2. **工具系统**：`memory`（模型主动写记忆）+ `session_search`（FTS5 历史检索）+
   `web_search` / `web_fetch`（联网）+ `get_current_time`（时间）+ provider 自带工具
   （`memory_search` / `vector_search`）；演示天气工具 `get_weather` 已删除（2026-08-07）
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
   只读工具（session_search / 记忆检索 / skills / 时间 / 联网等）并发，
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
    （与 write_file 同路径排队、不同路径并行）；V4A 补丁头格式已补齐（见 35）
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
      会话 ID 落 localStorage、可粘贴旧 ID 恢复、新对话按钮（侧栏 + 顶栏双入口）
    - 过程活动展示：run_agent_turn/process_turn 新增 events 参数，/chat 响应带
      events（tool/skill/source 三类，参数经 redact 脱敏、结果截断 300 字符），
      前端渲染成"活动托盘"放在助手消息上方（实时展开），回复完成后自动收拢成
      一行摘要（▸ 🧠 思考 1 · 工具 2），点击展开/收起明细（2026-08-10 调整）；
      外部记忆召回也作为 source 事件
    - 事件持久化 + 重放还原（2026-08-10）：新增 events 表（session_id +
      user_message_id + type/name/args/result），process_turn 每轮落库（REPL 也内部
      收集）；persist_messages 返回本轮用户消息 rowid 供事件挂靠；
      GET /sessions/<id>/messages 返回 events，前端重放历史时按 user_message_id
      还原收拢态活动托盘；load_session_messages 补充 id 字段
    - 旁白进托盘（方案 B，2026-08-10，对齐 Codex 交互）：中间轮（带 tool_calls）
      的 assistant 旁白改为 **note 事件**（"过程说明"）进活动托盘并落库，
      不再通过 on_token 流进消息气泡；只有最终回答（不带 tool_calls）才一次性
      交给气泡——气泡只留最终回答，过程（思考/旁白/工具）全在可收拢的托盘里
    - Codex 式托盘样式（2026-08-10）：过程中显示"已耗时 X.Xs"计时（500ms 刷新）、
      过程条目直接展开（纯文本、无图标/无卡片、左侧细竖线分隔），最终回答出来后
      自动收拢成一行"已处理 · 耗时 Xs"（无计数），点击展开/收起；**每个工具调用
      条目独立展开/收起**参数与结果（▾/▸）；events 表补 duration_ms 列
      （ALTER 迁移，process_turn 计时落库），重放托盘也能显示耗时
      （2026-08-10 微调：工具条目默认收拢 ▸；无推理内容的思考不展示、不落库）
    - 旁白去重（2026-08-10）：persist_messages 跳过带 tool_calls 的中间轮
      assistant 消息——旁白只以 note 事件进托盘，不再落库成消息，历史回显
      不再"托盘 + 消息"重复出现
    - 轮次收尾内部指令（"已经达到本轮执行上限"）带 _finalize 标记：不落库、
      前端跳过旧数据，避免伪装成用户提问（2026-08-10；已清理历史污染行 2 条）
    - 思考过程回显：每轮模型调用前发 think 事件（"第 N 轮思考"），若模型暴露
      reasoning_content（如 deepseek-reasoner）则附推理文本（截断 + 脱敏）；
      deepseek-chat 不返回推理内容，只显示轮次标签
    - SSE 流式：call_llm_stream 累积流式 content/tool_calls/reasoning_content，
      POST /chat/stream 实时推送 activity/token/message/error/done
      （Connection: close 结束，注意别发 keep-alive 否则 http.server 不关连接）；
      前端 readSse 解析 + 按事件 id 增量更新活动，回复逐 token 上屏，
      流式失败自动回退一次性 /chat
    - 侧栏会话列表：GET /sessions（最后活跃倒序 + 预览 + 消息数）+ 点击经
      GET /sessions/<id>/messages 加载历史回显（对齐 Hermes api_server 的
      list_sessions / get_messages）
    - 会话归档：POST /sessions/<id>/archive 软归档（对齐 Hermes
      set_session_archived：sessions.archived 标记，不删数据，--resume 仍可恢复）；
      侧栏每条会话可归档；"新对话"下方【已归档】按钮把主工作区切换为归档
      会话列表（非抽屉），只显示已归档会话（走 archived_only=1，修复
      include_archived 把未归档也列出的问题），每条仅"取消归档"、不支持
      打开会话，支持返回对话；
    - 插件/技能视图：【插件】列 memory provider（GET /plugins，
      memory_manager.list_provider_plugins 枚举 providers/ + docstring 首行 +
      MEMORY_PROVIDER 启用标记），【技能】列可用技能（GET /skills 走
      skills.discover_skills）；与已归档共用通用工作区视图切换
      （VIEWS 注册表 + showView/closeView）；【工具】列全部工具（GET /tools：
      server 的 self.tools = 核心 TOOLS + provider 工具）；侧栏导航按钮收进
      .side-nav 紧凑分组（gap 6px）
      sessions 表首次访问自动 ALTER TABLE 迁移补 archived 列
    - 审批弹窗：/chat 阻塞期间每 800ms 轮询 pending，按钮 允许一次/本会话/
      永久允许/拒绝（拒绝可填理由）；allow_permanent=false（smart deny）时
      隐藏"永久允许"（对齐 Hermes api_server 的 _approval_event_choices）
    - /health 探活指示器；请求失败/离线有提示
    - 服务端配套：list_pending_approvals 暴露 allow_permanent；同一会话 /chat
      加串行锁（对齐 Hermes turn lease）
    - 回归测试：tests/test_server.py 新增静态端点、路径穿越拒绝、allow_permanent
      断言；另用 Node DOM 桩冒烟验证 发送→回复→审批弹窗→once/always/deny 全流程
25. **服务鉴权 + 操作审计**（2026-08-07）：对齐 Hermes `plugins/dashboard_auth` 的思路，
    简化为单静态 token（无 OIDC，零新依赖）——
    - `SERVER_AUTH_TOKEN` 设置后，除 `/health` 与静态页面（`/`、`/web/*`）外，所有 API
      要求 `Authorization: Bearer <token>`，未带/错误返回 401；hmac 常量时间比较防时序侧信道
    - 前端 401 时弹出 token 输入框，保存到 localStorage 后自动重试一次（流式/非流式都支持）；
      token 只存浏览器，请求自动带 `Authorization` 头
    - 操作审计：每个请求追加一行 JSON 到 `AUDIT_LOG_PATH`（默认 `audit.log`，已 gitignore），
      记录时间/来源 IP/方法/路径/动作/会话/状态/是否成功，token 一律 `«redacted»` 不打明文；
      审计失败不影响主流程（对齐 Hermes observability / api_server 操作日志，简化版）
    - 三同步完成：`.env` / `.env.example` / README 变量表（`SERVER_AUTH_TOKEN`、`AUDIT_LOG_PATH`）
26. **用户名密码登录 + session cookie**（2026-08-07）：对齐 Hermes
    `plugins/dashboard_auth/basic`（新文件 `dashboard_auth.py`，零新依赖——scrypt/HMAC 均 stdlib）——
    - `DASHBOARD_USERNAME` + 密码（`DASHBOARD_PASSWORD_HASH` 推荐 / `DASHBOARD_PASSWORD` 备用）
      配置后启用：未登录访问 `/` 302 跳 `/login`，API 需 session cookie 或 Bearer token
    - 密码用 stdlib scrypt 哈希（`python dashboard_auth.py hash-password <密码>` 生成），
      未知用户名也跑 dummy hash + 常量时间比较，防时序侧信道（对齐 Hermes basic）
    - 会话是无状态 HMAC-SHA256 签名 token（payload+签名，base64url），cookie
      HttpOnly + SameSite=Lax + Max-Age=TTL（默认 12h，`DASHBOARD_SESSION_TTL_SECONDS` 可调）；
      `DASHBOARD_AUTH_SECRET` 未配置时进程内随机（重启失效，同 Hermes basic 语义）
    - 双通道并存：人走 cookie、机器走 Bearer（`SERVER_AUTH_TOKEN`）；前端启动探测
      `/api/auth/config`，401 时登录可用则跳 `/login`、否则弹 token 输入框
    - 端点：GET /login（web/login.html）、POST /api/auth/login、POST /api/auth/logout、
      GET /api/auth/me、GET /api/auth/config（公开）；登录成功/失败进审计（identity=用户名）
    - 侧栏底部改为用户卡片：显示当前用户名（/api/auth/me），
      支持「切换账号 / 退出登录」（POST /api/auth/logout 后回 /login）；未启用人机登录时隐藏
    - 三同步完成：`.env` / `.env.example` / README 变量表（`DASHBOARD_*` 5 个）
27. **会话删除**（2026-08-07）：对齐 Hermes api_server 的 `_handle_delete_session` +
    `SessionDB.delete_session`——
    - `DELETE /sessions/<id>`（新增 do_DELETE 处理）：单个事务硬删 sessions + messages +
      messages_fts，返回 `{"session_id", "deleted": true}`；**仅已归档会话可删**（未归档 400），未知 404
    - 会话正在处理中（turn 锁被占用）→ 409 "session is busy"，防删进行中的对话
    - 同时清理进程内状态（AgentServer.remove_session：会话字典 + 网关审批注销，未决审批按拒绝唤醒）
    - 交互限制：删除按钮只在"已归档"列表出现（先归档、再删除），服务端一并强制校验
      （用户 2026-08-07 要求）；复用统一鉴权门卫 + 审计（action=sessions:delete）
    - 无新增环境变量；tests/test_server.py 新增删除端点组（未归档 400/归档后可删/硬删/FTS/404/409/审计）
28. **会话标题 + fork**（2026-08-07）：对齐 Hermes api_server 的 `_handle_patch_session` /
    `_handle_fork_session`——
    - sessions 表新增 title 列（_db_conn 自动迁移）；首条用户消息自动生成标题（40 字截断），
      list_sessions 返回 title（显示优先级 title → preview → session_id）
    - `PATCH /sessions/<id>`（新增 do_PATCH）：{"title"} 改名，空串清除；缺 title/超长 400、未知 404
    - `POST /sessions/<id>/fork`：fork_session 复制 system_prompt + 标题 + 全部消息（含 FTS），
      默认标题 "<源标题> fork"；未知 404、新 id 冲突 409；删除源不影响分支
      （简化：无 parent 血缘列，语义同 Hermes 分支子会话独立）
    - 前端"最近"列表每条新增【分支】；改名改为**双击会话条目**打开对话框；
      新增通用对话框组件（替代原生 confirm/prompt，删除确认也走它，2026-08-07 用户要求）
    - 审计 action=sessions:title / sessions:fork（_audit_action 增加 method 参数区分 PATCH/DELETE）
    - 无新增环境变量；tests/test_server.py 新增标题+fork 组（自动标题/改名/校验/fork 复制/删除源独立/审计）
29. **联网能力**（2026-08-07）：对齐 Hermes `plugins/web/` 思路，零依赖简化版（新文件 web_tools.py）——
    - `web_search`：多源链式回退——必应 RSS 优先（cn.bing.com/search?format=rss，中国大陆可达性
      好）→ DuckDuckGo HTML 兜底（uddg 还原真实链接）；单源失败自动切换，全挂时错误带各来源原因
      （2026-08-07 修复：DDG 在中国大陆不稳定，用户报"搜索失败 timed out"后实测确认并加回退）
    - `web_fetch`：抓 http/https 正文，去 script/style/标签、charset 识别、截断（默认 4000，1MB 上限）
    - SSRF 防护：仅 http/https 公网；拒绝 file/ftp、localhost、回环/私网/链路本地/未指定/保留/组播 IP
    - 已注册进 TOOLS + run_tool 分发 + 并行只读白名单；失败返回可读错误不中断 Agent Loop
    - 无新增环境变量；tests/test_web_tools.py 新增（解析/limit/错误/截断/charset/SSRF/分发/白名单）；
      真实联网验证通过（2026-08-07：搜索返回 10 条真实结果、example.com 抓取正常）
30. **终端无限等待修复 + 时间工具**（2026-08-07）：
    - 复现：模型误调 `terminal({"command": "date"})` 卡死——Windows cmd 的 date/time 是交互式
      内置命令，subprocess 继承服务终端 stdin 等输入，直到 120s 超时（用户报"无限等待"）
    - 修复：run_terminal 的 subprocess.run 加 `stdin=subprocess.DEVNULL`（实测 date 0.01s 返回）
    - 新增 `get_current_time` 工具（本地日期时间+星期，中文 docstring，进并行白名单）；
      SYSTEM_PROMPT 新增规则 12（日期时间用 get_current_time；联网用 web_search/web_fetch，
      不要用 terminal 模拟联网；不要调裸 date/time）
    - 测试：test_approval.py 新增 stdin=DEVNULL 断言组；test_tool_dispatch.py 新增
      get_current_time 格式/注册/白名单组；全套 17 套通过
31. **turn 级预算（Agent Loop 预算控制）**（2026-08-07）：对齐 Hermes max_iterations /
    iteration_budget / handle_max_iterations，简化版——
    - 新环境变量 `MAX_AGENT_TURNS`（默认 5，替代写死的 5）与 `TURN_TOKEN_BUDGET`
      （默认 0 = 不限制）；三同步完成（.env / .env.example / README 变量表）
    - call_llm / call_llm_stream 改为返回 (message, prompt_tokens)，循环内累计
      api_call_count 与 token_used；每轮模型调用前预算预检，触顶即收尾
    - 收尾 `_finalize_turn_summary`：不带工具再调一次模型请求最终回答（对齐 Hermes
      "Requesting summary"），失败退回占位消息；回复写回 messages 供 REPL/前端展示
    - 测试：tests/test_server.py 新增 turn budget 组（轮数上限 3 → 3 次工具循环+1 次收尾=4 次调用；
      token 预算 250/每轮 100 → 第 4 轮预检触顶收尾），全套 17 套通过
32. **脱敏专项**（2026-08-07，对齐 Hermes `agent/redact.py`）：
    - DB 连接串：`_DB_CONNSTR_RE` 打码 `scheme://user:pass@` 的密码；用户名用 `*` 支持空用户名
      `redis://:pass@`（Hermes 要求 user: 前缀，骨架小扩展）
    - 手机号：`_PHONE_CN_RE`（大陆 11 位，前后数字边界）+ `_SIGNAL_PHONE_RE`（E.164），
      打码 `138****5678` / `+86****5678`
    - URL 查询参数：`_SENSITIVE_QUERY_PARAMS` 精确匹配（token/api_key/code/access_token/
      x-amz-signature 等），`token_count`/`session_id` 不误伤；Hermes 对 Web URL 默认关闭，
      骨架为展示安全默认开启（无 OAuth 回跳）
    - 全部接入 redact_sensitive_text 第 4 步管线，read_file/审批面板/工具展示自动生效；
      tests/test_redact.py 新增专项组，全套 17 套通过
33. **并行执行中断语义**（2026-08-07，对齐 Hermes `agent/tool_executor.py`）：
    - `execute_tool_calls_segmented(..., interrupt_event)`：预置中断全跳过；并行段 0.2s 轮询
      事件 → cancel pending future + 3s grace + `shutdown(wait=False)` 放弃卡住的线程；
      未完成回填 `{"status": "cancelled"}`，结果顺序回填不破坏
    - `run_agent_turn` / `process_turn` / `run_tool` / `run_terminal` 均新增 interrupt_event 透传；
      每轮模型调用前检查，中断即"已中断"收尾
    - terminal 中断：`_kill_process_tree`（Windows `taskkill /F /T`）——shell=True 时 ping 等是
      cmd 孙进程，只 kill 父进程会因孙进程占管道让 communicate 卡死（实测复现后修复）
    - 服务端 SSE：`_sse` 改为返回 bool，客户端断开（写帧失败）置位事件停止本轮（/chat/stream）
    - 测试：test_tool_dispatch.py 新增 executor 中断组（预置/并行 pending 取消/顺序后续跳过）；
      test_approval.py 新增 terminal 中断组（预置 + 运行中 kill）；全套 17 套通过
34. **Skills 前置条件检查**（2026-08-07，对齐 Hermes `agent/skill_utils.py::extract_skill_conditions`
    + `agent/prompt_builder.py::_skill_should_show` + `tools/skills_tool.py`）：
    - frontmatter 解析器升级：缩进嵌套映射（`prerequisites.env_vars` / `metadata.hermes.*`）
      + 块式标量列表 + 块式映射列表（`- name: X`，现代 `required_environment_variables` 写法），
      零依赖；`true/false` 解析为布尔
    - 索引期条件激活：`metadata.hermes.requires_tools`（缺工具 → 隐藏）/ `fallback_for_tools`
      （主工具在 → 隐藏兜底）+ toolsets 两组；`discover_skills` / `build_skills_index` /
      `skills_list` 均支持 available_tools 过滤，None 时显示全部（对齐 Hermes 向后兼容语义）；
      discover 条目带 conditions 字段
    - 加载期 readiness：`check_skill_readiness` 合并旧式 `prerequisites.env_vars` 与新式
      `required_environment_variables`（字符串或 {name, optional, prompt, help}），
      os.environ 优先 + BASE_DIR/.env 兜底（空值视为缺失）；`skill_view` 主视图返回
      required/missing/setup_needed/readiness_status/setup_note；`prerequisites.commands`
      仅 advisory（列出缺失但不阻塞，对齐 Hermes 语义）；子文件视图不带 readiness
    - `minimal_agent.py` 接线：`available_tool_names(manager)` 汇总核心 TOOLS + provider
      工具名（桩 manager 无 get_all_tool_schemas 时只统计核心，压缩重建提示词路径不崩）；
      `build_system_prompt` 与 `run_tool` 的 skills_list 传 available_tools；
      SYSTEM_PROMPT 新增规则 13（setup_needed 时要如实报告缺失，不得假装技能可用）
    - 示例技能 release-check 增加 prerequisites + metadata.hermes 演示 frontmatter
    - 测试：新增 tests/test_skills_preconditions.py（9 组：嵌套解析/条件提取/过滤语义/
      索引与列表过滤/readiness env/commands advisory/skill_view 字段/.env 兜底），
      全套 18 套通过
35. **patch 工具增强：V4A 补丁 + 模糊匹配**（2026-08-07，对齐 Hermes `tools/patch_parser.py`
    + `tools/fuzzy_match.py` + `tools/file_tools.py::patch_tool`）：
    - patch 新增 `mode=patch`（V4A）：*** Update/Add/Delete/Move File: 四类操作批量改文件，
      Move 自动建父目录；返回 files_modified/created/deleted + unified diff
    - 两阶段应用：先全量校验（hunk 逐条模拟、纯新增 hunk 校验 @@ 上下文唯一、多 hunk
      已应用自动跳过、纯上下文无改动报错）后写盘，校验失败零写入
    - 模糊匹配策略链（replace 与 V4A 共用）：exact → line_trimmed →
      whitespace_normalized → indentation_flexible → escape_normalized → context_aware
      （保守相似度兜底）；非精确匹配自动重排缩进；相似度策略 replace_all 多命中拒绝；
      is_already_applied 判定"补丁已应用"
    - 安全：V4A 补丁头拒绝 `..` 穿越（绝对路径允许，对齐 Hermes），Move 两端与
      Update/Add/Delete 全过敏感路径检查
    - 陈旧检测（简化版，对齐 Hermes file_state 思路）：校验记录 mtime，应用阶段发现
      外部修改即失败并保留外部内容；本补丁自写文件刷新快照不误报
    - 语法提示：.py 应用后 ast.parse 检查（信息性不阻塞）；并行规划器沿用 V4A 路径提取
    - 测试：test_file_tools.py 新增 5 组（模糊策略/replace 兜底/V4A 解析/V4A 应用/安全/
      陈旧检测），test_tool_dispatch.py 新增 V4A 路径重叠组
36. **REPL 中断接线**（2026-08-07）：交互模式每轮一个全新 interrupt_event 传给
    process_turn/run_agent_turn；Ctrl+C 打断本轮（回到输入提示继续对话）而非杀进程；
    中断后补"（已中断，本轮停止）"消息保持历史连贯；一次性模式中断即退出
    （对齐 Hermes interrupt 语义；服务端 SSE 早已接好，这是 REPL 收尾）
37. **文档抽取**（2026-08-10，对齐 Hermes `tools/read_extract.py`）：
    - 新模块 read_extract.py：.docx/.xlsx/.ipynb 转纯文本，全部标准库
      （zipfile + XML + JSON），零第三方依赖；损坏文档抛 ExtractionError
    - docx 按段落输出、w:tab→制表符、w:br/w:cr→换行；xlsx 按可见工作表输出
      表格行（共享字符串/内联串/布尔/错误值，隐藏表跳过，5000 行 × 256 列上限）；
      ipynb 按 markdown/code/raw 分节（raw 无编号），兼容 nbformat 3
    - read_file_tool 自动接入：可抽取文档先抽取再分页（extracted_document=True，
      offset/limit/READ_MAX_CHARS 照常生效）；抽取失败返回明确错误
      （Hermes 回退到普通路径+二进制保护，骨架无二进制扩展名保护，直接给错误更安全）；
      TOOLS 的 read_file 描述注明支持文档格式
    - 测试：新增 tests/test_read_extract.py（5 组：扩展名/docx/xlsx/ipynb/read_file 集成），
      全套 19 套通过
38. **todo 工具（任务规划）**（2026-08-10，对齐 Hermes `tools/todo_tool.py`）：
    - todo_tool.py：TodoStore（写入/读取/合并/校验/去重/内容截断/总数 256 封顶）、
      todo_tool 入口（todos 字符串自动解析、非法输入报错、返回完整列表+状态统计）、
      TODO_SCHEMA（行为引导写在 schema 描述里）；每会话一个 store（get_todo_store 注册表）
    - 压缩重注入：context_compressor.compress_context 新增 todo_block 参数，
      run_agent_turn 压缩时把未完成任务清单（format_for_injection，稳定头
      TODO_INJECTION_HEADER，只含 pending/in_progress）追加进摘要块，任务跨压缩不丢
    - 历史水合：hydrate_todo_store 倒序找最近 todo 工具结果，要求 tool_call_id 与
      assistant 的 todo 调用配对（防伪造注入），结果 >512KB 跳过；main() --resume 与
      server.get_session 恢复时调用
    - 接入：TOOLS 注册 + run_tool 分发（session_key 定位 store）；tool_dispatch 的
      _NEVER_PARALLEL_TOOLS 加入 todo（有状态写入不与只读工具并发）
    - REPL 可视化：render_todo_lines 渲染清单行；模型每轮动过 todo 就打印
      「📋 当前任务清单」面板，启动/恢复会话时也先展示已有清单（对齐"人工可盯进度"）
    - 网页常驻任务清单卡片（2026-08-10）：run_agent_turn 在 todo 工具执行后发
      type=todo 事件（完整清单 JSON）；/chat 响应与 GET /sessions/<id>/messages
      均返回 todos（历史接口先水合 store）；前端 handleActivityEvent 把 todo
      事件路由到常驻卡片（不进活动托盘），切换会话/新对话时按响应还原或隐藏
    - 测试：新增 tests/test_todo_tool.py（7 组：store 基础/merge/注入格式/入口/注册表/
      水合/接入含压缩重注入），全套 20 套通过
39. **Windows 控制台 UTF-8 兜底**（2026-08-10，release-check 会话发现并修复）：
    minimal_agent.py 在 `console = Console()` 前对 win32 的 stdout/stderr 做
    `reconfigure(encoding="utf-8")`——修复 GBK 控制台下 rich 渲染 emoji 抛
    UnicodeEncodeError 首屏崩溃的问题；日常 REPL/冒烟不再需要手动设
    PYTHONIOENCODING（终端显示乱码属代码页显示问题，可 `chcp 65001`）
40. **LLM 自动生成会话标题**（2026-08-10，对齐 Hermes `agent/title_generator.py`）：
    - 新模块 title_generator.py：首轮用户→助手交换后**后台线程**生成 3-7 词标题，
      不增加回复延迟；请求小（各截 500 字）、temperature=0.3、max_tokens=500、
      无工具清单、不进主循环 token 预算；输出清洗（去引号/"Title:"前缀/只取
      第一行/80 字截断），失败静默返回 None（对齐 Hermes）
    - 落库用新 set_auto_title_if_empty（谓词+写入同一 UPDATE）：人工改名与后台
      生成并发时先落库的赢，自动生成绝不覆盖；persist_messages 不再同步写
      截断标题，REPL 一次性模式同步生成、交互模式后台异步，server 两个 /chat
      通道（一次性 + SSE）都接线并在 shutdown 时 join 排空
    - 骨架差异：LLM 失败回退"首条用户消息截断 40 字"（保留离线体验），
      Hermes 不回退；环境变量 `TITLE_GENERATION_ENABLED`（默认 true）三同步
    - 测试：新增 tests/test_title_generator.py（27 条断言：参数/清洗/开关/失败
      静默/原子写入/失败回退/首轮触发与多轮跳过）；test_server.py 标题组改为
      LLM 后台生成 + 人工改名不被后续对话覆盖；ServerFixture 默认关标题生成
      （防污染 BudgetFakeClient 精确调用计数与 seq 顺序假 client）
41. **终端输出清洗**（2026-08-10，对齐 Hermes `tools/ansi_strip.py` +
    `tools/tool_output_limits.py` + `terminal_tool.py`）：
    - 新模块 ansi_strip.py（完整 ECMA-48：CSI/OSC/DCS/SOS/PM/APC/nF/Fp/Fe/Fs/
      8-bit C1 + strip_ansi + sanitize_display_text 控制字符清洗）与
      tool_output_limits.py（truncate_output：上限 50000 字符、头 40% + 尾 60%
      + 省略标记；骨架无 config.yaml，硬编码 Hermes 默认值）
    - minimal_agent.py 新增 _clean_terminal_output：截断 → 剥 ANSI → 脱敏，
      三条终端返回路径（普通 subprocess.run + 可中断 Popen 正常/中断）全部接线；
      env/printenv/set/export/declare 类命令（_is_env_dump_command 按管道/分号
      拆段判定）走赋值规则打码，普通命令按 code_file=True 避免源码常量误伤
      （对齐 Hermes redact_terminal_output 语义）
    - 测试：新增 tests/test_terminal_output.py（33 条断言：截断/ANSI/C1/显示
      清洗/env 判定/清洗管线/run_terminal 接线）
42. **working_diff 工具**（2026-08-10，对齐 Hermes `tools/working_diff.py`）：
    - 新模块 working_diff.py：collect_working_diff 三模式（working=未暂存+未跟踪 /
      staged=git diff --cached / all=git diff HEAD+未跟踪），未跟踪文件用
      git diff --no-index /dev/null 折入（上限 50 个），返回
      {success, stat, diff, untracked, empty}；非 git 目录/非法模式/git 缺失
      均返回可读错误
    - 工具入口 working_diff_tool 返回 JSON；已注册 TOOLS（mode/paths 参数）+
      run_tool 分发 + 并行只读白名单（_PARALLEL_SAFE_TOOLS）
    - 测试：新增 tests/test_working_diff.py（23 条断言：三模式/路径过滤/空仓库/
      非 git/非法模式/工具入口/run_tool 分发/白名单）
43. **网页工作区改动视图**（2026-08-10，对齐 Hermes gateway 的 /diff 入口）：
    - server.py 新增 `GET /working_diff`（mode/paths 查询参数，复用
      collect_working_diff，非法模式 400；走统一鉴权门卫 + 审计 action=working_diff）；
      working_diff.py 新增 parse_diff_files 把合并 diff 按文件拆成
      {path/status(added|modified|deleted)/additions/deletions/diff}，端点
      files 字段附带返回；新增 summarize_files 输出
      {files/additions/deletions/added/modified/deleted} 供左侧一行汇总
    - web/ 侧栏新增【工作区】按钮与 working-diff-view（复用 VIEWS 注册表 +
      showView/toggleView）：**右侧为带层级的可折叠目录树**（按目录分组、
      目录先于文件排序、▸/▾ 折叠、折叠状态跨刷新保留；文件行 = 文件名 +
      新增/修改/删除徽标 + 增删行数，点击切换），**左侧顶部只显示一行汇总**
      （共 N 个文件 · 新增 +X · 删除 -Y，替代原 git diff --stat 长表），
      下方是选中文件的逐行 diff（红绿标注增删行、@@ hunk 高亮；
      diff --git/index/---/+++/mode 等文件头元信息不显示，二进制文件留一行提示）；
      working/staged/all 三档切换 + 刷新按钮；textContent 写入防 XSS；
      干净工作区/错误均有空态
    - 测试：test_server.py 新增端点组（success/字段含 files+summary/paths
      过滤/非法模式 400/鉴权 401/审计）+ 静态断言；test_working_diff.py 新增
      parse_diff_files + summarize_files 组（拆分/状态/增删行数/汇总/路径）
44. **LLM 调用健壮性（重试）**（2026-08-10，对齐 Hermes `agent/retry_utils.py`
    的 jittered_backoff 思路，简化版）：
    - 新模块 retry_utils.py：jittered_backoff（指数退避 + 随机抖动，
      1s/2s/4s 封顶 8s，防多会话同步重试风暴）+ is_retryable_error
      （429/5xx/超时/断连可重试；400/401/403/404 立即放弃，靠 status_code
      或类型/关键词兜底）+ call_with_retry（首次 + LLM_MAX_RETRIES 次，
      on_retry 回调做用户可见提示，耗尽后抛出最后一个异常）
    - 8 处调用全部接线：call_llm / call_llm_stream（只重试 create"接通"阶段，
      已出字不重试）/ review_memories / _finalize_turn_summary /
      memory_provider.extract_facts_with_llm / approval 智能审批 /
      context_compressor._summarize / title_generator.generate_title；
      REPL 侧重试提示走 _on_llm_retry 打 dim 文案，其余静默重试；
      重试失败后的回退行为与原来一致
    - 环境变量 `LLM_MAX_RETRIES`（默认 3，0 = 不重试）三同步
    - 测试：新增 tests/test_llm_retry.py（33 条断言：退避/分类/循环/上限/回调/
      call_llm 与流式与标题接线/0 重试旁路），全套 24 套通过

## 运行方式

```powershell
# 一次性问答
python minimal_agent.py "这个项目是做什么的？"
# 多轮 REPL
python minimal_agent.py
# 恢复会话
python minimal_agent.py --resume session-xxx
# HTTP 服务化（/chat + /chat/stream 流式 + 审批轮询/resolve）
python server.py                    # 默认 127.0.0.1:8000；浏览器打开 http://127.0.0.1:8000/
                                    # 即进入 Web 页面（对话 + 审批按钮）；端点见 README
# 文件工具离线可视化演示
python demo_file_tools.py
```

- 环境变量在 `.env`（DeepSeek 用 `DEEPSEEK_API_KEY`；向量用 `EMBEDDING_*`，已配好）
- 配置源：`.env` 优先于系统环境变量（`load_dotenv(override=True)`，2026-08-07 按用户要求调整）
- 常用开关：`MEMORY_PROVIDER=vector`（语义召回）、`CONTEXT_WINDOW`、`PROTECT_LAST_N`
- Windows 控制台 UTF-8 已内置兜底（见 39）；终端仍乱码可 `chcp 65001`
- 本机测试可用内置 Python：`C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`
- 提交策略：由用户手动提交；当前 HEAD `049b4c9`（文档抽取/todo 工具/事件托盘等，
  2026-08-10），前序 `01d8b72`（脱敏专项 + 并行执行中断语义）、`b9a0a7f`（turn budget）
- ⚠️ 工作树仍有未提交改动（提交由用户手动进行，新会话先看 `git status`）：
  最近的 Codex 式过程展示收尾——旁白进托盘（note）、活动托盘耗时/工具默认收拢/
  空思考不显示、todo 网页常驻卡片、duration_ms、轮次收尾内部指令不落库
  （见 20/38/39）+ 本次 HANDOFF/README 查漏补缺
- `.env` 当前激活：`DASHBOARD_USERNAME=admin` + `DASHBOARD_PASSWORD_HASH`（用户自配登录）、
  `MAX_AGENT_TURNS=5` / `TURN_TOKEN_BUDGET=0`、`AUDIT_LOG_PATH=audit.log`、
  `MEMORY_PROVIDER=vector` + `EMBEDDING_*`（密钥勿外泄、勿提交）

## 已验证的测试

> 注：下面各测试文件记录的"条数"是当时统计，可能随用例增改漂移；
> 以直接运行 `python tests/test_xxx.py` 的输出为准（当前共 20 套）。

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
- 回归测试脚本 `tests/test_skills_preconditions.py`（新增，2026-08-07）：嵌套
  frontmatter（prerequisites/metadata.hermes/块式列表）、条件激活过滤（requires/
  fallback/toolsets/向后兼容）、索引与 skills_list 过滤、readiness env（缺失 →
  setup_needed、补齐 → available、optional 不阻塞）、commands advisory、
  skill_view 字段与 .env 兜底
- 回归测试脚本 `tests/test_approval_deny.py`：11 条断言全过（glob/大小写/多条、
  deny 先于 allowlist 与 off、返回结构）
- 回归测试脚本 `tests/test_tirith.py`：16 条断言全过（终端注入/隐形字符/同形字/
  管道检测、开关与 fail-open、审批集成含 off 旁路）
- 回归测试脚本 `tests/test_gateway_approval.py`：11 条断言全过（阻塞/唤醒/FIFO/
  超时失败关闭/理由传递/session 持久化/unregister 唤醒为拒绝）
- 回归测试脚本 `tests/test_server.py`：断言全过（health/pending/resolve/chat 400、
  静态端点与路径穿越、会话列表与历史、归档含 archived_only、技能/插件/工具列表、
  过程事件含思考推理与工具结果、SSE 流式 activity/token/message/done 全链路）
- 回归测试脚本 `tests/test_server.py` 新增鉴权+审计组（2026-08-07）：未配 token 免鉴权、
  无/错 token 401、正确 token 放行、/health 与静态豁免、/chat 与审批端点受保护、
  审计 JSON Lines 含 401/200、动作名与会话 id，token 不打明文
- 回归测试脚本 `tests/test_server.py` 新增会话删除组（2026-08-07）：删除后列表/FTS/消息全清、
  进程内状态清理、未归档 400、归档后删除、未知 404、进行中 409、释放后可删、
  审计含 sessions:delete 与会话 id
- 回归测试脚本 `tests/test_server.py` 新增标题+fork 组（2026-08-07）：自动标题取首条用户消息、
  改名往返、缺 title/超长/未知校验、fork 复制全部消息与标题、删除源后分支仍可用、
  审计含 sessions:title 与 sessions:fork
- 回归测试脚本 `tests/test_web_tools.py`（新增，2026-08-07）：DuckDuckGo HTML 解析（标题/真实 URL/摘要）、
  limit/空关键词/无结果/请求失败、抓取去标签/截断/charset、SSRF 全类地址拒绝、run_tool 分发与并行白名单
- 回归测试脚本 `tests/test_web_tools.py` 新增多源组（2026-08-07）：必应 RSS 优先（有结果只调一次、
  请求必应域名）、全源失败错误带"必应/DuckDuckGo"来源与原因；真实联网验证 2.4s 返回 10 条真实结果
- 回归测试脚本 `tests/test_approval.py` 新增 stdin 断言组（2026-08-07）：subprocess 必须带
  stdin=DEVNULL 且 timeout=120（防 Windows 交互式 date/time 卡死）；`tests/test_tool_dispatch.py`
  新增 get_current_time 组（格式/注册/白名单）
- 回归测试脚本 `tests/test_tool_dispatch.py` 新增 executor 中断组（2026-08-07）：预置中断全跳过、
  并行段已完成保留 + 阻塞中 cancelled、顺序段后续跳过；`tests/test_approval.py` 新增 terminal
  中断组（预置 + 运行中 kill 整棵树，快速返回 cancelled）
- 回归测试脚本 `tests/test_server.py` 新增 turn budget 组（2026-08-07）：轮数上限 3 →
  3 次工具循环 + 1 次收尾 = 4 次模型调用；token 预算 250/每轮 100 → 第 4 轮预检触顶收尾
- 回归测试脚本 `tests/test_redact.py` 新增专项组（2026-08-07）：postgres/redis/mongodb 连接串
  密码打码、大陆/E.164 手机号、URL 查询参数敏感键打码与 token_count/session_id 不误伤、
  日期数字不误伤
- 回归测试脚本 `tests/test_dashboard_auth.py`（新增，2026-08-07）：scrypt 哈希往返/错误/非法、
  HMAC 签名往返/篡改/过期/密钥不匹配、登录 正确/错误/未知用户/未启用；
  HTTP 全流程 302 跳登录、登录种 HttpOnly cookie、带 cookie 放行、/api/auth/me、
  过期会话 401、登出清 cookie、Bearer 双通道并存、登录失败审计（身份=用户名、密码不打明文）
- 回归测试脚本 `tests/test_file_tools.py` 新增 patch 组：唯一替换/多次报错/replace_all/
  已应用 no-change/.env 拒绝/CRLF 保留（修复了 Windows write_text 双换行 bug）；
  并行测试补 patch+write 同路径顺序、不同路径并行
- 回归测试脚本 `tests/test_file_tools.py` 新增 V4A+模糊组（2026-08-07）：模糊策略链
  （缩进差异/空白折叠/无关文本不匹配/相似度 replace_all 拒绝/is_already_applied）、
  replace 模糊兜底、V4A 解析（四操作/CRLF/无定界/畸形 Move 行）、V4A 应用
  （多操作/Move 自动建目录/校验失败零写入/纯上下文报错/多 hunk 已应用跳过）、
  安全（.. 穿越/敏感路径/Move 两端/绝对路径允许）、陈旧检测（外部修改拦截且不覆盖）；
  `tests/test_tool_dispatch.py` 新增 V4A 路径重叠组（patch+写同路径顺序、不同路径并行）
- 回归测试脚本 `tests/test_read_extract.py`（新增，2026-08-10）：扩展名判定、docx
  （段落/tab/br/空文档/损坏）、xlsx（共享字符串/内联串/布尔/隐藏表跳过/空表/损坏）、
  ipynb（分节/nbformat3/无单元/坏 JSON）、read_file 集成（自动抽取标记/分页/损坏回退/
  普通二进制仍拒绝）
- 回归测试脚本 `tests/test_todo_tool.py`（新增，2026-08-10）：TodoStore 基础
  （校验/去重/截断/封顶）、merge 模式、注入格式（稳定头/只含未完成）、工具入口
  （字符串解析/非法输入/统计）、会话注册表、历史水合（配对/超限/伪造忽略）、
  接入（TOOLS/run_tool/顺序屏障/压缩重注入）
- 回归测试脚本 `tests/test_server.py` 新增事件持久化组（2026-08-10）：历史接口返回
  events（think/tool 齐全、带 user_message_id、挂靠在用户消息 id 上）；
  轮次收尾内部指令不落库（test_turn_budget 组）；新增旁白组
  （test_chat_narration_as_note：中间轮旁白 → note 事件、最终回复不含旁白、
  note 已落库、旁白不进历史消息——只在托盘）；新增 todo 网页组
  （test_todo_event_and_panel_data：事件含 todo 类型且带完整清单、/chat 与
  历史接口返回 todos）

## 已知限制 / 下一步候选

- 审批增强已完成（smart/熔断/混淆/deny/tirith）；cron 审批上下文
  （`approvals.cron_mode`）已按用户要求取消（2026-08-10），不再实施
- 前端 + 流式已完成（对话/审批/会话列表/归档/插件技能工具视图/过程活动/思考回显/SSE）；
  请求鉴权、操作审计、用户名密码登录、会话删除/标题/fork 全部完成（见 25~28）
  （Hermes web/ 有完整 dashboard，骨架只做最小聊天页）
- 思考内容回显依赖模型暴露 reasoning_content（deepseek-chat 无）；SSE 响应必须
  Connection: close（keep-alive 会导致 http.server 不关连接）；
  无推理内容的思考事件不展示、不落库（2026-08-10）
- ⚠️ `__pycache__/minimal_agent.cpython-312.pyc` 被 git 跟踪（.gitignore 已含
  __pycache__/ 但对已跟踪文件无效），建议 `git rm --cached` 一次
- ⚠️ `build/1.txt`（用户测试写文件工具的产物，内容"hello python / 肯德基疯狂星期四"）
  已被 git 跟踪，如不需要可 `git rm`（新会话先确认是否保留）
- 文件工具简化：V4A 补丁/模糊匹配/语法提示/简化陈旧检测（35）与文档抽取（37）已完成，
  仍无文件锁、跨 profile 检查；
  搜索仍跳过敏感文件（Hermes 也过滤敏感路径的搜索结果）
- 并行执行中断语义已完成（见 33）；turn 级 budget 已完成（见 31）
- 外部协议接入（候补，暂不做）：MCP（连外部工具/数据源，Hermes 有
  `hermes_cli/mcp_config.py` + `mcp_picker.py` + `optional-mcps/`）与 ACP
  （被 VS Code/Zed/JetBrains 等编辑器客户端调用，Hermes 有 `acp_adapter/` +
  `hermes acp` 子命令）；2026-08-07 用户确认写入候补、暂不实施
- Skills 增强：prune/reinject（20）与前置条件检查（34）已完成；剩余技能 hub 同步
  （Hermes 有，骨架简化掉了）
- 脱敏专项已完成（DB 连接串/手机号/URL 查询参数，见 32）；多外部 memory provider 同时挂
  **有意不做**（2026-08-07 用户确认，与 Hermes 单外部 provider 设计一致）

## 下一阶段（前端 / 服务增强）

- 服务增强线全部完成：请求鉴权、操作审计、用户名密码登录、会话删除/标题/fork（见 25~28，
  对齐 Hermes api_server 的 auth / DELETE / PATCH / fork）；联网（29）、时间工具（30）、
  turn budget（31）、脱敏专项（32）、并行中断语义（33）、技能前置条件（34）、
  V4A patch + 模糊匹配（35）、REPL 中断接线（36）、文档抽取（37）、todo 工具（38）
  均已完成
- 审批线已完成（cron 审批按用户要求取消，见上）
- 文件工具剩余：文件锁、跨 profile 检查（V4A/模糊/语法提示/简化陈旧检测/文档抽取已完成）
- working_diff 已完成（42，2026-08-10）；终端输出清洗已完成（41）
- Skills 剩余：技能 hub 同步
- 运维：服务化下的日志、进程守护（Hermes 用 systemd/gateway daemon）
- 中断接线：REPL Ctrl+C 已完成（36）；一次性 /chat 无法感知断连（保持现状）
- 可选：web_search 升级为带 key 供应商（Tavily/SerpAPI 等，只改 web_tools.py 内部）

## 给新会话的起始指令（可直接粘贴）

> 请先阅读 `C:\Users\Administrator\Documents\Codex\2026-08-03\ru\outputs\minimal_agent\HANDOFF.md`
> 和该目录的 `README.md`，了解这个迷你 Agent 骨架的进度与约定。
> 之后所有代码决策与改动一律参考 `D:\space\hermes-agent-main` 的 Hermes 源码对齐。
> 我们上次停在这里（2026-08-10）：服务增强线全部完成（鉴权/审计/登录/会话删除/标题/fork，
  见 25~28）+ 联网（29）+ 时间工具（30）+ turn budget（31）+ 脱敏专项（32）+
  并行执行中断语义（33）+ Skills 前置条件检查（34）+ V4A patch/模糊匹配（35）+
  REPL 中断接线（36）+ 文档抽取（37）+ todo 工具（38）+ Windows UTF-8 兜底（39）；
  过程展示已 Codex 化（事件持久化、旁白进托盘、耗时收拢、todo 网页卡片，见 20/38）；
  全套 20 套回归通过；
  HEAD `049b4c9`（用户已提交主体）；工作树剩最近收尾（旁白进托盘/todo 网页卡片/
  耗时/空思考等）与 HANDOFF/README 本次查漏补缺，提交由用户手动进行。
> 下一步候选：文件工具剩余（文件锁/跨 profile）/ 运维日志与进程守护。
> working_diff（42）、终端输出清洗（41）、LLM 自动标题（40）已完成；
> cron 审批已按用户要求取消；Skills hub 已明确暂不做。

## 发版检查记录（2026-08-10，release-check 技能）

按 `skills/release-check` 清单逐项核对，结果：
1. DEEPSEEK_API_KEY 走环境变量/.env 注入（gitignore），未硬编码 ✓
2. AGENTS.md 代码规范未改动（与 HEAD 一致）✓
3. 回归：`tests/test_approval.py` + `tests/test_tool_dispatch.py` 全部通过 ✓
4. `approval_allowlist.json` 已 gitignore，未误提交 ✓
5. 冒烟：`python minimal_agent.py <问题>` 完整问答通过 ✓

发版前发现并修复一个阻塞性 bug：**Windows 中文控制台（GBK/cp936）下 rich 渲染
emoji（如「📖 已记住的信息」）抛 `UnicodeEncodeError`，`python minimal_agent.py`
首屏直接崩溃**。修复：minimal_agent.py 在 `console = Console()` 前对 win32 的
stdout/stderr 做 `reconfigure(encoding="utf-8")` 兜底（配套回归已重跑通过）。

注：终端若仍显示乱码属控制台代码页显示问题（非程序异常）；如需可视化 emoji 输出，
可在系统里 `chcp 65001` 切到 UTF-8 代码页。本记录供后续发版核对时参考。
