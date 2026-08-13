"use strict";
/* Agent Web 前端：连 server.py 的 /chat + 审批轮询/resolve。
   布局参考 Codex 首页：左侧毛玻璃侧栏 + 顶部标题栏 + hero/建议卡片 + 底部输入框。
   与 Hermes dashboard 的交互契约对齐：/chat 阻塞等待审批，前端轮询
   /approvals/pending 发现待审批项，POST /approvals/resolve 后线程被唤醒。 */

const API = {
  chat: "/chat",
  chatStream: "/chat/stream",
  pending: "/approvals/pending",
  resolve: "/approvals/resolve",
  clarifyPending: "/clarify/pending",
  clarifyResolve: "/clarify/resolve",
  sessions: "/sessions",
  createSession: "/sessions",
  chatSession: (id) => "/sessions/" + encodeURIComponent(id) + "/chat",
  chatSessionStream: (id) => "/sessions/" + encodeURIComponent(id) + "/chat/stream",
  plugins: "/plugins",
  mcp: "/mcp",
  skills: "/skills",
  tools: "/tools",
  workingDiff: "/working_diff",
  authConfig: "/api/auth/config",
  authMe: "/api/auth/me",
  authLogout: "/api/auth/logout",
  sessionsMessages: (id) => "/sessions/" + encodeURIComponent(id) + "/messages",
  sessionsArchive: (id) => "/sessions/" + encodeURIComponent(id) + "/archive",
  sessionExport: (id) => "/sessions/" + encodeURIComponent(id) + "/export?format=md",
  sessionDelete: (id) => "/sessions/" + encodeURIComponent(id),
  sessionFork: (id) => "/sessions/" + encodeURIComponent(id) + "/fork",
};

const STORAGE_KEY = "agent.session";
const AUTH_TOKEN_KEY = "agent.auth.token";
const POLL_INTERVAL_MS = 800;

const PLUGIN_TAB_META = {
  mcp: {
    title: "MCP 服务器",
    desc: "通过 Model Context Protocol 接入的外部工具服务器。",
  },
  plugins: {
    title: "记忆插件",
    desc: "已启用的记忆 provider 插件。",
  },
  skills: {
    title: "技能",
    desc: "按需加载的 SKILL 技能包。",
  },
  tools: {
    title: "工具",
    desc: "当前 Agent 可调用的内置与扩展工具。",
  },
};

const state = {
  sessionId: "",
  sessionTitle: "",
  currentView: "",
  inFlight: false,
  pollTimer: null,
  abort: null,
  queueCount: 0,
  pendingItem: null,
  pendingKey: "",
  clarifyItem: null,
  hasMessages: false,
  loginAvailable: false,
};

const $ = (id) => document.getElementById(id);
const chatEl = $("chat");
const homeEl = $("home");
const inputEl = $("input");
const sendBtn = $("btn-send");
const viewTitle = $("view-title");
const conversationPane = $("conversation-pane");

/* 工作区视图注册表：已归档 / 插件（MCP+记忆插件+技能+工具合并）/ 工作区改动 */
const VIEWS = {
  archived: {
    viewId: "archived-view",
    label: "已归档",
    load: refreshArchivedList,
  },
  plugins: {
    viewId: "plugins-view",
    label: "插件",
    load: refreshPluginsList,
  },
  wdiff: {
    viewId: "working-diff-view",
    label: "工作区改动",
    load: refreshWorkingDiff,
  },
};
const NAV_BUTTONS = {
  archived: "btn-archived",
  plugins: "btn-plugins",
  wdiff: "btn-working-diff",
};

/* ---------- 工具函数 ---------- */

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

/* 极简 Markdown 渲染（先整体转义再包标签，杜绝 XSS）。
   支持：```代码块```、`行内代码`、**加粗**、*斜体*、列表、段落。 */
