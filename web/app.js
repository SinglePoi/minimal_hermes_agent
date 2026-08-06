"use strict";
/* Agent Web 前端：连 server.py 的 /chat + 审批轮询/resolve。
   布局参考 Codex 首页：左侧毛玻璃侧栏 + 顶部标题栏 + hero/建议卡片 + 底部输入框。
   与 Hermes dashboard 的交互契约对齐：/chat 阻塞等待审批，前端轮询
   /approvals/pending 发现待审批项，POST /approvals/resolve 后线程被唤醒。 */

const API = {
  chat: "/chat",
  pending: "/approvals/pending",
  resolve: "/approvals/resolve",
  health: "/health",
  sessions: "/sessions",
  sessionsMessages: (id) => "/sessions/" + encodeURIComponent(id) + "/messages",
};

const STORAGE_KEY = "agent.session";
const POLL_INTERVAL_MS = 800;

const state = {
  sessionId: "",
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
const statusEl = $("status");
const statusText = $("status-text");
const viewTitle = $("view-title");

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
  viewTitle.textContent = state.sessionId
    ? "会话 · " + state.sessionId
    : "当前会话";
  state.hasMessages = true;
}

function showHome() {
  homeEl.classList.remove("hidden");
  chatEl.classList.add("hidden");
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

function setOnline(ok, text) {
  const dot = statusEl.querySelector(".dot");
  dot.classList.toggle("ok", ok);
  dot.classList.toggle("err", !ok);
  statusText.textContent = text;
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

function shortSessionId(id) {
  return id.length > 18 ? "…" + id.slice(-14) : id;
}

function formatTime(utc) {
  if (!utc) return "";
  const t = new Date(utc.replace(" ", "T") + "Z");
  if (isNaN(t.getTime())) return utc;
  const now = new Date();
  if (t.toDateString() === now.toDateString()) {
    return t.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  }
  return t.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

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
    const item = document.createElement("button");
    item.type = "button";
    item.className = "session-item";
    if (s.session_id === state.sessionId) item.classList.add("active");

    const idLine = document.createElement("div");
    idLine.className = "session-item-id";
    idLine.textContent = shortSessionId(s.session_id);

    const preview = document.createElement("div");
    preview.className = "session-item-preview";
    preview.textContent = s.preview || "(空会话)";

    const meta = document.createElement("div");
    meta.className = "session-item-meta";
    meta.textContent =
      formatTime(s.updated_at) +
      (s.message_count ? " · " + s.message_count + " 条" : "");

    item.append(idLine, preview, meta);
    item.addEventListener("click", () => switchSession(s.session_id));
    box.appendChild(item);
  });
}

async function loadSessionList() {
  try {
    const data = await httpJson("GET", API.sessions);
    renderSessionList(data.sessions || []);
  } catch (e) {
    /* 列表刷新失败不影响对话 */
  }
}

async function switchSession(id) {
  if (state.inFlight && state.abort) {
    state.abort.abort();
  }
  stopPolling();
  state.pendingItem = null;
  $("approval-overlay").classList.add("hidden");
  state.sessionId = id;
  $("session-id").value = id;
  saveSession(id);
  chatEl.querySelectorAll(".msg").forEach((el) => el.remove());
  viewTitle.textContent = "会话 · " + id;

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

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || state.inFlight) return;

  inputEl.value = "";
  autoResize();
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
    const data = await httpJson("POST", API.chat, body);
    if (data.session_id) {
      state.sessionId = data.session_id;
      $("session-id").value = data.session_id;
      saveSession(data.session_id);
      if (state.hasMessages) {
        viewTitle.textContent = "会话 · " + data.session_id;
      }
    }
    setThinking(false);
    appendMessage("assistant", data.reply || "(空回复)");
    setOnline(true, "服务在线");
  } catch (e) {
    setThinking(false);
    appendMessage("error", "请求失败：" + e.message);
    setOnline(false, "服务异常");
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
  state.queueCount = 0;
  state.pendingItem = null;
  $("session-id").value = "";
  saveSession("");
  chatEl.querySelectorAll(".msg").forEach((el) => el.remove());
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

  const handleNewSession = () => {
    if (state.inFlight || confirm("开始新对话？当前对话消息会被清空（历史仍在服务端）。")) {
      newSession();
    }
  };
  $("btn-new-session-top").addEventListener("click", handleNewSession);
  $("btn-new-session-side").addEventListener("click", handleNewSession);

  $("session-id").addEventListener("change", () => {
    const id = $("session-id").value.trim();
    state.sessionId = id;
    saveSession(id);
    if (id && state.hasMessages) {
      viewTitle.textContent = "会话 · " + id;
    }
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
}

async function checkHealth() {
  try {
    const data = await httpJson("GET", API.health);
    setOnline(data.ok === true, "服务在线");
  } catch (e) {
    setOnline(false, "服务离线");
  }
}

function init() {
  state.sessionId = loadSession();
  $("session-id").value = state.sessionId;
  bindEvents();
  checkHealth();
  loadSessionList();
  setInterval(checkHealth, 15000);
  inputEl.disabled = false;
  inputEl.focus();
}

init();
