# 最简 Agent 骨架（完整版）· DeepSeek 版

一个 Agent Loop 最小实现，已具备：多轮对话 + 记忆 + 项目上下文 + 历史检索。
`minimal_agent.py`

## 功能

1. **Agent Loop**：调模型 → 模型要求调工具 → 执行 → 结果回传 → 再问模型 → 最终回答
2. **系统提示词**：角色/规则集中在 `SYSTEM_PROMPT` 常量；放一个 `SYSTEM_PROMPT.md`
   在同目录即可不改代码换人设
3. **简单记忆（参考 Hermes Agent 设计）**：
   - `MEMORY.md`（自己学到的知识）+ `USER.md`（用户画像），条目用 `\n§\n` 分隔
   - **模型主动写入**：对话中模型发现用户信息时调用 `memory` 工具
     （`action=add/replace/remove`，`target=memory/user`）立即落盘
   - **记忆 nudge**：每 `MEMORY_NUDGE_INTERVAL` 轮（默认 10，0 禁用）后台触发一次
     记忆审查，让模型补提遗漏信息，子串重叠自动去重；恢复会话时按历史轮次对齐
     计数（对齐 Hermes 的 nudge_interval 语义），审查不阻塞对话
   - **占用率提示**：注入带 `[45% — 1000/2200 chars]` 头部，工具响应回 `usage`，
     让模型知道记忆快满、主动合并（对齐 Hermes 的 char_limit）
4. **项目上下文（Context Files）**：自动发现并注入 `AGENTS.md`（当前目录和脚本目录），
   超长时保留头尾 + 中间省略标记（对齐 Hermes 的 context files / context 层）
5. **外部记忆 provider（插件）**：定义 `MemoryProvider` 抽象 + `MemoryManager` 编排，
   通过环境变量 `MEMORY_PROVIDER=keyword` 激活插件；每轮 `prefetch(query)` 召回相关记忆，
   注入用户消息；对话结束 `sync_turn` 用 **LLM 提取事实后再入库**（对齐 Hermes mem0 的
   `infer=True`，而不是存用户原话；无 client 时退化为启发式过滤）
   （对齐 Hermes 的 `agent/memory_provider.py`
   + `plugins/memory/` 插件机制）
   - **provider 自带工具**：插件可通过 `get_tool_schemas()` 暴露自己的工具
     （keyword 插件提供了 `memory_search`），模型可主动调用（对齐 Hermes 的
     `mem0_search` / `handle_tool_call` 路由）
   - **向量检索**：`providers/vector` 用「文本 → 向量 → 余弦相似度」召回，
     嵌入后端可切换（`EMBEDDING_BACKEND=tfidf|local|api`）
6. **历史会话检索（session_search）**：每轮对话写入 SQLite 会话库（FTS5 全文索引），
   模型需要回忆过去对话时主动调用 `session_search` 工具，返回命中会话的上下文窗口
   （对齐 Hermes 的 `tools/session_search_tool.py`）
7. **多轮对话**：同一会话内连续问答，历史消息逐轮累积、每轮增量落库；
   `--resume <session_id>` 恢复历史会话（对齐 Hermes CLI 会话循环 + `/resume`；
   系统提示词随会话持久化到 sessions 表，恢复时一并取回）
8. **上下文压缩**：消息占用超过阈值（默认 50% 上下文窗口）时，
   把中间轮次交给 LLM 生成"交接摘要"，保留最近 N 条完整消息
   （对齐 Hermes 的 `agent/context_compressor.py`：protect_last_n、merge-into-tail、
   优先用 API 真实 token 数；压缩后重建系统提示词刷新记忆快照；
   已加载的大段技能内容在压缩时裁成 `[SKILL_PRUNED: ...]` 标记（小技能保留原文），
   摘要后把丢失的标记补回 "## Pruned Skills" 区块，模型可按需 skill_view 重载）
9. **危险命令审批**：新增 `terminal` 工具（本地执行 shell 命令），执行前先过审批门卫：
   - 硬性禁止地板（`rm -rf /`、关机、格式化等无条件阻止，连批准选项都没有）
   - 危险模式检测（删除、提权、SQL DROP、git 破坏性操作、覆盖 `.env` 等）
   - 交互选择 once / session / always / deny，会话级与永久级允许列表持久化
   （对齐 Hermes 的 `tools/approval.py`）
10. **工具并行执行**：同一轮里模型发多个工具调用时，按 Hermes 的规则切成
    parallel / sequential 段——只读工具（session_search / 记忆检索 / 联网 / 时间等）并发跑，
    memory / terminal 等有副作用的按顺序屏障执行，结果仍按原始顺序回填
    （对齐 Hermes 的 `agent/tool_dispatch_helpers.py` + `agent/tool_executor.py`）
11. **Skills（按需加载）**：技能放 `skills/<技能名>/SKILL.md`（frontmatter 含
    name/description/platforms）；系统提示词只注入「技能索引」（名称 + 描述），
    模型需要时用 `skills_list` 查看、`skill_view` 加载全文或 references/ 子文件
    （对齐 Hermes 的 `agent/skill_utils.py` + `tools/skills_tool.py` 渐进披露设计）
12. **文件工具 + 敏感路径保护**：`read_file`（分页+行号+截断）、`write_file`（先过
    敏感检查再写）、`search_files`（文件名/内容递归搜索）；写 `.env`、
    `approval_allowlist.json`、`~/.ssh`、密钥文件、系统目录一律拒绝——与终端审批
    组成"配对门"，堵住绕过路径（对齐 Hermes `tools/file_tools.py` 的 `_check_sensitive_path`）；
    三个工具已接入并行规划器的路径重叠检测（写同一文件排队、读写同路径顺序）
13. **敏感文本脱敏**：`redact.py` 对齐 Hermes `agent/redact.py`——sk-/ghp_/glpat- 等
    前缀密钥、`KEY=value`、JSON/YAML 配置、Authorization 头、JWT、私钥块、URL
    userinfo、DB 连接串、手机号、URL 查询参数全部打码；`read_file` 读敏感文件改为"打码后读取"（不可复用哨兵
    `«redacted:sk-…»`，防模型把打码值写回文件），审批面板与工具参数展示同样打码
14. **patch 工具**：`file_tools.py` 的 replace 模式（对齐 Hermes patch_tool）——
    在文件里找 `old_string` 换 `new_string`，比整文件重写省 token；old_string
    必须唯一（除非 replace_all=true），找不到时若 new_string 已存在则判定
    "补丁已应用"返回 no_change（防模型反复重发）；写前照常过敏感路径检查；
    已接入并行规划器写者集合（与 write_file 同路径排队）
15. **审批增强**（对齐 Hermes approvals.mode / _smart_approve / 熔断）：
    - 审批模式 `APPROVAL_MODE=manual|smart|off`：off 直接旁路；
      smart 先用辅助 LLM 评估（approve 自动放行、deny 给一次"仅本次"人工覆盖、
      escalate 落回人工），manual 维持逐条询问
    - 连续拒绝熔断：辅助 LLM 连续 deny 达到阈值（默认 3）后拒绝消息附加
      CIRCUIT BREAKER 硬停警告；任何人工批准都会重置计数
    - 命令混淆检测：`base64 -d | bash`、`eval $(curl)`、`bash <<EOF`、
      openssl 解码后执行等模式加入危险清单
    - 用户自定义 deny 规则：`APPROVAL_DENY`（; 分隔的 fnmatch glob，如
      `rm -rf *;git push --force*`）命中即无条件拦截——先于永久允许列表与
      mode=off 旁路，用户说"永不"就是永不（对齐 Hermes approvals.deny）
    - 内容级扫描 `tirith.py`（Hermes tirith 的 Python 简化版）：检测正则
      认不出的语义威胁——ANSI 转义/控制字符/单独回车（终端注入）、零宽字符/
      双向覆盖符（隐形文字）、同形字域名（钓鱼）、管道到解释器；发现即进入
      审批门卫（block 需审批、warn 带警告），`TIRITH_ENABLED`/`TIRITH_FAIL_OPEN`
      可配，off 旁路仍优先
    - 智能评估防注入：命令先剥 shell 注释、包在 `<command>` 定界符里、
      系统提示词明确要求忽略命令内夹带指令（对齐 Hermes 设计）
16. **记忆同步异步化 + 合并节流**（对齐 Hermes memory_manager 的后台串行设计）：
    对话结束的 `sync_all` 不再阻塞主流程——任务交给单线程后台 worker 立即返回；
    快速连发多轮时未开始的旧任务被最新任务覆盖（messages 是全量历史，不丢数据）；
    会话结束有界等待排空（flush 超时 10s），同步卡死也不阻塞退出
17. **系统提示词持久化 + 压缩重建**（对齐 Hermes SessionDB 的 update_system_prompt）：
    - 系统提示词（人设/项目上下文/记忆快照/技能索引）构建后写入会话库 sessions 表，
      `--resume` 恢复时直接取回——修复"恢复会话没有系统提示词"的问题
    - 上下文压缩触发时重建系统提示词（拿到最新记忆快照）并同步持久化
    - 压缩边界先调用 commit_memory_session（对齐 Hermes）：把当前原文对话的
      记忆提取落库，再压成摘要——原文被摘要掉之前先抢救信息
18. **会话历史清理策略**（对齐 Hermes SessionDB.prune_sessions）：启动时清理
    不活跃超过保留天数的旧会话（默认 90 天，`SESSION_RETENTION_DAYS` 可调，
    0 禁用）；活跃度 = 最后一条消息时间（无消息会话回退 sessions.updated_at）；
   连 messages + FTS 全文索引一起删，当前正在使用的会话受保护
19. **HTTP 服务化 + gateway 审批通知**（为前端铺路，对齐 Hermes dashboard/gateway）：
    `server.py` 零新依赖（标准库 http.server）——`POST /chat` 发消息、
    `GET /approvals/pending` 轮询待审批、`POST /approvals/resolve` 解决审批、
    `GET /health` 探活；审批走 approval.py 的网关队列（对齐 Hermes 的
    register_gateway_notify / resolve_gateway_approval：agent 线程阻塞等"门铃"，
    HTTP 端 resolve 唤醒）；主循环抽取为 process_turn 供 REPL 与服务器共用