function renderMarkdown(text) {
  const safe = escapeHtml(text);
  const lines = safe.split("\n");
  const out = [];
  let inFence = false;
  let fenceBuf = [];
  let listBuf = [];

  const inline = (s) =>
    s
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");

  const flushList = () => {
    if (listBuf.length) {
      out.push("<ul>" + listBuf.join("") + "</ul>");
      listBuf = [];
    }
  };

  const closeFence = () => {
    out.push("<pre><code>" + fenceBuf.join("\n") + "</code></pre>");
    fenceBuf = [];
  };

  for (const line of lines) {
    if (/^```/.test(line)) {
      flushList();
      if (inFence) {
        closeFence();
        inFence = false;
      } else {
        inFence = true;
      }
      continue;
    }
    if (inFence) {
      fenceBuf.push(line);
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      listBuf.push("<li>" + inline(line.replace(/^[-*]\s+/, "")) + "</li>");
      continue;
    }
    if (/^\d+\.\s+/.test(line)) {
      listBuf.push("<li>" + inline(line.replace(/^\d+\.\s+/, "")) + "</li>");
      continue;
    }
    flushList();
    if (line.trim() === "") {
      continue;
    }
    if (/^#{1,3}\s/.test(line)) {
      out.push("<p><strong>" + inline(line.replace(/^#{1,3}\s/, "")) + "</strong></p>");
    } else {
      out.push("<p>" + inline(line) + "</p>");
    }
  }
  flushList();
  if (inFence) closeFence();
  return out.join("");
}

/* ---------- 视图切换（首页 ↔ 会话线程） ---------- */

function updateHeaderActions() {
  const btn = $("btn-export-session");
  if (btn) btn.classList.toggle("hidden", !state.sessionId || !state.hasMessages);
}

function showThread() {
  homeEl.classList.add("hidden");
  chatEl.classList.remove("hidden");
  conversationPane.classList.remove("hidden");
  conversationPane.classList.add("grow");
  conversationPane.classList.remove("fill");
  hideAllViews();
  viewTitle.textContent = state.sessionTitle || "会话";
  state.hasMessages = true;
  updateHeaderActions();
}

function showHome() {
  homeEl.classList.remove("hidden");
  chatEl.classList.add("hidden");
  conversationPane.classList.remove("hidden");
  conversationPane.classList.remove("grow");
  conversationPane.classList.add("fill");
  hideAllViews();
  viewTitle.textContent = "新对话";
  state.hasMessages = false;
  updateHeaderActions();
}

function fallbackCopyText(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (e) {
    ok = false;
  }
  ta.remove();
  return ok;
}

async function copyMessageText(messageEl) {
  const text = (messageEl.textContent || "").trim();
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    if (!fallbackCopyText(text)) {
      appendMessage("error", "复制失败：" + e.message);
    }
  }
}

function addMessageActions(messageEl, row) {
  const actions = document.createElement("div");
  actions.className = "msg-actions";

  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "icon-btn";
  copyBtn.title = "复制";
  copyBtn.setAttribute("aria-label", "复制消息");
  copyBtn.innerHTML =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>' +
    '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>' +
    "</svg>";
  copyBtn.addEventListener("click", () => copyMessageText(messageEl));

  const forkBtn = document.createElement("button");
  forkBtn.type = "button";
  forkBtn.className = "icon-btn";
  forkBtn.title = "分支";
  forkBtn.setAttribute("aria-label", "分支当前会话");
  forkBtn.innerHTML =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/>' +
    '<circle cx="18" cy="6" r="2"/><path d="M6 8v8"/>' +
    '<path d="M18 8a6 6 0 0 1-6 6H6"/>' +
    "</svg>";
  forkBtn.addEventListener("click", () => {
    if (state.sessionId) forkSession(state.sessionId);
  });

  actions.append(copyBtn, forkBtn);
  row.appendChild(actions);
}

function appendMessage(role, text) {
  if (!state.hasMessages) showThread();
  const row = document.createElement("div");
  row.className = "msg-row";
  const div = document.createElement("div");
  div.className = "msg " + role;
  if (role === "assistant") {
    div.innerHTML = renderMarkdown(text);
  } else {
    div.textContent = text;
  }
  row.appendChild(div);
  if (role === "assistant") {
    addMessageActions(div, row);
  }
  chatEl.appendChild(row);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

const activityById = {};
// 当前助手轮的过程托盘：Codex 式——过程展开显示 + 已耗时计时，回答出来后收拢
let currentTray = null;
// 回合令牌：切换会话/新对话时自增，使在途旧轮次全部失效（防止跨会话泄漏）
let turnToken = 0;

function fmtElapsed(ms) {
  return "已耗时 " + (ms / 1000).toFixed(1) + "s";
}

function trayDoneText(durationMs) {
  const time = durationMs ? " · 耗时 " + (durationMs / 1000).toFixed(1) + "s" : "";
  return "已处理" + time;
}

function beginActivityTray() {
  const token = turnToken;
  // 事件 id 每轮从 0 重新编号：清掉上一轮的映射，避免新活动更新到旧条目上
  Object.keys(activityById).forEach((k) => delete activityById[k]);
  const events = [];
  const tray = document.createElement("div");
  tray.className = "activity-tray";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "activity-tray-head";
  const body = document.createElement("div");
  body.className = "activity-tray-body";
  tray.append(head, body);
  const started = performance.now();
  let finalized = false;
  let durationMs = 0;
  const renderHead = () => {
    if (finalized) {
      head.textContent = tray.classList.contains("collapsed")
        ? trayDoneText(durationMs)
        : fmtElapsed(durationMs);
      return;
    }
    head.textContent = fmtElapsed(performance.now() - started);
  };
  head.addEventListener("click", () => {
    tray.classList.toggle("collapsed");
    renderHead();
  });
  const timer = setInterval(() => {
    if (!tray.classList.contains("collapsed") && !finalized) renderHead();
  }, 100);
  chatEl.appendChild(tray);
  // 思考提示放在活动托盘下方、助手消息的位置，而不是托盘上方
  const thinking = chatEl.querySelector(".msg.thinking");
  if (thinking) chatEl.insertBefore(thinking, tray.nextSibling);
  chatEl.scrollTop = chatEl.scrollHeight;
  const markFinalized = (ms) => {
    finalized = true;
    durationMs = ms;
    renderHead();
  };
  const handle = { tray, body, events, head, timer, started, token, markFinalized };
  currentTray = handle;
  renderHead();
  return handle;
}

function finalizeTray(tray) {
  if (!tray) return;
  clearInterval(tray.timer);
  if (tray.events.length === 0) {
    tray.tray.remove();  // 本轮没有活动，不显示空盒子
  } else {
    tray.tray.classList.add("collapsed");  // 最终回答出来后收拢成一行时间
    tray.markFinalized(performance.now() - tray.started);
  }
  if (currentTray === tray) currentTray = null;
}

function buildActivityText(ev) {
  const type = ev.type || "tool";
  if (type === "note") {
    return ev.result || "";
  }
  if (type === "think") {
    return ev.result || "思考";
  }
  // tool / skill / source：参数 + 结果（名称由条目头展示）
  const lines = [];
  if (ev.args) lines.push("参数：" + ev.args);
  if (ev.result) lines.push("结果：" + ev.result);
  return lines.join("\n");
}

function buildActivityItem(ev) {
  const type = ev.type || "tool";
  // 没有推理内容的思考不展示（空占位无意义）
  if (type === "think" && !(ev.result || "").trim()) return null;
  if (type === "think" || type === "note") {
    const item = document.createElement("div");
    item.className = "activity";
    item.textContent = buildActivityText(ev);
    return { item };
  }
  // tool / skill / source：各自独立展开/收起参数与结果
  const wrap = document.createElement("div");
  wrap.className = "activity activity-tool";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "activity-tool-head";
  const detail = document.createElement("div");
  detail.className = "activity-tool-detail hidden";  // 默认收拢
  detail.textContent = buildActivityText(ev);
  const render = () => {
    const open = !detail.classList.contains("hidden");
    head.textContent = (open ? "▾ " : "▸ ") + (ev.name || type);
  };
  head.addEventListener("click", () => {
    detail.classList.toggle("hidden");
    render();
  });
  wrap.append(head, detail);
  render();
  return { item: wrap, detail };
}

function renderActivity(ev, tray) {
  // 过期/跨会话事件直接丢弃：托盘令牌必须等于当前回合令牌
  if (!tray || tray.token !== turnToken) return;
  const existing = ev.id !== undefined ? activityById[ev.id] : null;
  if (existing) {
    if (existing.detail) {
      existing.detail.textContent = buildActivityText(ev);
    } else {
      existing.item.textContent = buildActivityText(ev);
    }
    return;
  }
  const built = buildActivityItem(ev);
  if (!built) return;  // 空思考等无内容事件不展示
  tray.body.appendChild(built.item);
  tray.events.push(ev);
  chatEl.scrollTop = chatEl.scrollHeight;
  if (ev.id !== undefined) {
    activityById[ev.id] = { item: built.item, detail: built.detail || null };
  }
}

function renderReplayTray(events) {
  // 历史重放：还原成收拢状态的过程托盘（已耗时 Xs，点击展开）
  if (!events || !events.length) return null;
  const tray = document.createElement("div");
  tray.className = "activity-tray collapsed";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "activity-tray-head";
  const body = document.createElement("div");
  body.className = "activity-tray-body";
  tray.append(head, body);
  const refresh = () => {
    const collapsed = tray.classList.contains("collapsed");
    const duration = events[0] && events[0].duration_ms;
    head.textContent = collapsed
      ? trayDoneText(duration)
      : (duration ? fmtElapsed(duration) : "已处理");
  };
  head.addEventListener("click", () => {
    tray.classList.toggle("collapsed");
    refresh();
  });
  events.forEach((ev) => {
    if (ev.type === "todo") return;  // todo 事件走常驻清单卡片，不进托盘
    const built = buildActivityItem(ev);
    if (built) body.appendChild(built.item);
  });
  refresh();
  chatEl.appendChild(tray);
  return tray;
}

function setThinking(on) {
  if (on) {
    if (!chatEl.querySelector(".msg.thinking")) {
      const div = document.createElement("div");
      div.className = "msg thinking";
      div.textContent = "【正在思考】";
      chatEl.appendChild(div);
      chatEl.scrollTop = chatEl.scrollHeight;
    }
  } else {
    const el = chatEl.querySelector(".msg.thinking");
    if (el) el.remove();
  }
}

const todoPanel = $("todo-panel");
const TODO_MARKS = { pending: "[ ]", in_progress: "[>]", completed: "[x]", cancelled: "[~]" };

function renderTodoPanel(todos) {
  todoPanel.textContent = "";
  if (!todos || !todos.length) {
    todoPanel.classList.add("hidden");
    return;
  }
  const head = document.createElement("div");
  head.className = "todo-panel-head";
  head.textContent = "📋 任务清单";
  const body = document.createElement("div");
  body.className = "todo-panel-body";
  todos.forEach((t) => {
    const row = document.createElement("div");
    row.className = "todo-item";
    const mark = document.createElement("span");
    mark.className = "todo-mark";
    mark.textContent = TODO_MARKS[t.status] || "[?]";
    const label = document.createElement("span");
    label.textContent = t.content;
    row.append(mark, label);
    body.appendChild(row);
  });
  todoPanel.append(head, body);
  todoPanel.classList.remove("hidden");
}

function handleActivityEvent(ev, tray) {
  // todo 事件：更新常驻任务清单卡片，不进活动托盘
  if (ev.type === "todo") {
    try {
      const list = JSON.parse(ev.result || "[]");
      renderTodoPanel(Array.isArray(list) ? list : []);
    } catch (e) {
      /* 解析失败忽略 */
    }
    return;
  }
  renderActivity(ev, tray);
}

async function httpJson(method, url, body, retried) {
  const opts = { method, headers: authHeaders() };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (state.abort && method === "POST") {
    opts.signal = state.abort.signal;
  }
  let resp = await fetch(url, opts);
  if (resp.status === 401 && !retried) {
    if (state.loginAvailable) {
      window.location.href = "/login";
      const err = new Error("未登录");
      err.status = 401;
      throw err;
    }
    const ok = await requestToken();
    if (ok) return httpJson(method, url, body, true);
  }
  let data = {};
  try {
    data = await resp.json();
  } catch (e) {
    /* 非 JSON 响应 */
  }
  if (!resp.ok) {
    const err = new Error((data && data.error) || ("HTTP " + resp.status));
    err.status = resp.status;
    throw err;
  }
  return data;
}

function loadSession() {
  try {
    return localStorage.getItem(STORAGE_KEY) || "";
  } catch (e) {
    return "";
  }
}

function saveSession(id) {
  try {
    if (id) localStorage.setItem(STORAGE_KEY, id);
    else localStorage.removeItem(STORAGE_KEY);
  } catch (e) {
    /* 隐私模式忽略 */
  }
}

function loadToken() {
  try {
    return localStorage.getItem(AUTH_TOKEN_KEY) || "";
  } catch (e) {
    return "";
  }
}

function saveToken(token) {
  try {
    if (token) localStorage.setItem(AUTH_TOKEN_KEY, token);
    else localStorage.removeItem(AUTH_TOKEN_KEY);
  } catch (e) {
    /* 隐私模式忽略 */
  }
}

function authHeaders() {
  const token = loadToken();
  return token ? { Authorization: "Bearer " + token } : {};
}

/* 服务鉴权 token 弹窗：接口 401 时弹出，保存后自动重试一次 */
let tokenResolver = null;

function showTokenOverlay() {
  $("token-input").value = loadToken();
  $("token-overlay").classList.remove("hidden");
  $("token-input").focus();
}

function hideTokenOverlay() {
  $("token-overlay").classList.add("hidden");
}

function requestToken() {
  return new Promise((resolve) => {
    tokenResolver = resolve;
    showTokenOverlay();
  });
}

function finishToken(resolved) {
  hideTokenOverlay();
  if (tokenResolver) {
    const fn = tokenResolver;
    tokenResolver = null;
    fn(resolved);
  }
}

/* ---------- 通用对话框（替代原生 confirm/prompt） ---------- */
let dialogResolver = null;
let dialogValidate = null;

function showDialog(opts) {
  $("dialog-title").textContent = opts.title || "提示";
  const desc = $("dialog-desc");
  if (opts.desc) {
    desc.textContent = opts.desc;
    desc.classList.remove("hidden");
  } else {
    desc.classList.add("hidden");
  }
  const input = $("dialog-input");
  if (opts.input) {
    input.classList.remove("hidden");
    input.value = opts.inputValue || "";
    input.type = "text";
  } else {
    input.classList.add("hidden");
    input.value = "";
  }
  dialogValidate = opts.validate || null;
  $("btn-dialog-ok").textContent = opts.okText || "确定";
  $("btn-dialog-ok").classList.toggle("hidden", !!opts.danger);
  $("btn-dialog-danger").textContent = opts.dangerText || "确认";
  $("btn-dialog-danger").classList.toggle("hidden", !opts.danger);
  $("dialog-error").classList.add("hidden");
  $("dialog-overlay").classList.remove("hidden");
  if (opts.input) input.focus();
  return new Promise((resolve) => {
    dialogResolver = resolve;
  });
}

function finishDialog(result) {
  $("dialog-overlay").classList.add("hidden");
  if (dialogResolver) {
    const fn = dialogResolver;
    dialogResolver = null;
    dialogValidate = null;
    fn(result);
  }
}

function closeDialog() {
  finishDialog({ ok: false, value: "" });
}

/* ---------- 侧栏用户信息 ---------- */

async function refreshUserCard() {
  const card = $("user-card");
  if (!state.loginAvailable) {
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
  try {
    const data = await httpJson("GET", API.authMe);
    $("user-name").textContent = data.username || "已登录";
  } catch (e) {
    $("user-name").textContent = "未登录";
  }
}

async function logout() {
  try {
    await httpJson("POST", API.authLogout, {});
  } catch (e) {
    /* 即使接口失败也跳登录页，cookie 清理是尽力而为 */
  }
  window.location.href = "/login";
}

/* ---------- 会话列表（侧栏） ---------- */

function renderSessionList(list) {
  const box = $("session-list");
  box.textContent = "";
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "session-empty";
    empty.textContent = "暂无会话，开始第一段对话吧";
    box.appendChild(empty);
    return;
  }
  list.forEach((s) => {
    const item = document.createElement("div");
    item.className = "session-item";
    if (s.session_id === state.sessionId) item.classList.add("active");

    // 只显示标题：优先用用户自定义/自动标题，其次最后一条用户消息预览，空会话回退会话 ID
    const title = document.createElement("div");
    title.className = "session-item-title";
    title.textContent = s.title || s.preview || s.session_id;
    item.appendChild(title);

    const action = document.createElement("button");
    action.type = "button";
    action.className = "session-item-action";
    action.textContent = s.archived ? "恢复" : "归档";
    action.addEventListener("click", (e) => {
      e.stopPropagation();
      if (s.archived) unarchiveSession(s.session_id);
      else archiveSession(s.session_id);
    });

    item.append(action);

    item.addEventListener(
      "click",
      () => switchSession(s.session_id, s.title || s.preview || s.session_id)
    );
    // 双击会话条目打开改名对话框（替换原"改名"按钮）
    item.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      renameSession(s.session_id, s.title || s.preview || "");
    });
    box.appendChild(item);
  });
}

async function exportSession(sessionId) {
  try {
    let resp = await fetch(API.sessionExport(sessionId), { headers: authHeaders() });
    if (resp.status === 401) {
      if (state.loginAvailable) {
        window.location.href = "/login";
        return;
      }
      const ok = await requestToken();
      if (!ok) return;
      resp = await fetch(API.sessionExport(sessionId), { headers: authHeaders() });
    }
    if (!resp.ok) return;
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "session-" + sessionId + ".md";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    /* 静默：导出失败不影响主界面 */
  }
}

async function loadSessionList() {
  try {
    const data = await httpJson("GET", API.sessions);
    renderSessionList(data.sessions || []);
    // 当前会话的标题跟随列表（优先自定义/自动标题，其次最后一条用户消息预览）
    const current = (data.sessions || []).find(
      (s) => s.session_id === state.sessionId
    );
    if (current) {
      state.sessionTitle = current.title || current.preview || current.session_id;
      if (state.hasMessages) {
        viewTitle.textContent = state.sessionTitle;
      }
    }
  } catch (e) {
    /* 列表刷新失败不影响对话 */
  }
}

async function archiveSession(id) {
  try {
    await httpJson("POST", API.sessionsArchive(id), { archived: true });
  } catch (e) {
    appendMessage("error", "归档失败：" + e.message);
  }
  loadSessionList();
  // 归档视图开着时同步刷新
  if (!$("archived-view").classList.contains("hidden")) {
    refreshArchivedList();
  }
}

async function unarchiveSession(id) {
  try {
    await httpJson("POST", API.sessionsArchive(id), { archived: false });
  } catch (e) {
    appendMessage("error", "取消归档失败：" + e.message);
  }
  loadSessionList();
  refreshArchivedList();
}

async function deleteSession(id) {
  const result = await showDialog({
    title: "删除会话",
    desc: "确定删除该会话？删除后不可恢复。",
    danger: true,
    dangerText: "删除",
  });
  if (!result.ok) return;
  try {
    await httpJson("DELETE", API.sessionDelete(id));
  } catch (e) {
    appendMessage("error", "删除失败：" + e.message);
    return;
  }
  // 删除的是当前会话：切回新对话（newSession 内部会刷新列表）
  if (state.sessionId === id) {
    newSession();
  } else {
    loadSessionList();
  }
  refreshArchivedList();
}

async function renameSession(id, currentName) {
  const result = await showDialog({
    title: "重命名会话",
    desc: "输入新标题（留空清除自定义标题）",
    input: true,
    inputValue: currentName || "",
    okText: "保存",
    validate: (v) => (v.length > 100 ? "标题最长 100 字" : ""),
  });
  if (!result.ok) return;
  try {
    await httpJson("PATCH", API.sessionDelete(id), { title: result.value });
  } catch (e) {
    appendMessage("error", "改名失败：" + e.message);
    return;
  }
  loadSessionList();
  if (state.sessionId === id) {
    const titleEl = $("view-title");
    if (titleEl) titleEl.textContent = result.value || "会话";
  }
}

async function forkSession(id) {
  try {
    const data = await httpJson("POST", API.sessionFork(id), {});
    const forkId = data.session_id;
    if (!forkId) throw new Error("服务器未返回新会话 id");
    switchSession(forkId, data.title || "分支副本");
  } catch (e) {
    appendMessage("error", "分支失败：" + e.message);
  }
}

async function refreshArchivedList() {
  try {
    const data = await httpJson("GET", API.sessions + "?archived_only=1");
    const box = $("archived-list");
    box.textContent = "";
    const list = data.sessions || [];
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "session-empty";
      empty.textContent = "没有归档会话";
      box.appendChild(empty);
      return;
    }
    list.forEach((s) => {
      const item = document.createElement("div");
      item.className = "session-item archived";

      const title = document.createElement("div");
      title.className = "session-item-title";
      title.textContent = s.preview || s.session_id;

      const action = document.createElement("button");
      action.type = "button";
      action.className = "session-item-action";
      action.textContent = "取消归档";
      action.addEventListener("click", (e) => {
        e.stopPropagation();
        unarchiveSession(s.session_id);
      });

      const del = document.createElement("button");
      del.type = "button";
      del.className = "session-item-action session-item-del";
      del.textContent = "删除";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSession(s.session_id);
      });

      item.append(title, action, del);
      box.appendChild(item);
    });
  } catch (e) {
    /* 静默：刷新失败不影响主界面 */
  }
}

function hideAllViews() {
  Object.values(VIEWS).forEach((v) => $(v.viewId).classList.add("hidden"));
  Object.values(NAV_BUTTONS).forEach((id) => $(id).classList.remove("active"));
}

function showView(name) {
  const v = VIEWS[name];
  if (!v) return;
  homeEl.classList.add("hidden");
  chatEl.classList.add("hidden");
  conversationPane.classList.add("hidden");
  conversationPane.classList.remove("grow");
  conversationPane.classList.remove("fill");
  hideAllViews();
  $(v.viewId).classList.remove("hidden");
  $(NAV_BUTTONS[name]).classList.add("active");
  viewTitle.textContent = v.label;
  state.currentView = name;
  v.load();
}

function closeView() {
  hideAllViews();
  state.currentView = "";
  if (state.hasMessages) {
    showThread();
  } else {
    showHome();
  }
}

function toggleView(name) {
  if (state.currentView === name) {
    closeView();
  } else {
    showView(name);
  }
}

function renderPluginList(list) {
  const box = $("plugins-list");
  box.textContent = "";
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "session-empty";
    empty.textContent = "没有可用插件";
    box.appendChild(empty);
    return;
  }
  list.forEach((p) => {
    const item = document.createElement("div");
    item.className = "session-item stack";
    const head = document.createElement("div");
    head.className = "session-item-head";
    const title = document.createElement("span");
    title.className = "session-item-title";
    title.textContent = p.name;
    head.appendChild(title);
    if (p.active) {
      const badge = document.createElement("span");
      badge.className = "session-item-badge";
      badge.textContent = "启用中";
      head.appendChild(badge);
    }
    const desc = document.createElement("div");
    desc.className = "session-item-desc";
    desc.textContent = p.description || "";
    item.append(head, desc);
    box.appendChild(item);
  });
}

function renderSkillList(list) {
  const box = $("skills-list");
  box.textContent = "";
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "session-empty";
    empty.textContent = "没有可用技能";
    box.appendChild(empty);
    return;
  }
  list.forEach((s) => {
    const item = document.createElement("div");
    item.className = "session-item stack";
    const head = document.createElement("div");
    head.className = "session-item-head";
    const title = document.createElement("span");
    title.className = "session-item-title";
    title.textContent = s.name;
    head.appendChild(title);
    const desc = document.createElement("div");
    desc.className = "session-item-desc";
    desc.textContent = s.description || "";
    item.append(head, desc);
    box.appendChild(item);
  });
}

async function refreshPluginsList() {
  try {
    // 合并视图一次拉齐：MCP 服务器 / 记忆插件 / 技能 / 工具
    const [mcpData, pluginData, skillData, toolData] = await Promise.all([
      httpJson("GET", API.mcp),
      httpJson("GET", API.plugins),
      httpJson("GET", API.skills),
      httpJson("GET", API.tools),
    ]);
    renderMCPList(mcpData.servers || []);
    renderPluginList(pluginData.plugins || []);
    renderSkillList(skillData.skills || []);
    renderToolList(toolData.tools || []);
  } catch (e) {
    /* 静默 */
  }
}

function updatePluginHeading(name) {
  const meta = PLUGIN_TAB_META[name] || PLUGIN_TAB_META.mcp;
  const title = $("plugin-title");
  const desc = $("plugin-desc");
  if (title) title.textContent = meta.title;
  if (desc) desc.textContent = meta.desc;
}

function switchPluginTab(name) {
  /* 插件页标签切换：MCP / 记忆插件 / 技能 / 工具 */
  document.querySelectorAll(".plugin-tab").forEach((btn) => {
    const active = btn.dataset.tab === name;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  ["mcp", "plugins", "skills", "tools"].forEach((key) => {
    $("panel-" + key).classList.toggle("hidden", key !== name);
  });
  updatePluginHeading(name);
}

function renderMCPList(list) {
  const box = $("mcp-list");
  box.textContent = "";
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "session-empty";
    empty.textContent = "未配置 MCP 服务器（mcp_servers.json）";
    box.appendChild(empty);
    return;
  }
  list.forEach((s) => {
    const item = document.createElement("div");
    item.className = "session-item stack";
    const head = document.createElement("div");
    head.className = "session-item-head";
    const title = document.createElement("span");
    title.className = "session-item-title";
    title.textContent = s.name;
    head.appendChild(title);
    const countBadge = document.createElement("span");
    countBadge.className = "session-item-badge plugin-badge-count";
    countBadge.textContent = `${s.tools} 个工具`;
    head.appendChild(countBadge);
    if (s.parallel) {
      const parallelBadge = document.createElement("span");
      parallelBadge.className = "session-item-badge plugin-badge-parallel";
      parallelBadge.textContent = "可并行";
      head.appendChild(parallelBadge);
    }
    const desc = document.createElement("div");
    desc.className = "session-item-desc";
    desc.textContent = s.active ? "已连接" : "未连接";
    item.append(head, desc);
    box.appendChild(item);
  });
}

function renderToolList(list) {
  const box = $("tools-list");
  box.textContent = "";
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "session-empty";
    empty.textContent = "没有可用工具";
    box.appendChild(empty);
    return;
  }
  list.forEach((t) => {
    const item = document.createElement("div");
    item.className = "session-item stack";
    const head = document.createElement("div");
    head.className = "session-item-head";
    const title = document.createElement("span");
    title.className = "session-item-title";
    title.textContent = t.name;
    head.appendChild(title);
    const desc = document.createElement("div");
    desc.className = "session-item-desc";
    desc.textContent = t.description || "";
    item.append(head, desc);
    box.appendChild(item);
  });
}

/* ---------- 工作区改动视图 ---------- */

let wdiffMode = "working";
let wdiffSelectedPath = "";
let wdiffCache = null;
const wdiffClosedDirs = new Set();

function renderDiffLines(diffText) {
  const box = document.createElement("div");
  box.className = "wdiff-diff";
  // 二进制文件没有行级 diff（chunk 只有 "Binary files ... differ" 元信息）
  if (/^Binary files .* differ$/m.test(diffText)) {
    const row = document.createElement("div");
    row.className = "diff-line diff-meta";
    row.textContent = "（二进制文件，不显示 diff）";
    box.appendChild(row);
    return box;
  }
  diffText.split("\n").forEach((line) => {
    // 跳过 git diff 文件头元信息：diff --git / index / new file / deleted file /
    // old mode / new mode / --- a / +++ b（路径与新增/修改/删除状态目录树已展示）
    if (
      line.startsWith("diff --git") ||
      line.startsWith("index ") ||
      line.startsWith("new file") ||
      line.startsWith("deleted file") ||
      line.startsWith("old mode") ||
      line.startsWith("new mode") ||
      line.startsWith("--- ") ||
      line.startsWith("+++ ")
    ) {
      return;
    }
    const row = document.createElement("div");
    row.className = "diff-line";
    if (line.startsWith("@@")) {
      row.className += " diff-hunk";
    } else if (line.startsWith("+")) {
      row.className += " diff-add";
    } else if (line.startsWith("-")) {
      row.className += " diff-del";
    } else {
      row.className += " diff-ctx";
    }
    row.textContent = line || "\u00A0";
    box.appendChild(row);
  });
  return box;
}

function buildFileTree(files) {
  const root = { children: {}, files: [] };
  files.forEach((f) => {
    const parts = f.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!node.children[parts[i]]) node.children[parts[i]] = { children: {}, files: [] };
      node = node.children[parts[i]];
    }
    node.files.push(f);
  });
  return root;
}

function buildFileRow(f, depth) {
  const item = document.createElement("div");
  item.className = "wdiff-file" + (f.path === wdiffSelectedPath ? " active" : "");
  item.style.paddingLeft = 8 + depth * 14 + "px";
  const head = document.createElement("div");
  head.className = "wdiff-file-head";
  const name = document.createElement("span");
  name.className = "wdiff-file-name";
  name.textContent = f.path.split("/").pop();
  name.title = f.path;
  head.appendChild(name);
  const badge = document.createElement("span");
  badge.className = "wdiff-file-status " + f.status;
  badge.textContent =
    f.status === "added" ? "新增" : f.status === "deleted" ? "删除" : "修改";
  head.appendChild(badge);
  const counts = document.createElement("div");
  counts.className = "wdiff-file-counts";
  if (f.additions) {
    const a = document.createElement("span");
    a.className = "diff-add-count";
    a.textContent = "+" + f.additions;
    counts.appendChild(a);
  }
  if (f.deletions) {
    const d = document.createElement("span");
    d.className = "diff-del-count";
    d.textContent = "-" + f.deletions;
    counts.appendChild(d);
  }
  item.append(head, counts);
  item.addEventListener("click", () => selectWorkingDiffFile(f.path));
  return item;
}

function renderTreeNode(node, path, depth, container) {
  Object.keys(node.children)
    .sort((a, b) => a.localeCompare(b))
    .forEach((name) => {
      const child = node.children[name];
      const childPath = path ? path + "/" + name : name;
      const closed = wdiffClosedDirs.has(childPath);
      const row = document.createElement("div");
      row.className = "wdiff-folder";
      row.style.paddingLeft = 6 + depth * 14 + "px";
      const arrow = document.createElement("span");
      arrow.className = "wdiff-folder-arrow";
      arrow.textContent = closed ? "▸" : "▾";
      const label = document.createElement("span");
      label.className = "wdiff-folder-name";
      label.textContent = name;
      row.append(arrow, label);
      row.addEventListener("click", () => {
        if (closed) wdiffClosedDirs.delete(childPath);
        else wdiffClosedDirs.add(childPath);
        if (wdiffCache) renderWorkingDiff(wdiffCache);
      });
      container.appendChild(row);
      if (!closed) {
        const childContainer = document.createElement("div");
        childContainer.className = "wdiff-tree-children";
        renderTreeNode(child, childPath, depth + 1, childContainer);
        container.appendChild(childContainer);
      }
    });
  node.files
    .slice()
    .sort((a, b) => a.path.localeCompare(b.path))
    .forEach((f) => container.appendChild(buildFileRow(f, depth)));
}

function renderFileTree(files) {
  const box = $("wdiff-files");
  box.textContent = "";
  const root = buildFileTree(files);
  const frag = document.createDocumentFragment();
  renderTreeNode(root, "", 0, frag);
  box.appendChild(frag);
}

function renderWorkingDiff(data) {
  const files = $("wdiff-files");
  const diffBox = $("wdiff-diff");
  const statBox = $("wdiff-stat");
  files.textContent = "";
  diffBox.textContent = "";
  statBox.textContent = "";
  if (!data || !data.success) {
    const empty = document.createElement("div");
    empty.className = "session-empty";
    empty.textContent = (data && data.error) || "无法读取工作区改动";
    files.appendChild(empty);
    return;
  }
  const fileList = data.files || [];
  if (data.empty || !fileList.length) {
    const empty = document.createElement("div");
    empty.className = "session-empty";
    empty.textContent = "工作区干净，没有改动";
    files.appendChild(empty);
    return;
  }
  const summary = data.summary || {};
  statBox.textContent = summary.files
    ? "共 " + summary.files + " 个文件 · 新增 +" + summary.additions +
      " · 删除 -" + summary.deletions
    : "";
  const names = fileList.map((f) => f.path);
  if (!names.includes(wdiffSelectedPath)) wdiffSelectedPath = names[0];
  renderFileTree(fileList);
  const current = fileList.find((f) => f.path === wdiffSelectedPath) || fileList[0];
  diffBox.appendChild(renderDiffLines(current.diff));
}

async function refreshWorkingDiff() {
  try {
    const data = await httpJson(
      "GET",
      API.workingDiff + "?mode=" + encodeURIComponent(wdiffMode)
    );
    wdiffCache = data;
    renderWorkingDiff(data);
  } catch (e) {
    wdiffCache = null;
    renderWorkingDiff({ success: false, error: e.message });
  }
}

function selectWorkingDiffFile(path) {
  if (!wdiffCache) return;
  wdiffSelectedPath = path;
  renderWorkingDiff(wdiffCache);
}

function setWorkingDiffMode(mode) {
  wdiffMode = mode;
  ["working", "staged", "all"].forEach((m) => {
    $("btn-wdiff-" + m).classList.toggle("active", m === mode);
  });
  refreshWorkingDiff();
}

async function switchSession(id, title) {
  turnToken += 1;  // 在途旧轮次全部失效，防止跨会话泄漏
  if (state.inFlight && state.abort) {
    state.abort.abort();
  }
  stopPolling();
  state.pendingItem = null;
  state.pendingKey = "";
  state.clarifyItem = null;
  $("approval-overlay").classList.add("hidden");
  $("clarify-overlay").classList.add("hidden");
  hideAllViews();
  state.sessionId = id;
  state.sessionTitle = title || "会话";
  saveSession(id);
  chatEl.querySelectorAll(".msg-row, .msg.thinking, .activity, .activity-tray").forEach((el) => el.remove());
  Object.keys(activityById).forEach((k) => delete activityById[k]);
  currentTray = null;

  try {
    const data = await httpJson("GET", API.sessionsMessages(id));
    const messages = data.messages || [];
    if (!messages.length) {
      showHome();
    } else {
      showThread();
      // 过程事件按用户消息 id 分组：还原每轮的活动托盘（收拢态，可展开）
      const eventsByMsg = {};
      (data.events || []).forEach((ev) => {
        if (ev.user_message_id === undefined || ev.user_message_id === null) return;
        const k = ev.user_message_id;
        (eventsByMsg[k] = eventsByMsg[k] || []).push(ev);
      });
      messages.forEach((m) => {
        // 旧数据里被误存的"轮次上限收尾"内部指令：不渲染成用户提问
        if (
          m.role === "user" &&
          typeof m.content === "string" &&
          m.content.startsWith("已经达到本轮执行上限")
        ) {
          return;
        }
        if (m.role === "assistant" || m.role === "user") {
          appendMessage(m.role, m.content);
        }
        if (m.role === "user" && m.id !== undefined) {
          renderReplayTray(eventsByMsg[m.id]);
        }
      });
    }
    renderTodoPanel(data.todos || []);
  } catch (e) {
    appendMessage("error", "加载会话历史失败：" + e.message);
    showThread();
    renderTodoPanel([]);
  }
  loadSessionList();
  inputEl.focus();
}

/* ---------- 审批 ---------- */

function renderPending(items) {
  state.queueCount = items.length;
  const item = items.length ? items[0] : null;
  const nextKey = item ? JSON.stringify(item) : "";

  if (!item) {
    state.pendingItem = null;
    state.pendingKey = "";
    $("approval-overlay").classList.add("hidden");
    return;
  }

  // 轮询时不要每次都清掉拒绝框：用户可能刚点了“拒绝”，正在填理由。
  // 只有切换到下一条待审批命令时才重置拒绝框，避免 800ms 轮询打断操作。
  if (nextKey !== state.pendingKey) {
    state.pendingKey = nextKey;
    $("deny-box").classList.add("hidden");
    $("deny-reason").value = "";
  }

  state.pendingItem = item;
  $("approval-desc").textContent = item.description || "未知";
  $("approval-cmd").textContent = item.command || "";
  const allowPermanent = item.allow_permanent !== false;
  $("btn-always").classList.toggle("hidden", !allowPermanent);

  const note = [];
  if (item.allow_permanent === false) {
    note.push("智能审查已判定危险，仅允许单次覆盖，不支持永久/会话记忆。");
  }
  if (state.queueCount > 1) {
    note.push("还有 " + (state.queueCount - 1) + " 条命令排队等待处理（按先进先出）。");
  }
  $("approval-note").textContent = note.join(" ");
  $("approval-note").classList.toggle("hidden", note.length === 0);
  $("approval-queue").classList.toggle("hidden", state.queueCount <= 1);
  $("approval-queue").textContent = "队列 " + state.queueCount;

  $("approval-overlay").classList.remove("hidden");
}

async function pollApprovals() {
  if (!state.sessionId) return;
  try {
    const data = await httpJson(
      "GET",
      API.pending + "?session_id=" + encodeURIComponent(state.sessionId)
    );
    renderPending(data.pending || []);
  } catch (e) {
    /* 轮询失败静默重试，主请求的错误由 /chat 抛给用户 */
  }
  try {
    const data = await httpJson(
      "GET",
      API.clarifyPending + "?session_id=" + encodeURIComponent(state.sessionId)
    );
    renderClarify(data.pending || []);
  } catch (e) {
    /* 静默 */
  }
}

function startPolling() {
  stopPolling();
  pollApprovals();
  state.pollTimer = setInterval(pollApprovals, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function resolveApproval(choice) {
  if (!state.sessionId || !state.pendingItem) return;
  let reason;
  if (choice === "deny") {
    reason = $("deny-reason").value.trim() || undefined;
  }
  try {
    await httpJson("POST", API.resolve, {
      session_id: state.sessionId,
      choice,
      ...(reason ? { reason } : {}),
    });
    // 这条审批已提交；若队列里还有下一条，pollApprovals 会按新 key 重置拒绝框。
    $("deny-box").classList.add("hidden");
    $("deny-reason").value = "";
    state.pendingKey = "";
    // 立即刷新一次，让弹窗反映最新队列
    await pollApprovals();
  } catch (e) {
    appendMessage("error", "审批提交失败：" + e.message);
  }
}

/* ---------- 中途提问（clarify） ---------- */

function renderClarify(items) {
  const item = items.length ? items[0] : null;
  state.clarifyItem = item;
  if (!item) {
    $("clarify-overlay").classList.add("hidden");
    return;
  }
  $("clarify-question").textContent = item.question || "";
  const box = $("clarify-choices");
  box.textContent = "";
  if (item.choices && item.choices.length) {
    item.choices.forEach((c) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn clarify-choice";
      btn.textContent = c;
      btn.dataset.value = c;
      if (item.multi_select) {
        btn.addEventListener("click", () => btn.classList.toggle("selected"));
      } else {
        btn.addEventListener("click", () => resolveClarify(c));
      }
      box.appendChild(btn);
    });
  }
  $("clarify-queue").classList.toggle("hidden", items.length <= 1);
  $("clarify-queue").textContent = "队列 " + items.length;
  $("clarify-text").value = "";
  $("clarify-overlay").classList.remove("hidden");
}

async function resolveClarify(answer) {
  if (!state.sessionId || !state.clarifyItem) return;
  const item = state.clarifyItem;
  let finalAnswer = answer;
  if (item.multi_select) {
    const picked = Array.from(
      document.querySelectorAll("#clarify-choices .selected")
    ).map((b) => b.dataset.value);
    const text = $("clarify-text").value.trim();
    if (picked.length) finalAnswer = JSON.stringify(picked);
    else if (text) finalAnswer = text;
  } else {
    const text = $("clarify-text").value.trim();
    if (text) finalAnswer = text;
  }
  if (!finalAnswer || !finalAnswer.trim()) return;
  try {
    await httpJson("POST", API.clarifyResolve, {
      session_id: state.sessionId,
      clarify_id: item.clarify_id,
      answer: finalAnswer,
    });
    await pollApprovals();
  } catch (e) {
    appendMessage("error", "回答提交失败：" + e.message);
  }
}

/* ---------- 对话 ---------- */

async function readSse(resp, handlers) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = "message";
      const dataLines = [];
      raw.split("\n").forEach((line) => {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      });
      if (!dataLines.length) continue;
      let data;
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch (e) {
        continue;
      }
      const fn = handlers[event];
      if (fn) fn(data);
    }
  }
}

function createAssistantBubble() {
  const row = document.createElement("div");
  row.className = "msg-row";
  const div = document.createElement("div");
  div.className = "msg assistant";
  row.appendChild(div);
  addMessageActions(div, row);
  chatEl.appendChild(row);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

async function sendStreaming(body, retried) {
  const opts = {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
    body: JSON.stringify(body),
  };
  if (state.abort) opts.signal = state.abort.signal;

  let resp;
  try {
    resp = await fetch(API.chatSessionStream(state.sessionId), opts);
  } catch (e) {
    // 主动取消（新对话/切换会话）不再回退；连接失败回退非流式
    if (state.abort && state.abort.signal.aborted) return true;
    return false;
  }
  if (resp.status === 401 && !retried) {
    if (state.loginAvailable) {
      window.location.href = "/login";
      return false;
    }
    const ok = await requestToken();
    if (ok) return sendStreaming(body, true);
    return false;
  }
  if (!resp.ok || !resp.body) return false;

  // 活动托盘先于消息创建：思考和工具调用显示在消息上方
  const tray = beginActivityTray();
  let bubble = null;
  let replyText = "";
  let finalized = false;
  const showReply = () => {
    setThinking(false);
    if (!bubble) {
      bubble = createAssistantBubble();
    }
    bubble.innerHTML = renderMarkdown(replyText || "(空回复)");
  };
  const finalize = (withMessage = true) => {
    if (finalized) return;
    finalized = true;
    if (withMessage) {
      showReply();
    } else {
      setThinking(false);
    }
    finalizeTray(tray);  // 思考完成 → 活动收拢成一行摘要
    chatEl.scrollTop = chatEl.scrollHeight;
  };

  await readSse(resp, {
    session: (d) => {
      if (d.session_id) {
        state.sessionId = d.session_id;
        saveSession(d.session_id);
      }
    },
    activity: (ev) => handleActivityEvent(ev, tray),
    token: (d) => {
      replyText += d.text || "";
      if (!bubble) {
        setThinking(false);
        bubble = createAssistantBubble();
      }
      bubble.textContent = replyText;
      chatEl.scrollTop = chatEl.scrollHeight;
    },
    message: (d) => {
      if (d.session_id) {
        state.sessionId = d.session_id;
        saveSession(d.session_id);
      }
      if (typeof d.reply === "string") replyText = d.reply;
      finalize();
    },
    error: (d) => {
      if (bubble) {
        finalize();
      } else {
        finalize(false);
      }
      appendMessage("error", "请求失败：" + (d.error || "未知错误"));
    },
    done: () => finalize(),
  });
  return true;
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || state.inFlight) return;

  // 两步式：新会话先 POST /sessions 让服务端生成 id，再发聊天请求
  // （对齐 Hermes api_server：先建会话拿 id，后续请求都带它）
  if (!state.sessionId) {
    try {
      const created = await httpJson("POST", API.createSession, {});
      state.sessionId = created.session_id;
      saveSession(state.sessionId);
    } catch (e) {
      appendMessage("error", "创建会话失败：" + e.message);
      return;
    }
  }

  const token = turnToken;  // 本轮令牌：切换会话后失效，过期结果直接丢弃
  inputEl.value = "";
  autoResize();
  state.sessionTitle = text;
  appendMessage("user", text);
  setThinking(true);
  state.inFlight = true;
  sendBtn.disabled = true;
  sendBtn.setAttribute("aria-label", "发送中");
  inputEl.disabled = true;
  state.abort = new AbortController();
  startPolling();

  const body = { message: text };

  try {
    const streamed = await sendStreaming(body);
    if (token !== turnToken) return;  // 会话已切换：丢弃旧轮结果
    if (!streamed) {
      // 旧服务器或流式不可用：回退到一次性 /chat
      const data = await httpJson("POST", API.chatSession(state.sessionId), body);
      if (token !== turnToken) return;
      setThinking(false);
      const tray = beginActivityTray();  // 活动在消息上方，一次性返回后立即收拢
      (data.events || []).forEach((ev) => handleActivityEvent(ev, tray));
      finalizeTray(tray);
      appendMessage("assistant", data.reply || "(空回复)");
      renderTodoPanel(data.todos || []);
    }
  } catch (e) {
    if (token !== turnToken) return;  // 过期错误不展示
    if (!(state.abort && state.abort.signal.aborted)) {
      setThinking(false);
      appendMessage("error", "请求失败：" + e.message);
    }
  } finally {
    state.inFlight = false;
    updateSendState();
    sendBtn.setAttribute("aria-label", "发送");
    inputEl.disabled = false;
    stopPolling();
    inputEl.focus();
    // 请求结束后若还有未解决审批（如刷新后残留），保留弹窗让用户处理
    if (state.pendingItem) {
      pollApprovals();
    }
    loadSessionList();
  }
}

function newSession() {
  turnToken += 1;  // 同上：新对话使在途旧轮次失效
  if (state.inFlight && state.abort) {
    state.abort.abort();
  }
  state.sessionId = "";
  state.sessionTitle = "";
  state.queueCount = 0;
  state.pendingItem = null;
  state.pendingKey = "";
  saveSession("");
  chatEl.querySelectorAll(".msg-row, .msg.thinking, .activity, .activity-tray").forEach((el) => el.remove());
  Object.keys(activityById).forEach((k) => delete activityById[k]);
  currentTray = null;
  renderTodoPanel([]);
  stopPolling();
  $("approval-overlay").classList.add("hidden");
  showHome();
  loadSessionList();
}

function autoResize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 170) + "px";
}

function updateSendState() {
  /* 发送按钮状态跟随输入内容：无文本 / 发送中 / 输入禁用时置灰 */
  sendBtn.disabled = !inputEl.value.trim() || state.inFlight || inputEl.disabled;
}

/* ---------- 初始化 ---------- */

function bindEvents() {
  sendBtn.addEventListener("click", sendMessage);
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  inputEl.addEventListener("input", () => {
    autoResize();
    updateSendState();
  });

  // 建议卡片：点击即发送
  document.querySelectorAll(".suggestion").forEach((btn) => {
    btn.addEventListener("click", () => {
      inputEl.value = btn.dataset.prompt || btn.textContent.trim();
      sendMessage();
    });
  });

  // 历史会话已持久化且侧栏可随时切回，新对话直接执行、无需确认
  const handleNewSession = () => newSession();
  $("btn-new-session-side").addEventListener("click", handleNewSession);
  $("btn-archived").addEventListener("click", () => toggleView("archived"));
  $("btn-plugins").addEventListener("click", () => toggleView("plugins"));
  $("btn-working-diff").addEventListener("click", () => toggleView("wdiff"));
  document.querySelectorAll(".plugin-tab").forEach((btn) => {
    btn.addEventListener("click", () => switchPluginTab(btn.dataset.tab));
  });
  $("btn-working-diff-refresh").addEventListener("click", refreshWorkingDiff);
  $("btn-wdiff-working").addEventListener("click", () => setWorkingDiffMode("working"));
  $("btn-wdiff-staged").addEventListener("click", () => setWorkingDiffMode("staged"));
  $("btn-wdiff-all").addEventListener("click", () => setWorkingDiffMode("all"));
  $("btn-export-session").addEventListener("click", () => {
    if (state.sessionId) exportSession(state.sessionId);
  });

  // 审批按钮
  $("btn-once").addEventListener("click", () => resolveApproval("once"));
  $("btn-session").addEventListener("click", () => resolveApproval("session"));
  $("btn-always").addEventListener("click", () => resolveApproval("always"));
  $("btn-deny").addEventListener("click", () => {
    $("deny-box").classList.remove("hidden");
    $("deny-reason").focus();
  });
  $("btn-deny-cancel").addEventListener("click", () => {
    $("deny-box").classList.add("hidden");
  });
  $("btn-deny-confirm").addEventListener("click", () => resolveApproval("deny"));
  $("btn-clarify-submit").addEventListener("click", () => resolveClarify(""));
  $("btn-clarify-cancel").addEventListener("click", () =>
    resolveClarify("（用户取消）")
  );
  $("clarify-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter") resolveClarify("");
  });

  // 鉴权 token 弹窗按钮
  $("btn-token-save").addEventListener("click", () => {
    saveToken($("token-input").value.trim());
    finishToken(true);
  });
  $("btn-token-cancel").addEventListener("click", () => finishToken(false));
  $("token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      $("btn-token-save").click();
    }
  });

  // 通用对话框按钮
  $("btn-dialog-ok").addEventListener("click", () => {
    const input = $("dialog-input");
    const usingInput = !input.classList.contains("hidden");
    const value = usingInput ? input.value.trim() : "";
    if (dialogValidate) {
      const err = dialogValidate(value);
      if (err) {
        $("dialog-error").textContent = err;
        $("dialog-error").classList.remove("hidden");
        return;
      }
    }
    finishDialog({ ok: true, value });
  });
  $("btn-dialog-danger").addEventListener("click", () => {
    finishDialog({ ok: true, value: "" });
  });
  $("btn-dialog-cancel").addEventListener("click", closeDialog);
  $("dialog-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      $("btn-dialog-ok").click();
    }
  });

  // 侧栏用户卡片：切换账号 / 退出登录（都先注销并回到登录页）
  $("btn-switch-account").addEventListener("click", logout);
  $("btn-logout").addEventListener("click", logout);
}

function init() {
  state.sessionId = loadSession();
  bindEvents();
  loadSessionList();
  // 探测服务端是否提供用户名密码登录：401 时决定跳 /login 还是弹 token 输入框
  fetch(API.authConfig)
    .then((r) => r.json().catch(() => ({})))
    .then((data) => {
      state.loginAvailable = !!data.login_available;
      refreshUserCard();
    })
    .catch(() => {
      state.loginAvailable = false;
      refreshUserCard();
    });
  inputEl.disabled = false;
  updateSendState();
  inputEl.focus();
}

init();
