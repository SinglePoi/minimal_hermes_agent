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
  sessions: "/sessions",
  plugins: "/plugins",
  skills: "/skills",
  tools: "/tools",
  sessionsMessages: (id) => "/sessions/" + encodeURIComponent(id) + "/messages",
  sessionsArchive: (id) => "/sessions/" + encodeURIComponent(id) + "/archive",
};

const STORAGE_KEY = "agent.session";
const POLL_INTERVAL_MS = 800;

const state = {
  sessionId: "",
  sessionTitle: "",
  currentView: "",
  inFlight: false,
  pollTimer: null,
  abort: null,
  queueCount: 0,
  pendingItem: null,
  hasMessages: false,
};

const $ = (id) => document.getElementById(id);
const chatEl = $("chat");
const homeEl = $("home");
const inputEl = $("input");
const sendBtn = $("btn-send");
const viewTitle = $("view-title");

/* 工作区视图注册表：已归档 / 插件 / 技能 */
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
  skills: {
    viewId: "skills-view",
    label: "技能",
    load: refreshSkillsList,
  },
  tools: {
    viewId: "tools-view",
    label: "工具",
    load: refreshToolsList,
  },
};
const NAV_BUTTONS = {
  archived: "btn-archived",
  plugins: "btn-plugins",
  skills: "btn-skills",
  tools: "btn-tools",
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

function showThread() {
  homeEl.classList.add("hidden");
  chatEl.classList.remove("hidden");
  hideAllViews();
  viewTitle.textContent = state.sessionTitle || "会话";
  state.hasMessages = true;
}

function showHome() {
  homeEl.classList.remove("hidden");
  chatEl.classList.add("hidden");
  hideAllViews();
  viewTitle.textContent = "新对话";
  state.hasMessages = false;
}

function appendMessage(role, text) {
  if (!state.hasMessages) showThread();
  const div = document.createElement("div");
  div.className = "msg " + role;
  if (role === "assistant") {
    div.innerHTML = renderMarkdown(text);
  } else {
    div.textContent = text;
  }
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

const ACTIVITY_ICONS = { think: "🤔", tool: "🛠️", skill: "📘", source: "🌐" };
const ACTIVITY_LABELS = { think: "思考", tool: "调用工具", skill: "加载技能", source: "来源" };
const activityById = {};

function buildActivityDetail(ev) {
  const lines = [];
  if (ev.args) lines.push("参数：" + ev.args);
  if (ev.result) lines.push("结果：" + ev.result);
  return lines.join("\n");
}

function renderActivity(ev) {
  const existing = ev.id !== undefined ? activityById[ev.id] : null;
  if (existing) {
    existing.detail.textContent = buildActivityDetail(ev);
    return;
  }
  const type = ev.type || "tool";
  const item = document.createElement("div");
  item.className = "activity";

  const head = document.createElement("button");
  head.type = "button";
  head.className = "activity-head";
  head.textContent =
    (ACTIVITY_ICONS[type] || "•") +
    " " +
    (ACTIVITY_LABELS[type] || type) +
    "：" +
    (ev.name || "");

  const detail = document.createElement("div");
  detail.className = "activity-detail hidden";
  detail.textContent = buildActivityDetail(ev);
  head.addEventListener("click", () => detail.classList.toggle("hidden"));

  item.append(head, detail);
  chatEl.appendChild(item);
  chatEl.scrollTop = chatEl.scrollHeight;
  if (ev.id !== undefined) activityById[ev.id] = { head, detail };
}

function setThinking(on) {
  if (on) {
    if (!chatEl.querySelector(".msg.thinking")) {
      const div = document.createElement("div");
      div.className = "msg thinking";
      div.textContent = "思考中";
      chatEl.appendChild(div);
      chatEl.scrollTop = chatEl.scrollHeight;
    }
  } else {
    const el = chatEl.querySelector(".msg.thinking");
    if (el) el.remove();
  }
}

async function httpJson(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (state.abort && method === "POST") {
    opts.signal = state.abort.signal;
  }
  const resp = await fetch(url, opts);
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

    // 只显示标题：优先用最后一条用户消息作为会话标题，空会话回退到会话 ID
    const title = document.createElement("div");
    title.className = "session-item-title";
    title.textContent = s.preview || s.session_id;
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
    item.appendChild(action);

    item.addEventListener(
      "click",
      () => switchSession(s.session_id, s.preview || s.session_id)
    );
    box.appendChild(item);
  });
}