20. **前端 Web 页面（web/ 静态站点）**：对齐 Hermes dashboard 的交互契约（参考
    Hermes `web/` 的 Vite + React 设计，按骨架惯例简化为原生 HTML/CSS/JS，
    零构建、零新依赖）——`server.py` 直接托管 `web/`（`GET /` 与 `/web/*`），
    同一 origin 免跨域；布局参考 Codex 首页：左侧悬浮圆角毛玻璃侧栏（品牌/能力清单）+
    右侧一整块玻璃面板（顶部融入式标题栏显示会话/视图名，hero/建议卡片与会话
    线程共用，底部输入框融入对话块），首条消息后切到会话线程：
    - 视觉（2026-08-12 按 ui-ux-pro-max 设计系统美化）：背景叠加暖陶土/柔蓝/浅绿
      光斑渐变，侧栏与主内容为同款毛玻璃面板（blur 20-22px + 顶部高光 + 悬浮阴影）、
      导航与插件页标签用陶土色渐变胶囊；补齐 :focus-visible 焦点环与
      prefers-reduced-motion 无障碍支持；隐藏滚动条（保留滚动功能）
    - 输入框（2026-08-12 重做）：输入区 + 功能栏同处一个**白色圆角输入卡**
      （输入区在上、功能栏在下：左侧 Enter/Shift+Enter 提示，右侧**圆形纯图标
      发送按钮**，发送中禁用变灰；聚焦整卡描边）；对话容器（消息 + 输入卡）
      限宽 860px **居左**，右侧留白给后续【环境信息】卡片（首页不参与该容器）
    - 对话：`POST /chat` 发消息、渲染 Markdown 子集（代码块/行内代码/加粗/
      斜体/列表，先转义再包标签防 XSS）、建议卡片一键发送、会话 ID 自动生成并
      持久化到 localStorage、可粘贴旧会话 ID 恢复、新对话按钮
    - 过程活动展示：`POST /chat` 响应带 `events`（工具调用/技能加载/外部记忆召回，
      参数脱敏、结果截断），会话界面把思考/工具调用渲染成"活动托盘"放在助手
      消息**上方**：Codex 式交互——过程中显示"已耗时 X.Xs"计时、过程条目
      直接展开（纯文本，无图标/无卡片；**每个工具调用可独立展开/收起**参数与结果），
      最终回答出来后自动**收拢**成一行"已处理 · 耗时 X.Xs"，点击可展开/收起；
      工具调用条目默认收拢（▸），无推理内容的思考不显示；
      过程事件会**落库**（events 表，挂在本轮用户消息 id 下），切换会话重放历史时
      按轮次还原成收拢状态的活动托盘（含 duration_ms 耗时）；**中间轮的旁白也作为
      note 事件进托盘**且不再落库为 assistant 消息——历史消息只保留最终回答；
      气泡最终也以最终回答为准（流式中途的中间文字会先上屏，message 事件到达后
      覆盖），过程全在托盘（对齐 Codex 交互）
    - SSE 流式：`POST /chat/stream` 以 text/event-stream 实时推送
      activity（思考/工具/技能/来源）/ token（回复增量）/ message / error / done；
      前端优先走流式（思考与工具事件边发生边显示、回复逐 token 上屏打字机效果），
      旧服务器或流式不可用时自动回退一次性 `/chat`
    - 侧栏会话列表：`GET /sessions` 列出历史会话（最后活跃倒序 + 预览 + 消息数），
      点击条目经 `GET /sessions/<id>/messages` 加载历史回显；侧栏有"新对话"入口
      （对齐 Hermes api_server 的 list_sessions / get_messages）
    - 会话归档：每条会话可"归档"（软标记，对齐 Hermes set_session_archived），
      归档后从"最近"隐藏但数据保留、`--resume` 仍可恢复；"查看归档"视图可
      取消归档；侧栏"新对话"下方的【已归档】按钮把主工作区切换为归档列表，
      只显示已归档会话（`GET /sessions?archived_only=1`），每条仅"取消归档"、
      不支持打开会话，支持"← 返回对话"；`?include_archived=1` 表示未归档+归档都返回
    - 插件页（合并视图 + 标签页）：侧栏【插件】一个入口，页面顶部
      **MCP 服务器 / 记忆插件 / 技能 / 工具**四个标签页切换（数据一次并行拉取，
      点击标签切换展示）；MCP 卡片带"工具数（绿）/可并行（蓝）"徽标、卡片悬停
      浮起；与【已归档】一样切换主工作区、支持"← 返回对话"；侧栏导航按钮
      （新对话/已归档/插件/工作区）收进紧凑分组
    - 审批弹窗：/chat 阻塞期间每 800ms 轮询 `GET /approvals/pending`，展示命令与
      原因（服务端已脱敏），按钮 允许一次/本会话允许/永久允许/拒绝（拒绝可填理由）；
      smart deny 场景按网关数据的 `allow_permanent=false` 自动隐藏"永久允许"
      （对齐 Hermes api_server 的 `_approval_event_choices`）
    - 连接状态：`/health` 探活指示器；请求失败/服务离线有明确提示
    - 品牌：通用 Agent（不限定业务领域，对齐 Hermes 的通用助手形态）
    - 服务端配套：`list_pending_approvals` 暴露 `allow_permanent`；同一会话的
      /chat 加串行锁（对齐 Hermes turn lease，防并发请求竞争 messages 状态）
21. **服务鉴权 + 操作审计**（对齐 Hermes `plugins/dashboard_auth` 的思路，简化为单静态 token）：
    - `SERVER_AUTH_TOKEN` 设置后，除 `/health` 与静态页面（`/`、`/web/*`）外，所有 API
      要求 `Authorization: Bearer <token>`，未带/错误返回 401；用 hmac 常量时间比较防时序侧信道
    - 前端遇到 401 弹出 token 输入框，保存到 localStorage 后自动重试一次（流式/非流式都支持）
    - 操作审计：每个请求追加一行 JSON 到 `AUDIT_LOG_PATH`（默认 `audit.log`），
      记录时间/来源 IP/方法/路径/动作/会话/状态/是否成功，token 一律不打明文（`«redacted»`）
    - 审计失败不影响主流程；本地开发不配 token 时行为与之前完全一致
22. **用户名密码登录 + session cookie**（2026-08-07，对齐 Hermes `plugins/dashboard_auth/basic`）：
    - `DASHBOARD_USERNAME` + `DASHBOARD_PASSWORD`（或 `DASHBOARD_PASSWORD_HASH`）配置后，
      未登录访问 `/` 302 跳 `/login`，API 需有效 session cookie 或 Bearer token
    - 密码用 stdlib scrypt 哈希（`python dashboard_auth.py hash-password <密码>` 生成），
      不存明文；未知用户名也跑 dummy hash 防时序侧信道
    - 会话是无状态 HMAC-SHA256 签名 token（`dashboard_auth.py`），cookie 为
      HttpOnly + SameSite=Lax + Max-Age=TTL（默认 12h），HTTPS 部署可开 Secure；
      `DASHBOARD_AUTH_SECRET` 未配置时每次启动随机（重启后会话失效，与 Hermes basic 一致）
    - 人走 cookie、机器走 Bearer（`SERVER_AUTH_TOKEN`）双通道并存；登录成功/失败进审计
      （identity 记录用户名，密码不打明文）
    - 侧栏底部改为用户卡片：显示当前用户名，支持「切换账号 / 退出登录」（注销后回 /login）；
      未启用人机登录时卡片隐藏，仅保留品牌行
23. **会话删除**（2026-08-07，对齐 Hermes api_server 的 `_handle_delete_session` +
    `SessionDB.delete_session`）：
    - `DELETE /sessions/<id>`：单个事务硬删会话行 + 消息 + FTS 全文索引，返回
      `{"session_id", "deleted": true}`；**仅允许删除已归档会话**（未归档返回 400），未知 404
    - 会话正在处理中（turn 锁被占用）→ 409 "session is busy"，避免删掉进行中的对话
    - 同时清理进程内会话状态与网关审批注册（未决审批按拒绝唤醒）
    - 交互限制：删除按钮只出现在"已归档"列表（先归档、再删除）；服务端同时强制校验，
      绕过前端直接调 DELETE 也删不掉未归档会话
    - 删除走统一鉴权门卫 + 审计（action=sessions:delete，含 session_id）
24. **会话标题 + fork**（对齐 Hermes api_server 的 PATCH /api/sessions 与 /fork；
    LLM 自动标题对齐 Hermes agent/title_generator.py）：
    - sessions 表新增 title 列（首次访问自动迁移）；**首轮交换后后台线程用 LLM 生成
      3-7 词标题**（对齐 Hermes：不增加回复延迟、失败静默、低温度/小 token 请求；
      LLM 失败回退首条用户消息截断 40 字），`TITLE_GENERATION_ENABLED` 可整体关闭；
      列表显示优先级：title → 最后一条用户消息预览 → 会话 ID
    - `PATCH /sessions/<id>`：`{"title": "..."}` 手动改名（空串清除，回退自动标题）；
      缺 title 400、超长（>100）400、未知 404
    - `POST /sessions/<id>/fork`：复制源会话的 system_prompt、标题与全部消息（含 FTS 索引）
      开新会话，默认标题 `"<源标题> fork"`；源未知 404、新 id 冲突 409；
      删除源会话不影响分支（简化：无 parent 血缘列，与 Hermes"分支子会话独立"一致）
    - 前端"最近"列表每条新增【分支】按钮；**改名改为双击会话条目**打开对话框；
      新增通用对话框组件（web 自建 modal，替代原生 confirm/prompt，删除确认同样走它）
    - 均走统一鉴权门卫 + 审计（action=sessions:title / sessions:fork）
25. **联网能力**（2026-08-07，对齐 Hermes `plugins/web/` 思路，零依赖简化版）：
    - `web_search`：多源链式回退——必应 RSS 优先（中国大陆可达性好）→ DuckDuckGo HTML 兜底，
      无需 API key，返回标题/真实链接/摘要，限 1-10 条；单源失败自动切换，全挂时错误带各来源原因；
      `web_fetch`：抓取 http/https 网页正文（去 script/style/标签、charset 识别、
      默认截断 4000 字符、最多读 1MB）
    - SSRF 防护：只允许 http/https 公网地址，拒绝 file/ftp、localhost、回环/私网/链路本地/
      未指定/保留/组播 IP（域名解析到内网无法预检，简化）
    - 两个工具已进并行规划器只读白名单（可与其他只读工具并发）；失败返回可读错误不中断 Loop
    - 无新增环境变量（零依赖、零密钥）；web_tools.py 独立模块 + tests/test_web_tools.py 回归
