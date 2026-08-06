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
    parallel / sequential 段——只读工具（get_weather / session_search / 记忆检索）并发跑，
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
    userinfo 全部打码；`read_file` 读敏感文件改为"打码后读取"（不可复用哨兵
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
    同一 origin 免跨域；布局参考 Codex 首页：左侧毛玻璃侧栏（品牌/能力清单）+
    顶部标题栏 + 首页 hero/四张建议卡片 + 底部毛玻璃输入框，首条消息后切到会话线程：
    - 对话：`POST /chat` 发消息、渲染 Markdown 子集（代码块/行内代码/加粗/
      斜体/列表，先转义再包标签防 XSS）、建议卡片一键发送、会话 ID 自动生成并
      持久化到 localStorage、可粘贴旧会话 ID 恢复、新对话按钮
    - 审批弹窗：/chat 阻塞期间每 800ms 轮询 `GET /approvals/pending`，展示命令与
      原因（服务端已脱敏），按钮 允许一次/本会话允许/永久允许/拒绝（拒绝可填理由）；
      smart deny 场景按网关数据的 `allow_permanent=false` 自动隐藏"永久允许"
      （对齐 Hermes api_server 的 `_approval_event_choices`）
    - 连接状态：`/health` 探活指示器；请求失败/服务离线有明确提示
    - 品牌：通用 Agent（不限定业务领域，对齐 Hermes 的通用助手形态）
    - 服务端配套：`list_pending_approvals` 暴露 `allow_permanent`；同一会话的
      /chat 加串行锁（对齐 Hermes turn lease，防并发请求竞争 messages 状态）

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

## 运行

PowerShell 里执行：

```powershell
# 1. 设置 API Key
$env:DEEPSEEK_API_KEY="sk-你的key"

# 2. 运行（两种方式任选）
python minimal_agent.py "北京天气怎么样"
# 或直接运行，然后输入问题
python minimal_agent.py
```

如果中文显示乱码，先执行：

```powershell
$env:PYTHONIOENCODING="utf-8"
```

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
| `/chat` | POST | `{"message": "...", "session_id": "..."?}` → 返回 `{"reply": ...}` |
| `/approvals/pending` | GET | `?session_id=xxx` → 当前待审批项 |
| `/approvals/resolve` | POST | `{"session_id", "choice": "once\|session\|always\|deny", "reason"?}` |
| `/health` | GET | 探活 |

审批流程：`/chat` 请求里的 agent 线程遇到危险命令会阻塞等待；客户端在另一个
连接轮询 `pending`、再 `POST resolve`，线程被唤醒后 `/chat` 返回最终结果。
Web 页面已内置该流程：请求期间每 800ms 轮询，弹出审批框点按钮即可。
（流式/SSE 与 WebSocket 是二期。）

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
| `APPROVAL_DENY` | 用户自定义拒绝规则（; 分隔的 fnmatch glob，命中即无条件拦截） | 空 |
| `TIRITH_ENABLED` | 内容级安全扫描开关（false 关闭） | `true` |
| `TIRITH_FAIL_OPEN` | 扫描器异常时放行（true）还是拦截（false） | `true` |

## 体验一个完整循环

```text
--- 第 1 轮：调用大模型 ---
  🔧 模型要调用工具：get_weather({'city': '北京'})
  📦 工具返回：晴，25°C
--- 第 2 轮：调用大模型 ---
🤖 北京今天晴，气温 25°C。
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
> `powershell -Command "..."`——这正是审批模式覆盖的形态（`Remove-Item`、
> `-EncodedCommand` 等单独裸写不会被当作危险命令，与 Hermes 行为一致）。

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
---
```

说明：索引只注入名称 + 描述，不占上下文；`skill_view` 可加载 SKILL.md 全文，
也可加载技能包内的 references/templates/scripts 等子文件；声明的 platforms
与当前系统不匹配的技能不会出现在索引里。

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

> 脱敏：读 `.env` 不再拒绝，而是把密钥打码——例如
> `DEEPSEEK_API_KEY=«redacted:sk-…»`；审批面板里的命令同样打码。

不想用 API Key？离线可视化演示（不需要 DeepSeek，直接看工具返回）：

```powershell
python demo_file_tools.py
```

会依次演示写文件、带行号读取、分页、搜索、以及写 `.env` 被拒绝，
临时目录自动清理。

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
| 危险命令审批 `approval.py` | `tools/approval.py`（DANGEROUS_PATTERNS、HARDLINE_PATTERNS、prompt_dangerous_approval） |
| `terminal` 工具（先审批再执行） | `tools/terminal_tool.py`（check_all_command_guards + subprocess） |
| 永久允许列表 `approval_allowlist.json` | `config.yaml` 的 `command_allowlist`（JSON 免去 YAML 依赖） |
| 工具并行执行 `tool_dispatch.py` | `agent/tool_dispatch_helpers.py`（_plan_tool_batch_segments）+ `agent/tool_executor.py`（execute_tool_calls_segmented） |
| Skills `skills.py` + `skills/` 目录 | `agent/skill_utils.py`（发现/frontmatter）+ `tools/skills_tool.py`（skills_list/skill_view）+ `agent/prompt_builder.py`（技能索引） |
| 文件工具 `file_tools.py` | `tools/file_tools.py`（read_file_tool / write_file_tool / _check_sensitive_path） |
| 敏感脱敏 `redact.py` | `agent/redact.py`（redact_sensitive_text / mask_secret / file_read 哨兵） |
| patch 工具（replace 模式） | `tools/file_tools.py` 的 patch_tool + `tools/file_operations.py` 的 patch_replace |
| 审批增强（smart/熔断/混淆检测） | `tools/approval.py`（_smart_approve / _record_denial / DANGEROUS_PATTERNS） |
| 记忆异步同步 `memory_manager.py` | `agent/memory_manager.py`（sync_all 后台 worker + flush_pending） |

骨架简化掉了的工业级细节：文件锁、注入威胁扫描、外部漂移检测、可插拔 MemoryProvider、
会话压缩后的 lineage 去重（压缩黑洞处理）、记忆主动 nudge、审批的 cron/gateway 上下文、
Smart Approval（辅助 LLM 审批）与连续拒绝熔断、命令混淆检测、工具并行里的中断语义与
turn 级 budget 收尾、Skills 的 hub/组织同步/插件命名空间/前置条件检查、压缩时的技能
prune/reinject、文件工具的跨 profile/陈旧检测/文档抽取、patch 的 V4A 补丁头/
模糊匹配/语法检查、脱敏的 URL 查询参数/手机号/DB 连接串专项——这些是后续深入源码时值得关注的点。

## 加新工具

在 `TOOLS` 列表里加一段描述，再在 `run_tool()` 里加一行，即可让模型使用新工具。

## 下一步可以加什么

- 服务增强：SSE 流式响应 / 请求鉴权（token）/ 会话列表管理
- 审批剩余：cron 审批上下文（`approvals.cron_mode`）
- 并行执行的中断语义与 turn 级 budget 收尾（Hermes executor 有，骨架简化掉了）