async function loadSessionList() {
  try {
    const data = await httpJson("GET", API.sessions);
    renderSessionList(data.sessions || []);
    // 当前会话的标题跟随列表里的预览（最后一条用户消息），与侧栏保持一致
    const current = (data.sessions || []).find(
      (s) => s.session_id === state.sessionId
    );
    if (current) {
      state.sessionTitle = current.preview || current.session_id;
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

      item.append(title, action);
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
    const data = await httpJson("GET", API.plugins);
    renderPluginList(data.plugins || []);
  } catch (e) {
    /* 静默 */
  }
}

async function refreshSkillsList() {
  try {
    const data = await httpJson("GET", API.skills);
    renderSkillList(data.skills || []);
  } catch (e) {
    /* 静默 */
  }
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

async function refreshToolsList() {
  try {
    const data = await httpJson("GET", API.tools);
    renderToolList(data.tools || []);
  } catch (e) {
    /* 静默 */
  }
}

async function switchSession(id, title) {
  if (state.inFlight && state.abort) {
    state.abort.abort();
  }
  stopPolling();
  state.pendingItem = null;
  $("approval-overlay").classList.add("hidden");
  hideAllViews();
  state.sessionId = id;
  state.sessionTitle = title || "会话";
  saveSession(id);
  chatEl.querySelectorAll(".msg").forEach((el) => el.remove());
  Object.keys(activityById).forEach((k) => delete activityById[k]);

  try {
    const data = await httpJson("GET", API.sessionsMessages(id));
    const messages = data.messages || [];
    if (!messages.length) {
      showHome();
    } else {
      showThread();
      messages.forEach((m) => {
        if (m.role === "assistant" || m.role === "user") {
          appendMessage(m.role, m.content);
        }
      });
    }
  } catch (e) {
    appendMessage("error", "加载会话历史失败：" + e.message);
    showThread();
  }
  loadSessionList();
  inputEl.focus();
}

/* ---------- 审批 ---------- */

function renderPending(items) {
  state.queueCount = items.length;
  state.pendingItem = items.length ? items[0] : null;

  if (!state.pendingItem) {
    $("approval-overlay").classList.add("hidden");
    return;
  }

  const item = state.pendingItem;
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

  $("deny-box").classList.add("hidden");
  $("deny-reason").value = "";
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
    // 立即刷新一次，让弹窗反映最新队列
    await pollApprovals();
  } catch (e) {
    appendMessage("error", "审批提交失败：" + e.message);
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
  const div = document.createElement("div");
  div.className = "msg assistant";
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

async function sendStreaming(body) {
  const opts = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  if (state.abort) opts.signal = state.abort.signal;

  let resp;
  try {
    resp = await fetch(API.chatStream, opts);
  } catch (e) {
    // 主动取消（新对话/切换会话）不再回退；连接失败回退非流式
    if (state.abort && state.abort.signal.aborted) return true;
    return false;
  }
  if (!resp.ok || !resp.body) return false;

  const bubble = createAssistantBubble();
  setThinking(false);
  let replyText = "";
  let finalized = false;
  const finalize = () => {
    if (finalized) return;
    finalized = true;
    bubble.innerHTML = renderMarkdown(replyText || "(空回复)");
    chatEl.scrollTop = chatEl.scrollHeight;
  };

  await readSse(resp, {
    activity: (ev) => renderActivity(ev),
    token: (d) => {
      replyText += d.text || "";
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
      finalize();
      appendMessage("error", "请求失败：" + (d.error || "未知错误"));
    },
    done: () => finalize(),
  });
  return true;
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || state.inFlight) return;

  inputEl.value = "";
  autoResize();
  state.sessionTitle = text;
  appendMessage("user", text);
  setThinking(true);
  state.inFlight = true;
  sendBtn.disabled = true;
  sendBtn.textContent = "…";
  inputEl.disabled = true;
  state.abort = new AbortController();
  startPolling();

  const body = { message: text };
  if (state.sessionId) body.session_id = state.sessionId;

  try {
    const streamed = await sendStreaming(body);
    if (!streamed) {
      // 旧服务器或流式不可用：回退到一次性 /chat
      const data = await httpJson("POST", API.chat, body);
      if (data.session_id) {
        state.sessionId = data.session_id;
        saveSession(data.session_id);
      }
      setThinking(false);
      (data.events || []).forEach(renderActivity);
      appendMessage("assistant", data.reply || "(空回复)");
    }
  } catch (e) {
    if (!(state.abort && state.abort.signal.aborted)) {
      setThinking(false);
      appendMessage("error", "请求失败：" + e.message);
    }
  } finally {
    state.inFlight = false;
    sendBtn.disabled = false;
    sendBtn.textContent = "发送";
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
  if (state.inFlight && state.abort) {
    state.abort.abort();
  }
  state.sessionId = "";
  state.sessionTitle = "";
  state.queueCount = 0;
  state.pendingItem = null;
  saveSession("");
  chatEl.querySelectorAll(".msg").forEach((el) => el.remove());
  Object.keys(activityById).forEach((k) => delete activityById[k]);
  stopPolling();
  $("approval-overlay").classList.add("hidden");
  showHome();
  loadSessionList();
}

function autoResize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 170) + "px";
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
  inputEl.addEventListener("input", autoResize);

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
  $("btn-skills").addEventListener("click", () => toggleView("skills"));
  $("btn-tools").addEventListener("click", () => toggleView("tools"));
  $("btn-archived-back").addEventListener("click", closeView);
  $("btn-plugins-back").addEventListener("click", closeView);
  $("btn-skills-back").addEventListener("click", closeView);
  $("btn-tools-back").addEventListener("click", closeView);

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
}

function init() {
  state.sessionId = loadSession();
  bindEvents();
  loadSessionList();
  inputEl.disabled = false;
  inputEl.focus();
}

init();