26. **终端无限等待修复 + 时间工具**（2026-08-07）：
    - 修复：`terminal` 的 subprocess 加 `stdin=DEVNULL`——Windows cmd 内置的 `date`/`time`
      是交互式命令，模型误调裸 `date` 时会在服务终端里等输入造成"无限等待"（实测 0.01s 返回）
    - 新增 `get_current_time` 工具：直接返回本地日期时间 + 星期，模型问"今天几号/几点"走它，
      不再碰终端命令；已进并行只读白名单
    - SYSTEM_PROMPT 新增规则 12：日期时间用 get_current_time；联网用 web_search/web_fetch，
      不要用 terminal 模拟联网；不要调用裸 date/time
27. **turn 级预算（Agent Loop 预算控制）**（2026-08-07，对齐 Hermes max_iterations /
    iteration_budget，简化版）：
    - `MAX_AGENT_TURNS`（默认 5）：单次提问内"调模型"最大轮数，替代原先写死的 5；
      `TURN_TOKEN_BUDGET`（默认 0 = 不限制）：单次提问累计 prompt token 预算
    - 循环内累计模型调用次数与真实 token 用量（call_llm/call_llm_stream 现在返回用量）；
      每轮模型调用前做预算预检，触顶即收尾
    - 收尾对齐 Hermes `handle_max_iterations`：不带工具再调一次模型，请求"基于已有信息给出
      最终回答、不要再调工具"，失败时退回占位消息；回复照常写回 messages 供前端展示
28. **脱敏专项**（2026-08-07，对齐 Hermes `agent/redact.py`）：
    - DB 连接串：`postgres://user:密码@host` 等只打码密码（支持空用户名 `redis://:pass@`，
      为骨架扩展，Hermes 原正则要求 user: 前缀）
    - 手机号：大陆 11 位（`138****5678`）与 E.164（`+86****5678`），前后数字边界防误伤日期/长串
    - URL 查询参数：`token`/`api_key`/`code`/`access_token`/`x-amz-signature` 等敏感键值打码
      （精确匹配，`token_count`/`session_id` 不误伤）；Hermes 对 Web URL 默认关闭此规则，
      骨架为展示安全默认开启（无 OAuth 回跳链路）
    - 全部接入同一 `redact_sensitive_text` 管线：read_file 打码、审批面板、工具参数/结果展示自动生效
29. **并行执行中断语义**（2026-08-07，对齐 Hermes `agent/tool_executor.py`）：
    - `execute_tool_calls_segmented(..., interrupt_event)`：预置中断 → 全部跳过并回填
      `{"status": "cancelled"}`；并行段等待期间 0.2s 轮询事件 → 取消 pending future、
      给运行中工具 3s 优雅退出（对齐 Hermes grace）、未完成回填 cancelled、顺序回填不破坏
    - `run_agent_turn(..., interrupt_event)`：每轮模型调用前检查，中断即"已中断"收尾
    - `terminal` 支持中断：事件置位立即杀进程树（Windows `taskkill /T`，修 shell=True 孙进程
      占管道导致 communicate 卡死的问题），返回 cancelled
    - 服务端 SSE：客户端断开（写帧失败）→ 置位中断事件，停止本轮（/chat/stream 生效；
      一次性 /chat 无法感知客户端断开，保持现状）
30. **Skills 前置条件检查**（2026-08-07，对齐 Hermes `agent/skill_utils.py::extract_skill_conditions`
    + `agent/prompt_builder.py::_skill_should_show` + `tools/skills_tool.py`）：
    - frontmatter 解析器升级：支持缩进嵌套映射（`prerequisites.env_vars`、
      `metadata.hermes.requires_tools` 等）与块式列表（`- item` / `- name: X`），
      零依赖、不引 pyyaml
    - 索引期条件激活：技能可声明 `metadata.hermes.requires_tools`（缺工具 → 从索引隐藏）
      与 `fallback_for_tools`（主工具已存在 → 隐藏兜底技能），toolsets 两组同样支持；
      系统提示词技能索引、`skills_list` 均按当前可用工具集过滤（对齐 Hermes
      `build_skills_system_prompt(available_tools=...)`；未提供工具集时显示全部，向后兼容）
    - 加载期前置检查：`skill_view` 返回 `required_environment_variables` /
      `missing_required_environment_variables` / `setup_needed` / `readiness_status` /
      `setup_note`（env 缺失 → setup_needed，补齐后 available）；`prerequisites.commands`
      只做 advisory（列出缺失但阻塞，对齐 Hermes"command checks remain advisory only"）
    - 环境变量查询：os.environ 优先、`BASE_DIR/.env` 兜底（空值视为缺失，
      对齐 Hermes `_is_env_var_persisted` 的语义）
    - `minimal_agent.py` 接入：`build_system_prompt` / `run_tool` 汇总核心 TOOLS +
      provider 自带工具名传入过滤；SYSTEM_PROMPT 新增规则 13（skill_view 返回
      setup_needed 时要如实告诉用户缺什么，不得假装技能可用）
31. **patch 工具增强：V4A 补丁 + 模糊匹配**（2026-08-07，对齐 Hermes `tools/patch_parser.py`
    + `tools/fuzzy_match.py` + `tools/file_tools.py::patch_tool`）：
    - patch 工具新增 `mode=patch`（V4A 格式）：支持 `*** Update/Add/Delete/Move File:`
      四类操作批量改文件（Move 自动建父目录），可一次完成"改两处 + 新建 + 删除"；
      返回 files_modified/created/deleted + unified diff
    - 两阶段应用：先全量校验（hunk 逐条模拟、纯新增 hunk 校验 @@ 上下文唯一、
      多 hunk 中"已应用"的 hunk 自动跳过）后写盘，校验失败零写入
    - 模糊匹配策略链（replace 与 V4A 共用）：exact → line_trimmed →
      whitespace_normalized → indentation_flexible → escape_normalized →
      context_aware（相似度兜底，保守）；非精确匹配自动按文件实际缩进重排新文本；
      相似度策略在 replace_all 且多命中时拒绝执行
    - 安全：V4A 补丁头拒绝 `..` 穿越（绝对路径允许，对齐 Hermes），Move 两端与
      Update/Add/Delete 全部过敏感路径检查（.env 等拒绝）
    - 陈旧检测（简化版，对齐 Hermes file_state 思路）：校验阶段记录 mtime，
      应用阶段发现文件被外部修改即失败并保留外部内容，不覆盖
    - 语法提示：.py 文件应用后 ast.parse 检查（信息性，不阻塞）
    - 并行规划器沿用现有 V4A 路径提取：patch+写同路径顺序、不同路径并行
32. **REPL 中断接线**（2026-08-07）：交互模式每轮一个全新 interrupt_event，
    Ctrl+C 打断本轮（回到输入提示继续对话）而不是杀掉整个进程；
    中断后补一条"（已中断，本轮停止）"消息保持历史连贯（对齐 Hermes 的
    interrupt 语义；服务端 SSE 早已接好，这是 REPL 收尾）
33. **文档抽取**（2026-08-10，对齐 Hermes `tools/read_extract.py`）：
    - 新模块 `read_extract.py`：.docx / .xlsx / .ipynb 转纯文本，
      全部标准库实现（zipfile + XML + JSON），零第三方依赖
    - docx 按段落输出、tab/换行保留；xlsx 按可见工作表输出表格行
      （共享字符串/内联串/布尔/错误值，隐藏表跳过，行/列数上限防撑爆）；
      ipynb 按 markdown/code/raw 分节，兼容 nbformat 3
    - `read_file` 自动接入：遇到可抽取文档先抽取再分页返回
      （`extracted_document=True`，offset/limit 照常生效）；
      损坏文档给明确错误，不回退成乱码；普通二进制仍拒绝
34. **todo 工具（任务规划）**（2026-08-10，对齐 Hermes `tools/todo_tool.py`）：
    - 每个会话一个内存任务清单：`todo` 工具传 `todos` 参数写入、省略即读取，
      每次调用返回完整列表 + 状态统计（pending/in_progress/completed/cancelled）；
      `merge=true` 按 id 更新、默认整体替换；清单顺序即优先级
    - 上下文压缩时把未完成任务清单随摘要一起保留（稳定头
      `TODO_INJECTION_HEADER`，只注入 pending/in_progress），任务跨压缩不丢、
      压缩后模型不会重复做已完成的事
    - `--resume` 与服务端恢复会话时从历史消息水合最近的 todo 列表
      （要求 tool 结果与之前的 assistant todo 调用配对，防伪造注入；
      超大结果跳过）；条目内容/总数有上限防膨胀
    - 已接入并行规划器顺序屏障（有状态写入不与只读工具并发）
    - 网页常驻任务清单卡片：模型动过 todo 后前端渲染「📋 任务清单」卡片
      （[ ]/[>]/[x]/[~] 状态标记），/chat 响应与历史接口都返回 todos，
      切换会话自动还原；不占用活动托盘
35. **Windows 控制台 UTF-8 兜底**（2026-08-10，发版检查发现并修复）：minimal_agent.py
    在 `console = Console()` 前对 win32 stdout/stderr 做 `reconfigure(encoding="utf-8")`，
    修复 GBK 控制台下 rich 渲染 emoji 崩溃；日常运行无需手动设 PYTHONIOENCODING
36. **LLM 自动生成会话标题**（2026-08-10，对齐 Hermes `agent/title_generator.py`）：
    首轮用户→助手交换后**后台线程**用 LLM 生成 3-7 词标题（不增加回复延迟；
    各截 500 字、temperature=0.3、max_tokens=500、失败静默），
    `set_auto_title_if_empty` 原子写入，人工改名不被覆盖；LLM 失败回退首条
    用户消息截断 40 字；`TITLE_GENERATION_ENABLED` 可整体关闭
37. **终端输出清洗**（2026-08-10，对齐 Hermes `tools/ansi_strip.py` +
    `tools/tool_output_limits.py` + `terminal_tool.py`）：terminal 工具返回前
    对输出依次做**截断**（上限 50000 字符，头 40% + 尾 60% + 省略标记）→
    **剥 ANSI**（完整 ECMA-48，防模型把转义序列抄进文件写入）→ **脱敏**
    （env/printenv/set 类命令的 KEY=value 走赋值规则打码，普通命令按代码文件
    处理避免源码常量误伤）
38. **working_diff 工具**（2026-08-10，对齐 Hermes `tools/working_diff.py`）：
    查看工作区 git 改动——working（未暂存+未跟踪）/ staged（已 add）/ all
    （相对 HEAD 全部），未跟踪文件用 `git diff --no-index` 折入；已注册
    TOOLS + run_tool 分发 + 并行只读白名单；**网页侧栏新增【工作区】视图**
    （`GET /working_diff` 端点返回按文件拆分的 files 数组 + summary 汇总 +
    working/staged/all 切换；**右侧为带层级的可折叠目录树**（按目录分组，
    Codex 风格），**左侧顶部只显示一行汇总**（共 N 个文件 · 新增 +X · 删除 -Y），
    下方是选中文件的 diff——红绿标注增删行、@@ hunk 高亮，
    git diff 文件头元信息行（diff --git/index/---/+++/mode）不显示
    （路径与状态目录树已给出），二进制文件保留一行提示；
    对齐 Hermes gateway 的 /diff 入口），不再依赖模型调用即可直接看改动
39. **LLM 调用健壮性（重试）**（2026-08-10，对齐 Hermes `agent/retry_utils.py`
    的 jittered_backoff 思路，简化版）：调大模型失败时按"指数退避 + 随机抖动"
    重试——429 限流 / 5xx / 网络超时 / 断连会等 1s/2s/4s（封顶 8s，叠加随机量
    防同步重试风暴）再试，最多 `LLM_MAX_RETRIES` 次（默认 3，0 = 不重试）；
    400/401/403/404 等"重试也没用"的错误立即放弃；8 处调用全部接线（主循环、
    流式、记忆审查/提取、收尾、标题生成、智能审批、上下文压缩），重试失败后
    行为与原来一致（静默回退/报错）；**重试耗尽或不可重试错误会转成助手错误
    消息（REPL 与 Web 都不再裸 Traceback），本轮结束、可继续对话**；
    流式只重试"接通"阶段，已出字不重试
40. **REPL 斜杠命令**（2026-08-10，对齐 Hermes CLI 的 slash 体系）：
    `/help`（命令帮助）、`/sessions`（列出最近会话含 id/标题/消息数）、
    `/resume <id>`（**中途切换会话**：重载系统提示词与历史、水合 todo 清单、
    重置轮次/落库计数，无需重启）、`/diff [模式|路径]`（工作区改动：
    working/staged/all 摘要 + 文件清单，指定路径显示该文件完整 diff，
    未跟踪文件也可查）、`/exit` 保留原退出；未知命令提示 /help；
    REPL 状态收敛为 ReplState（/resume 的会话切换载体）
41. **后台/常驻终端**（2026-08-10，对齐 Hermes `tools/process_registry.py`）：
    `terminal(background=true)` 把长命令（构建/安装/起服务）转后台立即返回
    session_id，不再阻塞对话；新 `process` 工具管理：poll（非阻塞查状态 +
    已累积输出）/ wait（阻塞等到结束，带超时）/ kill（整棵树终止，
    Windows taskkill /T）；stdout/stderr 由守护线程持续排空，滚动缓冲
    200KB、返回前截断 50KB 防撑爆上下文；REPL 与 server 退出时 shutdown_all
    兜底清理，防孤儿进程
42. **会话导出**（2026-08-10，对齐 Hermes `hermes_cli/session_export_md.py`
    + `session_export_html.py`，简化版）：新模块 session_export.py 把会话
    渲染成 Markdown（frontmatter + 逐条消息头）或独立 HTML（内容转义防 XSS、
    零依赖内联样式）；REPL 新增 `/export <id> [md|html]` 写到 `./exports/`；
    Web 侧栏会话条目新增【导出】按钮（带鉴权下载 md）；服务端
    `GET /sessions/<id>/export?format=md|html`（附件下载，走统一鉴权 +
    审计 sessions:export）。简化掉：SHA256 导出校验、压缩分段、tool_calls
    明细（Hermes 有）
43. **中途问用户（clarify）**（2026-08-10，对齐 Hermes `tools/clarify_tool.py`
    + `tools/clarify_gateway.py`）：模型需要用户拍板/任务有歧义/收集反馈时调
    `clarify` 工具——单选（最多 4 个选项 + "其他"自由输入）/ 多选 / 开放式三种
    形态；REPL 直接终端交互（编号选择），Web 走网关队列（/clarify/pending
    轮询弹窗 + /clarify/resolve 唤醒，与审批同一套门铃思路，超时 300s 防挂死）；
    危险命令确认不用它（terminal 自带审批）；已加入并行白名单的"永不并行"集合
44. **两步式会话 API（先建后聊）**（2026-08-10，对齐 Hermes api_server）：
    `POST /sessions` 先创建空会话返回 session_id（服务端生成
    `session-时间戳-随机`，也可客户端传 id），再 `POST /sessions/<id>/chat[/stream]`
    聊天——session_id 在 URL 里，请求阻塞期间轮询天然可用（clarify/审批不再
    依赖"响应末尾才知道 id"）；原 `POST /chat[/stream]` 保留为兼容旧接口
    （隐式建会话 + 聊）；前端新建对话先调 POST /sessions 拿 id 再聊；
    REPL 仍走服务端生成；审计 sessions:create / sessions:chat
45. **OpenAI 兼容接口**（2026-08-12，对齐 Hermes `gateway/platforms/api_server.py`）：
    `GET /v1/models` 模型列表 + `POST /v1/chat/completions`（OpenAI Chat Completions
    格式，非流式 + `stream=true` SSE chunk 流）——Open WebUI / LibreChat / LobeChat
    等现成前端把 Base URL 指向 `http://host:port/v1` 即可直连骨架：
    - 会话默认按「system + 首条用户消息」sha256 推导稳定的 `api-<digest>` id，
      无状态前端跨轮复用同一骨架会话（对齐 Hermes `_derive_chat_session_id`）；
      新推导会话的首请求若自带历史则折入一次，之后以会话内状态为准
    - 客户端 system 消息作为临时指令层叠加（对齐 Hermes 的 ephemeral system
      prompt：不持久化、每请求重叠加、压缩重建后不污染）
    - `X-Hermes-Session-Id` 请求头显式续接指定会话（需配置 `SERVER_AUTH_TOKEN`，
      对齐 Hermes 安全门：未鉴权禁止枚举会话历史）；非法/超长 id 400
    - content 支持字符串或 parts 数组（text/input_text/output_text 拼接、图片等
      非文本 part 静默跳过，骨架管线不支持多模态）；请求体上限 5MB（413）、
      OpenAI 风格错误信封（400/403/413/500）、usage 按字符数估算
    - 响应带 `X-Hermes-Session-Id` 头；流式发标准 `chat.completion.chunk`
      （role → content 逐段转发 → finish → `[DONE]`），**工具调用发标准
      `delta.tool_calls` 帧**（Open WebUI 等客户端可直接显示"调用了什么工具、
      参数是什么"；参数用已脱敏版本，单帧携带完整参数），工具/技能活动同步发
      自定义 `hermes.tool.progress` 事件；均走统一鉴权 + 审计
      （openai:chat / openai:models）
46. **服务化日志 + 手动启动（运维线）**（2026-08-12，对齐 Hermes
    `hermes_logging.py` 简化版）：
    - `server_logging.py`：集中式 `setup_logging()`——JSON Lines 结构化日志 +
      大小轮转（默认 5MB / 3 份备份）+ 脱敏（所有字符串字段落盘前过
      `redact_sensitive_text`，密钥不打明文，对齐 Hermes RedactingFormatter）+
      会话关联（`set_session_context` / `clear_session_context`，thread-local）
    - 事件：`server.start` / `server.stop`（host/port/pid）、`turn.end`
      （session_id / duration_ms / reply_chars / tools）、`chat.error` /
      `chat_stream.error` / `openai_chat*.error` 异常路径（带 session_id）
    - 环境变量三同步：`SERVER_LOG_PATH`（默认 `logs/server.log`）、
      `SERVER_LOG_MAX_MB`（默认 5）、`SERVER_LOG_BACKUP_COUNT`（默认 3）
    - `server_ctl.ps1 start|stop|status|restart`：后台启动（隐藏窗口 + 输出重定向
      + `server.pid`）、按进程树停止（`taskkill /T`；pid 文件误删时按端口兜底、
      确认退出才清理 pid 文件）、探活；启动前检查重复实例与端口占用、启动后
      探活确认；与手动前台运行 `python server.py` 等价；`logs/` 与 `server.pid`
      已 gitignore（Hermes 参照 systemd / gateway daemon，Windows 手动模式为
      Task Scheduler / Windows 服务的前置阶段）
47. **MCP（Model Context Protocol）客户端**（2026-08-12，对齐 Hermes
    `tools/mcp_tool.py` + `hermes_cli/mcp_config.py` 简化版）：
    - `mcp_client.py`：stdio 传输的 JSON-RPC 2.0 客户端——启动子进程 →
      initialize → notifications/initialized → tools/list（含分页兜底）→
      tools/call；工具注册进 TOOLS，模型可像内置工具一样调用
    - 工具名带 `mcp__<服务器>__<工具>` 前缀（对齐 Hermes 的
      mcp_prefixed_tool_name）；配置在 `mcp_servers.json`（已 gitignore，
      `MCP_SERVERS_PATH` 可覆盖；参考 `mcp_servers.example.json`）
    - 安全（对齐 Hermes）：子进程环境只透传安全基线变量 + 配置显式 env、
      `${VAR}` / `${env:VAR}` 插值（密钥不进配置文件）、结果与错误回传前
      截断（50000）+ 脱敏；工具调用超时视为服务器卡死，终止子进程不挂死循环
    - 并行判定：MCP 工具默认串行（外部副作用未知），配置声明
      `supports_parallel_tool_calls: true` 才进入并行白名单
    - 退出清理：REPL / 服务停止时统一 shutdown_mcp() 终止外部子进程（防孤儿）
    - 网页：`GET /mcp` 服务器状态（名称/工具数/可并行）；侧栏【插件】合并视图
      （MCP 服务器 / 记忆插件 / 技能 / 工具四组一页，原独立技能/工具入口移除）
    - 未做（Hermes 有）：HTTP / StreamableHTTP / SSE 传输、自动重连、sampling
    - 注意：中文 Windows 下自研 Python MCP 服务器需自己
      `reconfigure(encoding="utf-8")`（MCP 规范要求 UTF-8；测试假服务器已示范）

## 你需要准备的

1. **Python 3.8+**：到 [python.org](https://www.python.org/downloads/) 下载安装
   （安装时勾选 "Add python.exe to PATH"）
2. **安装依赖**：

   ```powershell
   pip install -r requirements.txt
   ```

3. **一个 DeepSeek API Key**：到 [platform.deepseek.com](https://platform.deepseek.com) 注册并创建

> 也可以在项目根目录创建 `.env` 文件：
>
> ```text
> DEEPSEEK_API_KEY=你的key
> ```
>
> **配置源说明**：`.env` 优先于系统环境变量（`load_dotenv(override=True)`）。
> `.env` 里配置的值会盖过系统环境变量；`.env` 没有的键仍回退读取系统环境变量。

## 运行

PowerShell 里执行：

```powershell
# 1. 设置 API Key
# 方式 A：写入 .env（推荐，优先级最高）
#   在项目根目录 .env 里填：DEEPSEEK_API_KEY=sk-你的key
# 方式 B：系统环境变量（.env 未配置对应键时才生效）
#   $env:DEEPSEEK_API_KEY="sk-你的key"

# 2. 运行（两种方式任选）
python minimal_agent.py "北京天气怎么样"
# 或直接运行，然后输入问题
python minimal_agent.py
```

Windows 控制台 UTF-8 已内置兜底（minimal_agent.py 顶部 win32 reconfigure），
正常情况下无需手动设置；终端若仍显示乱码（代码页显示问题），执行：

```powershell
$env:PYTHONIOENCODING="utf-8"
```

## 服务化部署（HTTP + Web 前端，运维线：手动启动）

前台运行（调试用，Ctrl+C 停止）：

```powershell
python server.py                    # 默认 127.0.0.1:8000
python server.py 127.0.0.1 8001     # 换端口
```

后台运行（手动启动模式，日志落盘 + PID 管理）：

```powershell
.\server_ctl.ps1 start              # 后台启动：隐藏窗口 + 输出重定向 + server.pid
.\server_ctl.ps1 status             # 探活：显示 PID / 启动时间 / 地址
.\server_ctl.ps1 restart            # 重启
.\server_ctl.ps1 stop               # 按进程树停止（taskkill /T），确认退出后清理 pid
```

结构化日志（JSON Lines + 大小轮转 + 脱敏）：`logs/server.log`，stdout/stderr
在 `logs/server.out.log` / `logs/server.err.log`；轮转阈值与备份数用
`SERVER_LOG_MAX_MB` / `SERVER_LOG_BACKUP_COUNT` 调整（见环境变量表）。
`logs/` 与 `server.pid` 已 gitignore，不会误提交。

> 注意：`server_ctl.ps1` 是 UTF-8 带 BOM 的脚本（Windows PowerShell 5.1 需要
> BOM 才能正确解析中文注释）；如果 git 或编辑器把它转成了无 BOM，请保持 BOM。

### 服务化运行（HTTP + 审批）

```powershell
python server.py                 # 默认 127.0.0.1:8000
# 或指定地址端口：python server.py 0.0.0.0 9000
```

启动后浏览器打开 <http://127.0.0.1:8000/> 即进入 Agent Web 页面（对话 + 审批按钮）。

端点一览：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/` | GET | Web 前端页面（`web/index.html`） |
| `/web/*` | GET | 前端静态资源（app.js / style.css） |
| `/sessions` | GET | 会话列表（按最后活跃倒序；`?include_archived=1` 含归档） |
| `/sessions/<id>/messages` | GET | 指定会话的历史消息（前端回显） |
| `/sessions/<id>/archive` | POST | 归档/取消归档：`{"archived": true\|false}` |
| `/sessions/<id>` | DELETE | 删除**已归档**会话（含消息与全文索引）；未归档 400，未知 404，进行中 409 |
| `/sessions/<id>` | PATCH | 设置会话标题：`{"title": "..."}`（空串清除） |
| `/sessions/<id>/fork` | POST | 复制会话历史开新会话（含系统提示词与全文索引）；未知 404，id 冲突 409 |
| `/skills` | GET | 技能列表（`skills.discover_skills()` 的 name + description） |
| `/plugins` | GET | 记忆 provider 插件列表（name + description + active） |
| `/tools` | GET | 可用工具列表（核心 TOOLS + provider 工具，name + description） |
| `/chat/stream` | POST | SSE 流式对话（event: activity/token/message/error/done） |
| `/chat` | POST | `{"message": "...", "session_id": "..."?}` → 返回 `{"reply": ...}` |
| `/sessions` | POST | 创建空会话（两步式第一步），返回 `{"session_id"}`（可传 `id`；默认服务端生成） |
| `/sessions/<id>/chat` | POST | 向指定会话发消息（推荐，两步式第二步） |
| `/sessions/<id>/chat/stream` | POST | 同上，SSE 流式（event: session/activity/token/message/done） |
| `/sessions/<id>/export` | GET | 导出会话 `?format=md\|html`（附件下载） |
| `/working_diff` | GET | 工作区 git 改动（`?mode=working\|staged\|all`、`?paths=`） |
| `/clarify/pending` | GET | `?session_id=xxx` → 模型中途提问的待回答项 |
| `/clarify/resolve` | POST | `{"session_id", "clarify_id"?, "answer"}` 回答澄清问题 |
| `/approvals/pending` | GET | `?session_id=xxx` → 当前待审批项 |
| `/approvals/resolve` | POST | `{"session_id", "choice": "once\|session\|always\|deny", "reason"?}` |
| `/health` | GET | 探活 |
| `/login` | GET | 登录页（`web/login.html`，公开） |
| `/api/auth/config` | GET | 登录可用性探测（公开，前端 401 时决定跳登录页还是弹 token 框） |
| `/api/auth/login` | POST | `{"username", "password"}` → 成功种 HttpOnly session cookie |
| `/api/auth/logout` | POST | 注销并清 cookie（无需已登录） |
| `/api/auth/me` | GET | 当前会话用户（需登录；返回 username + expires_at） |

审批流程：`/chat` 请求里的 agent 线程遇到危险命令会阻塞等待；客户端在另一个
连接轮询 `pending`、再 `POST resolve`，线程被唤醒后 `/chat` 返回最终结果。
中途提问（clarify）走同一套"门铃"流程：轮询 `/clarify/pending` 弹窗 →
`POST /clarify/resolve` 唤醒。Web 页面已内置两个流程：请求期间每 800ms 轮询，
弹窗点按钮即可。SSE 流式已实现；WebSocket 不在计划内。

可选环境变量：

| 变量 | 作用 | 默认值 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API Key（必填） | 无 |
| `DEEPSEEK_BASE_URL` | 换用其他 OpenAI 兼容接口 | `https://api.deepseek.com` |
| `MODEL` | 换模型（`deepseek-chat` 支持工具调用） | `deepseek-chat` |
| `CONTEXT_WINDOW` | 上下文窗口（token），压缩阈值 = 50% | `128000` |
| `PROTECT_LAST_N` | 压缩时保留最近多少条消息完整 | `20` |
| `APPROVAL_TIMEOUT` | 危险命令审批超时秒数（超时按拒绝处理） | `300` |
| `APPROVAL_MODE` | 审批模式：`manual` / `smart`（辅助 LLM 评估）/ `off`（旁路） | `manual` |
| `APPROVAL_SMART_POLICY` | 追加给辅助 LLM 的自定义策略（如"涉及 /etc 一律转人工"） | 空 |
| `APPROVAL_DENIAL_BREAKER` | 连续智能拒绝多少次后触发熔断（0 = 禁用） | `3` |
| `SESSION_RETENTION_DAYS` | 会话历史保留天数，启动时清理不活跃旧会话（0 = 禁用） | `90` |
| `MEMORY_NUDGE_INTERVAL` | 记忆 nudge 间隔（用户轮次）：每 N 轮后台审查一次（0 = 禁用） | `10` |
| `MAX_AGENT_TURNS` | 单次提问内"调模型"最大轮数（防无限调工具） | `5` |
| `TURN_TOKEN_BUDGET` | 单次提问累计 prompt token 预算（0 = 不限制；触顶请求模型收尾） | `0` |
| `TITLE_GENERATION_ENABLED` | LLM 自动生成会话标题开关（首轮交换后后台生成；false 关闭后不自动命名） | `true` |
| `LLM_MAX_RETRIES` | 调大模型失败时最多重试次数（429/5xx/超时/断连；0 = 不重试） | `3` |
| `APPROVAL_DENY` | 用户自定义拒绝规则（; 分隔的 fnmatch glob，命中即无条件拦截） | 空 |
| `TIRITH_ENABLED` | 内容级安全扫描开关（false 关闭） | `true` |
| `TIRITH_FAIL_OPEN` | 扫描器异常时放行（true）还是拦截（false） | `true` |
| `SERVER_AUTH_TOKEN` | HTTP 服务鉴权 Bearer token；设置后 API 需带 `Authorization: Bearer <token>`（留空 = 不鉴权） | 空 |
| `AUDIT_LOG_PATH` | 操作审计日志路径（JSON Lines，每请求一行；留空 = 关闭审计） | `audit.log` |
| `DASHBOARD_USERNAME` | 用户名密码登录的用户名（配了才启用登录页） | 空 |
| `DASHBOARD_PASSWORD_HASH` | scrypt 密码哈希（推荐；`python dashboard_auth.py hash-password <密码>` 生成） | 空 |
| `DASHBOARD_PASSWORD` | 明文密码备用（配了 HASH 时优先用 HASH） | 空 |
| `DASHBOARD_AUTH_SECRET` | 会话签名密钥（≥16 字节；不配则每次启动随机，重启后会话失效） | 进程内随机 |
| `DASHBOARD_SESSION_TTL_SECONDS` | 会话有效期秒数（下限 60s） | `43200`（12h） |
| `DASHBOARD_COOKIE_SECURE` | HTTPS 部署设 true，session cookie 加 Secure | `false` |
| `SERVER_LOG_PATH` | 服务化日志路径（结构化 JSON Lines + 大小轮转；相对项目根目录） | `logs/server.log` |
| `SERVER_LOG_MAX_MB` | 单个日志文件轮转阈值（MB） | `5` |
| `SERVER_LOG_BACKUP_COUNT` | 保留的轮转备份份数 | `3` |
| `MCP_SERVERS_PATH` | MCP 服务器配置路径（JSON；留空 = 禁用 MCP） | `mcp_servers.json` |

> 鉴权与审计：`SERVER_AUTH_TOKEN` 是生产密钥，只从环境变量注入；前端 401 时会弹出
> token 输入框并自动重试，token 只存浏览器 localStorage。审计日志与 Hermes 的
> observability / api_server 操作日志对齐（简化版），token 不打明文。

## 体验一个完整循环

```text
--- 第 1 轮：调用大模型 ---
  🔧 模型要调用工具：web_search({'query': '北京今天天气'})
  📦 工具返回：关键词：北京今天天气\n共 10 条结果：\n1. 北京天气…（截断）
--- 第 2 轮：调用大模型 ---
🤖 北京今天天气的搜索结果：……
```

这就是 Agent Loop 的全部：**调工具 → 结果回传 → 再问模型 → 得到最终回答**。

## 体验记忆（学习闭环）

```powershell
# 第一次：告诉它你的信息（模型会主动调用 memory 工具保存）
python minimal_agent.py "我叫小明，我喜欢喝美式咖啡"

# 第二次：换个问题，它能"想起"你
python minimal_agent.py "我是谁？我喜欢喝什么咖啡？"
```

打开 `USER.md` / `MEMORY.md` 可以看到记住的内容。这就是 Hermes "学习闭环"的最小原型：
**模型主动写入（memory 工具）→ 持久化 → 下次注入上下文 → 对话结束审查补漏**。

## 体验项目上下文（AGENTS.md）

在项目目录放一个 `AGENTS.md`（示例文件已附在脚本目录）：

```text
# Agent 骨架项目
定位：对齐 Hermes Agent 架构的通用 Agent 骨架，不限定业务领域
技术栈：Python + DeepSeek API
```

然后问相关问题，模型会基于项目上下文回答：

```powershell
python minimal_agent.py "我们项目是做什么的？"
```

> 说明：`AGENTS.md` 对齐 Hermes 的 context files（`AGENTS.md` / `.cursorrules`），
> 注入系统提示词的 context 层（stable → context → volatile）。
> 注意：Hermes 没有 `KNOWLEDGE.md` 这个概念；大知识按需召回在 Hermes 里由
> 外部 memory provider 插件（mem0/honcho 等）承担，我们曾用 KNOWLEDGE.md 模拟，
> 现已按你的选择替换为更贴近 Hermes 的 Context Files。

## 体验外部记忆 provider（插件）

```powershell
# 1. 激活 keyword 插件（环境变量，对齐 Hermes 的 memory.provider 配置）
$env:MEMORY_PROVIDER="keyword"

# 2. 在 providers/keyword/memory.json 里预置知识条目

# 3. 问相关问题，模型会用插件召回的记忆回答
python minimal_agent.py "这个项目是做什么的？"
```

运行时会看到「🔌 外部记忆 provider：keyword」和「🔌 外部记忆召回」两个面板，
对话结束后 `sync_turn` 会把本轮用户消息存档进 `memory.json`（学习闭环）。
想换真正的向量检索？新建 `providers/vector/` 目录实现同一个 `MemoryProvider` 接口即可，
主代码一行不用改。

provider 自带工具演示：

```powershell
$env:MEMORY_PROVIDER="keyword"
python minimal_agent.py "用 memory_search 工具搜索记忆库，看看有哪些记录"
```

模型会主动调用 `memory_search` 工具并基于返回结果回答——这就是
「模型按需检索记忆库」而不是等 prefetch 自动注入。

## 体验向量检索（providers/vector）

```powershell
$env:MEMORY_PROVIDER="vector"
# 默认零依赖：tfidf 词频向量（跑通管道，语义弱）
python minimal_agent.py "这个项目是做什么的？"
```

三种嵌入后端（`EMBEDDING_BACKEND`）：

| 后端 | 依赖 | 配置 | 语义能力 |
|---|---|---|---|
| `tfidf`（默认） | 无 | 无 | 弱（仍偏字面） |
| `local` | `pip install sentence-transformers`（含 torch，较大）+ 模型自动下载 | `EMBEDDING_BACKEND=local` | ✅ 真语义，离线 |
| `api` | 一个 OpenAI 兼容 embeddings 供应商 | 见下方 Qwen 示例 | ✅ 真语义 |

> **重要**：DeepSeek 官方 API 目前**没有 embeddings 接口**（社区在提 issue 但未提供），
> 所以语义向量检索需要额外来源。本项目已按你的选择默认配置 **Qwen `qwen3.7-text-embedding`**
> （阿里云百炼 DashScope，OpenAI 兼容）：

```powershell
# 方式 1：环境变量
$env:MEMORY_PROVIDER="vector"
$env:EMBEDDING_BACKEND="api"
$env:EMBEDDING_API_KEY="你的dashscope-key"

# 方式 2：复制 .env.example 为 .env 并填写（load_dotenv 自动加载）
```

默认值已内置：`EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`、
`EMBEDDING_MODEL=qwen3.7-text-embedding`；也兼容 `DASHSCOPE_API_KEY` 变量。
其他 OpenAI 兼容供应商（OpenAI / 硅基流动等）只需覆盖这三个变量即可。

```powershell
python minimal_agent.py "这个项目是做什么的？"
```

> 注册地址：[阿里云百炼](https://bailian.console.aliyun.com/)（DashScope），
> 创建 API-KEY 后填入即可。Key 只保存在本地 `.env`（已被 gitignore）。

## 体验历史会话检索（session_search）

```powershell
# 第一次：说一件以后要回忆的事（对话结束自动写入会话库 sessions.db）
python minimal_agent.py "帮我记一下：明天上午 10 点有个产品评审会"

# 第二次（新会话）：它不记得？不——它会自己搜索历史
python minimal_agent.py "我明天上午有什么安排？"
```

第二次运行时模型会主动调用 `session_search` 工具，搜到第一次的对话并带回上下文窗口，
然后基于检索结果回答。底层是 SQLite FTS5 全文索引 + BM25 相关性排序。

> 说明：Hermes 为中文加载了原生 CJK 分词扩展（bigram tokenizer）；
> 我们用 Python 侧把中文拆成相邻双字（2-gram）再进 FTS5，效果等价但零依赖。

## 体验多轮对话

```powershell
# 交互模式：连续问答，输入 退出 结束
python minimal_agent.py

# 一次性问题模式：答完即退出
python minimal_agent.py "北京天气怎么样"

# 恢复之前的会话继续聊
python minimal_agent.py --resume session-20260805-104459 "我们刚才聊到哪了？"
```

每次运行的会话 ID 会在启动时显示（`📚 会话：session-xxx`），结束时会提示如何恢复。
对话历史逐轮写入 `sessions.db`，中途退出也不丢失。

## 体验危险命令审批（terminal 工具）

```powershell
$env:PYTHONIOENCODING="utf-8"
python minimal_agent.py

# 交互里问它：帮我删除 build 目录
# 模型会调用 terminal("rm -rf build")，终端弹出审批面板：
#   选择：[o] 仅此一次  [s] 本会话允许  [a] 永久允许  [d] 拒绝
```

- `o` 仅此一次：本次放行，不记忆
- `s` 本会话允许：本次会话内同类模式不再询问（如其他 `rm -r`）
- `a` 永久允许：写入 `approval_allowlist.json`（已 gitignore，重启后仍生效）
- `d` 拒绝 / 超时：失败关闭，返回 `BLOCKED` 消息，命令不会执行

硬性禁止的命令（如 `rm -rf /`、`shutdown`）没有批准选项，直接拒绝执行。

> 说明：Windows 下 `terminal` 走系统默认 shell（cmd）。PowerShell 专属命令请显式写成
> `powershell -Command "..."`。裸写删除命令（`rmdir` / `rd` / `del` / `erase` /
> `Remove-Item` / `ri` / `rm`，锚定在命令起始）也已纳入危险清单——Hermes 只拦
> `cmd /c` 与 `powershell` 前缀形式，本骨架按用户要求补上了裸命令拦截（安全加固，
> 有意超出 Hermes 模式表）；`-EncodedCommand` 等混淆形式同样拦截。

## 跑回归测试

审批模块带一套零依赖回归测试（纯 Python 断言，无需 pytest）：

```powershell
python tests/test_approval.py
python tests/test_tool_dispatch.py
python tests/test_skills.py
python tests/test_file_tools.py
python tests/test_redact.py
python tests/test_memory_sync.py
python tests/test_session_prompt.py
python tests/test_session_cleanup.py
python tests/test_memory_nudge.py
python tests/test_skills_compression.py
python tests/test_approval_deny.py
python tests/test_tirith.py
python tests/test_gateway_approval.py
python tests/test_server.py
python tests/test_skills_preconditions.py
python tests/test_read_extract.py
python tests/test_todo_tool.py
```

覆盖危险/硬性模式检测、deny/session/always 审批分支、允许列表落盘重载、
terminal 工具的执行与拦截；并行批分段、路径重叠、并发真实发生与结果顺序回填；
Skills 的 frontmatter 解析、发现、索引、加载与路径安全；文件工具的分页读取、
敏感路径拒绝、搜索与真实工具名的路径重叠；脱敏的前缀密钥/赋值/JSON/YAML/
请求头/私钥/JWT 与 file_read 哨兵；patch 的唯一性/已应用检测/CRLF 保留。
记忆后台同步的异步/串行/合并节流/flush 超时；系统提示词的落库往返、
UPSERT 覆盖与压缩后重建；会话清理的旧会话删除/FTS 清理/保护/禁用。
记忆 nudge 的间隔触发、恢复水合与后台 worker 排空。
技能压缩的标记往返、幽灵技能收集与摘要后补回。
技能前置条件的嵌套 frontmatter 解析、条件激活过滤（requires/fallback）与
env/command readiness 检查。
文档抽取的扩展名判定、docx/xlsx/ipynb 抽取（共享字符串/隐藏表/分节）、
read_file 自动抽取集成与损坏回退。
todo 清单的写入/合并/校验/封顶、压缩重注入格式、历史水合配对校验、
run_tool 分发与并行顺序屏障。
用户 deny 规则的 glob 匹配、先于 allowlist/off 的优先级与返回结构。
tirith 的终端注入/隐形字符/同形字/管道检测与审批集成。
网关审批队列的阻塞/唤醒/FIFO/超时；HTTP 端点的 health/pending/resolve/chat。
Windows 控制台无需手动设编码，脚本会自动切换 UTF-8。

## 体验 Skills（按需加载）

```powershell
$env:PYTHONIOENCODING="utf-8"
python minimal_agent.py

# 问：项目发版前要检查什么？
# 模型会先 skills_list 看到 release-check，再 skill_view 加载检查清单回答
```

技能放在 `skills/<技能名>/SKILL.md`，头部 frontmatter：

```text
---
name: release-check
description: 项目发版前的检查清单与发布步骤（示例技能）。
platforms: [windows, linux, macos]
prerequisites:
  env_vars: [DEEPSEEK_API_KEY]
  commands: [git]
metadata:
  hermes:
    requires_tools: [terminal, web_search]
---
```

说明：索引只注入名称 + 描述，不占上下文；`skill_view` 可加载 SKILL.md 全文，
也可加载技能包内的 references/templates/scripts 等子文件；声明的 platforms
与当前系统不匹配的技能不会出现在索引里；`metadata.hermes.requires_tools` /
`fallback_for_tools` 声明了所需/兜底工具，当前工具集不满足时技能会从索引隐藏
（对齐 Hermes 的条件激活）。`skill_view` 加载时会检查前置条件：缺少
`prerequisites.env_vars` 里的环境变量（或新式 `required_environment_variables`）
返回 `setup_needed` 与缺失清单，`commands` 只做提示不阻塞。

## 体验文件工具

```powershell
$env:PYTHONIOENCODING="utf-8"
python minimal_agent.py

# 问：帮我把 build 目录下的文件整理成清单文件 build-list.txt
# 模型会用 search_files 找文件、write_file 写清单、read_file 回读确认
```

敏感保护示例（模型尝试写 `.env` 会被拒绝并返回提示）：

```text
Refusing to access sensitive path (项目安全文件): ...
Agent cannot read or modify security-sensitive files.
```

说明：相对路径按启动目录解析；`read_file` 带行号与分页（offset/limit），
二进制文件拒绝读取；`write_file` 自动建父目录并返回实际写入的绝对路径。
`read_file` 遇到 `.docx` / `.xlsx` / `.ipynb` 会自动抽取成文本再分页
（返回 `extracted_document=true`），模型可以直接"读一下这份文档/表格"。

patch 工具支持两种模式：

- `mode=replace`（默认）：找 `old_string` 换 `new_string`，找不到时自动走模糊匹配
  （容忍缩进/空白差异），仍找不到且目标文本已存在则判定"补丁已应用"返回 no-change
- `mode=patch`：V4A 补丁格式，一次批量操作多个文件——

```text
*** Begin Patch
*** Update File: src/app.py
@@ 上下文 @@
 def old():
-    return 1
+    return 2
*** Add File: src/new.py
+print("hello")
*** Delete File: old.txt
*** Move File: src/a.py -> src/b.py
*** End Patch
```

V4A 模式先全量校验（失败零写入）再应用；补丁头含 `..` 穿越或指向敏感文件
（.env 等）直接拒绝；应用前有简化版陈旧检测（文件被外部改动会失败并保留原内容）。

> 脱敏：读 `.env` 不再拒绝，而是把密钥打码——例如
> `DEEPSEEK_API_KEY=«redacted:sk-…»`；审批面板里的命令同样打码。

不想用 API Key？离线可视化演示（不需要 DeepSeek，直接看工具返回）：

```powershell
python demo_file_tools.py
```

会依次演示写文件、带行号读取、分页、搜索、以及写 `.env` 被拒绝，
临时目录自动清理。

## 体验任务规划（todo）

```powershell
$env:PYTHONIOENCODING="utf-8"
python minimal_agent.py

# 问：我要做一个发版，帮我列个执行计划并逐步推进
# 模型会调用 todo 工具写入任务清单（in_progress / pending），
# 每完成一步调用 todo 更新状态，你随时可以问"现在进行到哪了"
```

说明：任务清单按会话隔离（不同会话互不影响）；复杂任务拆解后模型会用它
跟踪进度；上下文压缩后未完成清单会自动保留（稳定头标记），不会丢失；
`--resume` 恢复会话时清单也会从历史里水合回来。REPL 里模型每次动清单都会
打印一个「📋 当前任务清单」面板，启动时也会先展示已有清单，方便你盯着进度。

## 体验智能审批（Smart Approval）

```powershell
$env:PYTHONIOENCODING="utf-8"
$env:APPROVAL_MODE="smart"
python minimal_agent.py

# 问：帮我清理一下 build 目录（模型会调 rm -rf build）
# 辅助 LLM 判定低风险 → 显示"智能审批：自动放行"，不再逐条问你
```

说明：smart 模式需要 API Key（辅助 LLM 和主模型共用 DeepSeek）；辅助 LLM 失败
或拿不准时自动"转人工"，不会静默放行；连续被判定危险会触发熔断警告。

## 体验工具并行执行

```powershell
$env:PYTHONIOENCODING="utf-8"
python minimal_agent.py

# 一次问多个独立的事，模型会在一轮里发多个工具调用：
# "查一下北京和上海的天气，再搜搜历史里有没有聊过产品评审会"
# 输出里会出现：⚡ 并行执行 3 个工具
```

并行只发生在「只读、无共享状态」的工具之间；写记忆、执行命令的工具始终按顺序
执行，保证 side effect 边界与全串行一致（对齐 Hermes 的分段规划器）。

## 体验 OpenAI 兼容接口

Open WebUI / LibreChat / LobeChat 等前端在设置里把 Base URL 指向
`http://localhost:8000/v1`、API Key 填任意值即可直连（服务端配了
`SERVER_AUTH_TOKEN` 时填该 token）。命令行冒烟（OpenAI SDK）：

```powershell
python server.py          # 默认 127.0.0.1:8000

python -c "from openai import OpenAI; c = OpenAI(base_url='http://127.0.0.1:8000/v1', api_key='x'); print(c.chat.completions.create(model='deepseek-chat', messages=[{'role': 'user', 'content': '你好'}]).choices[0].message.content)"
```

说明：骨架只认自己的工具/技能/记忆体系，请求里的 `tools` / `temperature` /
`max_tokens` 等 OpenAI 参数暂不生效（模型与工具仍走骨架配置）；`/v1/responses`
（OpenAI Responses API）未实现；`X-Hermes-Session-Id` 续接需要配置
`SERVER_AUTH_TOKEN`，默认推导会话无需任何配置即可用。流式下工具调用以标准
`delta.tool_calls` 帧输出，Open WebUI 能看到工具名与参数（参数已脱敏）。

## 体验 MCP（外部工具）

**例：接官方 filesystem 服务器**（需要 Node.js/npx）。先把 `mcp_servers.json`
写成这样：

```powershell
Copy-Item mcp_servers.example.json mcp_servers.json   # 或手写下面这份
```

```json
{
  "filesystem": {
    "command": "npx",
    "args": [
      "-y",
      "@modelcontextprotocol/server-filesystem",
      "C:/Users/Administrator/Documents/Codex/2026-08-03/ru/outputs/minimal_agent/mcp_demo_data"
    ],
    "timeout": 120,
    "connect_timeout": 60
  }
}
```

然后启动服务（REPL 或 HTTP 都行），工具会自动注册：

```powershell
python server.py
# 浏览器打开 http://127.0.0.1:8000，或直接验证：
#   GET http://127.0.0.1:8000/tools  → 能看到 mcp__filesystem__list_directory 等 14 个工具
```

实测结果（2026-08-12）：连接耗时约 5s（npx 首次下载包），注册 14 个工具；
`mcp__filesystem__list_directory` 返回 `[FILE] readme.txt`，
`mcp__filesystem__read_text_file` 能读出文件内容。对话里模型需要时就会调用
`mcp__<服务器>__<工具>`（与内置工具同一套调用/并行/审批体系，结果截断 + 脱敏
后回传）。没有 `mcp_servers.json` 时 MCP 自动跳过，不影响其他功能。

其他服务器同理：`command`/`args`/`env`（密钥用 `${VAR}` 插值注入，不写明文）、
可选 `timeout` / `connect_timeout` / `supports_parallel_tool_calls`。Windows 上
`npx` 这类命令实际是 `npx.cmd`，客户端已用 `shutil.which` 自动解析；配置 JSON
带 BOM 也能读（utf-8-sig）。复制 `mcp_servers.example.json` 可看到更多字段。

## 与 Hermes 源码的对应关系

| 本骨架 | Hermes 源码 |
|---|---|
| `MEMORY.md` / `USER.md` | `tools/memory_tool.py`（`ENTRY_DELIMITER`、`_path_for`） |
| `memory` 工具 add/replace/remove | `tools/memory_tool.py` 的 MemoryStore |
| 注入系统提示词 | `agent/system_prompt.py`（volatile 层） |
| 对话结束审查 | `agent/memory_manager.py` 的 `on_session_end()` / turn-end review |
| 字符上限（2200/1375） | `agent/agent_init.py` 的 `memory_char_limit` / `user_char_limit` |
| 占用率头部 `[% — chars]` | `tools/memory_tool.py` 的 `_render_block()` / `_success_response()` |
| 项目上下文 `load_context_files()` | `agent/prompt_builder.py` 的 `build_context_files_prompt()` + `_truncate_content()` |
| 历史检索 `session_search_tool()` | `tools/session_search_tool.py`（FTS5 + BM25 + 锚定窗口 `get_anchored_view`） |
| 多轮会话 / 恢复 | `cli.py` 会话循环 + `run_conversation(conversation_history=...)` + `/resume` |
| 增量落库 `persist_messages(start=...)` | `agent/conversation_loop.py` 逐轮写入 SessionDB |
| 上下文压缩 `context_compressor.py` | `agent/context_compressor.py`（阈值 50%、protect_last_n、交接摘要） |
| `MemoryProvider` 抽象 | `agent/memory_provider.py`（ABC：prefetch / sync_turn / system_prompt_block） |
| `MemoryManager` 编排 | `agent/memory_manager.py`（prefetch_all / sync_all / build_system_prompt） |
| 插件加载 `load_provider()` | `plugins/memory/__init__.py`（`load_memory_provider`） |
| provider 自带工具 | `memory_manager.py` 的 `get_all_tool_schemas()` / `handle_tool_call()`（mem0 的 `mem0_search` 同款） |
| 记忆异步同步 `memory_manager.py` | `agent/memory_manager.py`（sync_all 后台 worker + flush_pending） |
| 系统提示词持久化 + 压缩重建 | `agent/conversation_loop.py`（update_system_prompt / _restore_or_build_system_prompt） |
| 压缩边界记忆提交 `commit_memory_session` | `run_agent.py` 的 commit_memory_session（压缩前同步提取） |
| 会话历史清理 `prune_sessions()` | `hermes_state.py` 的 SessionDB.prune_sessions（older_than_days） |
| 记忆 nudge `MEMORY_NUDGE_INTERVAL` | `agent/turn_context.py`（_turns_since_memory 计数 + 恢复水合）+ `agent/agent_init.py`（nudge_interval） |
| 技能压缩 prune/reinject | `agent/context_compressor.py`（_skill_pruned_marker / _collect_ghosted_skill_names / _reinject_pruned_skill_markers） |
| 用户 deny 规则 `APPROVAL_DENY` | `tools/approval.py`（_match_user_deny_rule / _user_deny_block_result，approvals.deny） |
| 内容级扫描 `tirith.py` | `tools/tirith_security.py`（check_command_security，Python 简化版） |
| 服务化 `server.py` + 网关审批 | `gateway/run.py`（register_gateway_notify / resolve_gateway_approval）+ `gateway/platforms/api_server.py` |
| 服务鉴权 `SERVER_AUTH_TOKEN` + Bearer 校验 | `plugins/dashboard_auth/`（self_hosted 思路简化：单静态 token，无 OIDC） |
| 操作审计 `audit.log`（JSON Lines） | Hermes observability / `api_server` 操作日志（简化版） |
| 用户名密码登录 `dashboard_auth.py` + session cookie | `plugins/dashboard_auth/basic/`（scrypt 哈希 + HMAC 无状态会话，无 refresh 轮换） |
| 会话删除 `DELETE /sessions/<id>` | `gateway/platforms/api_server.py` 的 `_handle_delete_session` + `hermes_state.py` 的 `SessionDB.delete_session` |
| 会话标题 `PATCH /sessions/<id>` | `gateway/platforms/api_server.py` 的 `_handle_patch_session` |
| 会话 fork `POST /sessions/<id>/fork` | `gateway/platforms/api_server.py` 的 `_handle_fork_session`（简化：无 parent 血缘列） |
| 联网 `web_search` / `web_fetch` | `plugins/web/`（tavily/searxng 等思路简化：必应 RSS 优先 + DuckDuckGo 兜底，urllib 零依赖无 key） |
| 时间工具 `get_current_time` + `stdin=DEVNULL` | Hermes 无直接对应（骨架修复 Windows cmd 交互式 date/time 卡死问题） |
| 脱敏专项（DB 连接串/手机号/URL 查询参数） | `agent/redact.py` 的 `_DB_CONNSTR_RE` / `_SIGNAL_PHONE_RE` / `_redact_url_query_params` |
| 并行执行中断语义 | `agent/tool_executor.py`（interrupt 预检 + 等待轮询 + cancel + 3s grace + 进程树终止） |
| turn 级预算 `MAX_AGENT_TURNS` / `TURN_TOKEN_BUDGET` | `agent/agent_init.py` 的 `max_iterations` / `agent/iteration_budget.py` + `chat_completion_helpers.py` 的 `handle_max_iterations` |
| 危险命令审批 `approval.py` | `tools/approval.py`（DANGEROUS_PATTERNS、HARDLINE_PATTERNS、prompt_dangerous_approval） |
| `terminal` 工具（先审批再执行） | `tools/terminal_tool.py`（check_all_command_guards + subprocess） |
| 永久允许列表 `approval_allowlist.json` | `config.yaml` 的 `command_allowlist`（JSON 免去 YAML 依赖） |
| 工具并行执行 `tool_dispatch.py` | `agent/tool_dispatch_helpers.py`（_plan_tool_batch_segments）+ `agent/tool_executor.py`（execute_tool_calls_segmented） |
| todo 工具 `todo_tool.py` | `tools/todo_tool.py`（TodoStore / todo_tool / TODO_SCHEMA / format_for_injection / _hydrate_todo_store） |
| Skills `skills.py` + `skills/` 目录 | `agent/skill_utils.py`（发现/frontmatter）+ `tools/skills_tool.py`（skills_list/skill_view）+ `agent/prompt_builder.py`（技能索引） |
| Skills 前置条件检查 | `agent/skill_utils.py` 的 `extract_skill_conditions` + `agent/prompt_builder.py` 的 `_skill_should_show` + `tools/skills_tool.py` 的 `_get_required_environment_variables`（env 缺失 → setup_needed；commands 仅 advisory） |
| 文件工具 `file_tools.py` | `tools/file_tools.py`（read_file_tool / write_file_tool / _check_sensitive_path） |
| 文档抽取 `read_extract.py` | `tools/read_extract.py`（EXTRACTABLE_EXTENSIONS / extract_document_text，docx+xlsx 用 zipfile+XML、ipynb 用 JSON，零依赖） |
| 敏感脱敏 `redact.py` | `agent/redact.py`（redact_sensitive_text / mask_secret / file_read 哨兵） |
| patch 工具（replace + V4A 双模式） | `tools/file_tools.py` 的 patch_tool（mode=replace\|patch）+ `tools/patch_parser.py`（parse_v4a_patch / apply_v4a_operations）+ `tools/fuzzy_match.py`（fuzzy_find_and_replace） |
| 审批增强（smart/熔断/混淆检测） | `tools/approval.py`（_smart_approve / _record_denial / DANGEROUS_PATTERNS） |
| 记忆异步同步 `memory_manager.py` | `agent/memory_manager.py`（sync_all 后台 worker + flush_pending） |
| LLM 自动标题 `title_generator.py` | `agent/title_generator.py`（maybe_auto_title / generate_title / set_auto_title_if_empty） |
| 终端输出清洗 `ansi_strip.py` + `tool_output_limits.py` | `tools/ansi_strip.py`（strip_ansi）+ `tools/tool_output_limits.py`（get_max_bytes）+ `tools/terminal_tool.py`（截断→剥 ANSI→脱敏） |
| working_diff `working_diff.py` | `tools/working_diff.py`（collect_working_diff，/diff 三模式） |
| 网页工作区改动视图 `GET /working_diff` + 侧栏【工作区】 | Hermes gateway 的 `/diff` 入口（CLI 与 gateway 共用同一收集逻辑） |
| LLM 调用重试 `retry_utils.py` | `agent/retry_utils.py`（jittered_backoff）+ `chat_completion_helpers.py` 的重试循环（简化版） |
| REPL 斜杠命令 `/help` `/sessions` `/resume` `/diff` | Hermes CLI 的 slash 体系（`hermes_cli/commands.py`）+ `tools/working_diff.py` 的 `/diff`（骨架简化：仅 4+1 个命令） |
| 后台进程 `process_registry.py` + `process` 工具 | `tools/process_registry.py`（spawn/poll/wait/kill，滚动 200KB 缓冲；骨架无检查点/TTL/notify） |
| 会话导出 `session_export.py` + `/export` | `hermes_cli/session_export_md.py` + `session_export_html.py`（骨架简化：无 SHA256 校验/分段/tool_calls） |
| 中途问用户 `clarify.py` | `tools/clarify_tool.py`（schema/选项清洗/多选）+ `tools/clarify_gateway.py`（阻塞事件队列 + 超时） |
| 两步式会话 API `POST /sessions` + `/sessions/<id>/chat` | `gateway/platforms/api_server.py`（POST /api/sessions 建会话 + /api/sessions/{id}/chat；客户端可传 id，默认服务端生成 `api_时间戳_uuid`） |
| OpenAI 兼容 `GET /v1/models` + `POST /v1/chat/completions` | `gateway/platforms/api_server.py`（`_handle_chat_completions` / `_write_sse_chat_completion` / `_derive_chat_session_id` / `_openai_error` / `_normalize_chat_content`；骨架简化：单模型无 model_routes、`/v1/responses` 与 `X-Hermes-Session-Key` 未做） |
| 服务化日志 `server_logging.py` | `hermes_logging.py`（setup_logging / RotatingFileHandler / RedactingFormatter / set_session_context；骨架简化：JSON Lines 单文件、单进程用标准库轮转） |
| 手动启动 `server_ctl.ps1` | Hermes systemd / gateway daemon（Windows 手动模式：后台启动 + PID + 探活 + 进程树停止；Task Scheduler / Windows 服务未做，留待长期驻留需要） |
| MCP 客户端 `mcp_client.py` | `tools/mcp_tool.py` + `hermes_cli/mcp_config.py`（stdio JSON-RPC 2.0：initialize/tools/list/tools/call、`mcp__` 前缀命名、安全环境过滤、`${VAR}` 插值、supports_parallel_tool_calls；骨架简化：仅 stdio、无自动重连/sampling/HTTP-SSE） |

骨架当前简化掉（或有意不做）的工业级细节：文件锁、注入威胁扫描、外部漂移检测、
会话压缩后的 lineage 去重（压缩黑洞处理）、Skills 的 hub/组织同步/插件命名空间、
文件工具的跨 profile/文件锁、多外部 memory provider 同时挂载、cron 审批
（用户取消）、MCP/ACP（已列入待办模块，见 HANDOFF）——这些是后续深入源码时
值得关注的点。已对齐的近期能力（LLM 自动标题/终端输出清洗/working_diff/LLM 重试/
REPL 斜杠命令/后台终端/会话导出/clarify/两步式会话 API/OpenAI 兼容接口）见上面对应关系表。

## 加新工具

在 `TOOLS` 列表里加一段描述，再在 `run_tool()` 里加一行，即可让模型使用新工具。

## 下一步可以加什么

- 已完成（1~49）：Agent Loop/工具/三层记忆/压缩/审批/并行/技能/文件工具/脱敏/
  服务化+前端/鉴权审计登录/会话删除标题 fork/联网/时间工具/turn budget/中断语义/
  LLM 自动标题/终端输出清洗/working_diff+网页工作区视图/LLM 重试/REPL 斜杠命令/
  后台终端/会话导出/clarify 中途问用户/两步式会话 API/OpenAI 兼容接口/
  服务化日志+手动启动/MCP 客户端
  （详见 README 功能列表）
- 待办（详见 HANDOFF"待办模块"）：多代理/委派 → ACP → 大结果落盘/网站策略/
  澄清增强等小件（运维剩余 Task Scheduler / Windows 服务可选）
- 明确暂不做：cron 审批（用户取消）、文件锁/跨 profile、Skills hub 同步、
  多外部 memory provider 同时挂载
