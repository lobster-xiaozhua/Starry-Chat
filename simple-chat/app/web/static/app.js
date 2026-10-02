// app/web/static/app.js
// 单文件、无构建、无框架、原生 ES Module。
//
// 设计要点（与 CODEBUDDY.md 契约一致）：
// - POST /api/chat 返回 text/event-stream，事件序列 token* -> done / error
// - event: token  -> {"delta":"..."}
// - event: done   -> {"conversation_id","message_id","usage":{...}}
// - event: error  -> {"code","message"}  （扁平，无 error 外壳）
// - 流开始前的错误用 HTTP 状态码 + {"error":{"code","message"}}
//
// 流式渲染策略：
// - 流式进行中：仅向最后一个 assistant 气泡的纯文本节点追加 delta（textContent），
//   绝不整段重绘，绝不半截 markdown -> html。
// - 流式结束后：把累积文本一次性 marked.parse + escape，再渲染整段气泡。
// - 用户输入永远先 escape 再拼接到 DOM；markdown 只用于 assistant 最终态。

const API_BASE = "/api/chat";

// ───────────────────────── DOM 引用 ─────────────────────────
const $ = (id) => document.getElementById(id);
const sidebarEl = $("sidebar");
const listEl = $("list");
const streamEl = $("stream");          // 消息流容器（.stream）
const messagesEl = $("messages");       // 滚动视口
const inputEl = $("input");
const sendBtn = $("send");
const stopBtn = $("stop");
const scrollBtn = $("scroll-btn");
const scrollBadge = $("scroll-badge");
const modelLabel = $("model-label");
const btnNew = $("btn-new");
const btnClear = $("btn-clear");
const sidebarToggle = $("sidebar-toggle");
const scrim = $("scrim");

// ───────────────────────── 运行时状态 ─────────────────────────
const state = {
  conversationId: null,
  sending: false,                  // 流式进行中？
  conversations: [],               // 侧边栏缓存
  modelName: "",
  lastUserText: "",                // 用于“重试”
  lastAssistantEl: null,           // 当前流式写入的 assistant 气泡
  controller: null,                // AbortController
  newMessageCount: 0,              // 用户未读（已自动滚动则清零）
};

// 中文输入法 composing 状态：期间 Enter 不触发发送
let composing = false;

// ───────────────────────── 工具：转义 / DOM ─────────────────────────
const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === "html") node.innerHTML = v;
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(c));
  }
  return node;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// marked 仅在流式结束后使用一次；缺则退化为纯文本。
// 注意：marked.parse 输出的 HTML 会在 finalizeAssistantHtml 中统一清洗
// （移除 <script>/on* 事件属性等），用户输入绝不直接 innerHTML。
function renderMarkdown(text) {
  if (window.marked) {
    try { return window.marked.parse(text, { breaks: true, gfm: true }); }
    catch { /* 落到下面手动渲染 */ }
  }
  // 无 marked 时：纯文本 + 转义，至少保留换行
  return `<p>${escapeHtml(text).replace(/\n/g, "<br>")}</p>`;
}

// 在最终 HTML 上做最小加固（无 DOMPurify，仅这一处手动清洗）：
// - 移除 <script> / <style> / <iframe> 等可执行/可注入节点
// - 剥离 on* 事件属性、javascript: 与 data: 链接
// - 代码块包裹 .code-wrap + 复制按钮
// - 外链加 rel/target
function finalizeAssistantHtml(container, html) {
  // 0. XSS 防护：优先 DOMPurify 一次性净化（剥离 script/style/on*/javascript:/data: 等）；
  //    加载失败则回退到手动剥离危险节点与事件属性。无论如何，用户输入都不会直接 innerHTML。
  if (window.DOMPurify) {
    container.innerHTML = window.DOMPurify.sanitize(html, {
      FORBID_TAGS: ["style", "iframe", "object", "embed", "link", "meta", "form"],
      FORBID_ATTR: ["style"],
      ADD_ATTR: ["target"],
    });
  } else {
    container.innerHTML = html;
    container
      .querySelectorAll("script, style, iframe, object, embed, link, meta, form")
      .forEach((n) => n.remove());
    container.querySelectorAll("*").forEach((node) => {
      for (const attr of [...node.attributes]) {
        const name = attr.name.toLowerCase();
        const val = attr.value || "";
        if (name.startsWith("on")) node.removeAttribute(attr.name);
        if ((name === "href" || name === "src") && /^\s*(javascript|data)\s*:/i.test(val)) {
          node.removeAttribute(attr.name);
        }
      }
    });
  }

  // 1. 代码块：marked 输出 <pre><code>，外层包 .code-wrap + 复制按钮
  container.querySelectorAll("pre").forEach((pre) => {
    if (pre.parentElement.classList.contains("code-wrap")) return;
    const wrap = el("div", { class: "code-wrap" });
    pre.replaceWith(wrap);
    wrap.appendChild(pre);
    const btn = el("button", { class: "copy-btn", type: "button", "aria-label": "复制代码" }, "复制");
    btn.addEventListener("click", () => {
      const code = pre.querySelector("code")?.innerText ?? pre.innerText;
      navigator.clipboard?.writeText(code).then(
        () => {
          btn.textContent = "已复制";
          btn.classList.add("copied");
          setTimeout(() => { btn.textContent = "复制"; btn.classList.remove("copied"); }, 1500);
        },
        () => { btn.textContent = "复制失败"; setTimeout(() => { btn.textContent = "复制"; }, 1500); }
      );
    });
    wrap.appendChild(btn);
  });

  // 2. 外链安全加固
  container.querySelectorAll("a").forEach((a) => {
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noopener noreferrer");
  });
}

// ───────────────────────── API 封装 ─────────────────────────
// 与后端契约对齐：错误体恒为 {"error":{"code","message",...}}

async function readErrorBody(res) {
  // 先读文本再解析，避免 body 已被消费
  const text = await res.text().catch(() => "");
  if (!text) return { code: "HTTP_ERROR", message: `HTTP ${res.status}` };
  try {
    const data = JSON.parse(text);
    const err = data?.error ?? data; // 兼容扁平 error 事件
    return { code: err.code ?? "HTTP_ERROR", message: err.message ?? `HTTP ${res.status}` };
  } catch {
    return { code: "HTTP_ERROR", message: text.slice(0, 200) || `HTTP ${res.status}` };
  }
}

async function loadConversations() {
  const res = await fetch(`${API_BASE}/conversations`, { headers: { Accept: "application/json" } });
  if (res.status === 204) return [];
  const data = await res.json();
  if (!res.ok) throw new Error(data?.error?.message || `HTTP ${res.status}`);
  return data.conversations || [];
}

async function loadMessages(conversationId) {
  const res = await fetch(`${API_BASE}/conversations/${conversationId}/messages`);
  const data = await res.json();
  if (!res.ok) throw new Error(data?.error?.message || `HTTP ${res.status}`);
  return data.messages || [];
}

async function deleteConversation(id) {
  const res = await fetch(`${API_BASE}/conversations/${id}`, { method: "DELETE" });
  if (res.status === 204) return;
  // 有错误体则解析
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.error?.message || `HTTP ${res.status}`);
}

/**
 * 发送消息，返回 ReadableStream（用于 SSE 解析）。
 * @param {string|null} conversationId  null 表示新建会话
 * @param {string} message
 * @param {{ signal?: AbortSignal }} [opts]
 * @returns {Promise<ReadableStream<Uint8Array>>}
 */
async function sendMessage(conversationId, message, { signal } = {}) {
  const res = await fetch(`${API_BASE}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ message, conversation_id: conversationId ?? undefined, stream: true }),
    signal,
  });
  if (!res.ok) {
    const err = await readErrorBody(res);
    const e = new Error(err.message); e.code = err.code; e.httpStatus = res.status;
    throw e;
  }
  if (!res.body) throw new Error("服务端未返回流式响应");
  return res.body;
}

// ───────────────────────── SSE 解析 ─────────────────────────
// 按 \n\n 分帧，保留末尾未完整片段以应对 chunk 边界截断
function splitFrames(buffer) {
  const frames = buffer.split("\n\n");
  return { frames: frames.slice(0, -1), rest: frames[frames.length - 1] };
}

function parseFrame(frame) {
  let event = "message";
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue;          // SSE comment / heartbeat
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return null;
  let data = {};
  try { data = JSON.parse(dataLines.join("\n")); }
  catch { return null; }                          // 忽略无法解析的帧
  return { event, data };
}

// ───────────────────────── 渲染：消息气泡 ─────────────────────────
function appendUserBubble(text) {
  const node = el("div", { class: "msg user", role: "article" });
  // 用户输入永远先 escape（虽然 textContent 已安全，保持一致）
  node.textContent = text;
  streamEl.appendChild(node);
  maybeScrollToBottom();
  return node;
}

function appendAssistantBubble() {
  // 流式占位 typing dots
  const dots = el("div", { class: "typing-dots", "aria-hidden": "true" },
    el("span"), el("span"), el("span"));
  const node = el("div", { class: "msg assistant streaming", role: "article" }, dots);
  streamEl.appendChild(node);
  state.lastAssistantEl = node;
  maybeScrollToBottom();
  return node;
}

function appendErrorBar(message, onRetry) {
  const bar = el("div", { class: "err-bar", role: "alert" },
    el("span", { class: "err-text" }, message));
  if (onRetry) {
    const retry = el("button", { class: "retry", type: "button", "aria-label": "重试上一次请求" }, "重试");
    retry.addEventListener("click", onRetry);
    bar.appendChild(retry);
  }
  streamEl.appendChild(bar);
  maybeScrollToBottom();
  return bar;
}

// 流式期间：把 delta 追加到最后一个 assistant 气泡的纯文本节点
function appendDelta(delta) {
  const node = state.lastAssistantEl;
  if (!node) return;
  // 首个 delta：去掉 typing dots，建立文本节点
  if (node.classList.contains("streaming")) {
    const dots = node.querySelector(".typing-dots");
    if (dots) dots.remove();
  }
  // 直接拼到 textContent（性能：不触发整段重绘，DOM 仅追加文本）
  // 用一个专用的文本节点持续 append，避免读取大字符串
  if (!node._textNode) {
    node._textNode = document.createTextNode("");
    node.appendChild(node._textNode);
  }
  node._textNode.data += delta;
  maybeScrollToBottom();
}

function finalizeAssistantBubble() {
  const node = state.lastAssistantEl;
  if (!node) return;
  const text = node._textNode ? node._textNode.data : "";
  node.classList.remove("streaming");
  // 流式结束后一次性 markdown -> html + 安全加固
  const html = renderMarkdown(text);
  finalizeAssistantHtml(node, html);
  // 清理文本节点引用（已 innerHTML 重置）
  node._textNode = null;
  maybeScrollToBottom();
}

// ───────────────────────── 渲染：历史消息 ─────────────────────────
function renderMessage(m) {
  if (m.role === "system") return null; // 不展示 system
  const node = el("div", { class: `msg ${m.role}`, role: "article" });
  if (m.role === "assistant") {
    // 历史消息直接 markdown 渲染（已是完整内容）
    finalizeAssistantHtml(node, renderMarkdown(m.content || ""));
  } else {
    node.textContent = m.content || "";
  }
  return node;
}

function clearStream() { streamEl.innerHTML = ""; }

// ───────────────────────── 空状态 ─────────────────────────
const SAMPLE_QUESTIONS = [
  { q: "用一句话介绍 FastAPI", hint: "了解框架定位" },
  { q: "写一个 Python 函数：判断回文数", hint: "代码块会带复制按钮" },
  { q: "解释 SSE 与 WebSocket 的区别", hint: "对比流式方案" },
];

function renderEmptyApp() {
  clearStream();
  const wrap = el("div", { class: "empty-state" });
  const brand = el("div", { class: "brand", "aria-hidden": "true" });
  brand.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  wrap.appendChild(brand);
  wrap.appendChild(el("h1", {}, "Simple Chat"));
  wrap.appendChild(el("p", {}, "一个最小可用的流式对话服务。发送一条消息开始对话，或试试下面的示例。"));
  const ex = el("div", { class: "examples" });
  for (const item of SAMPLE_QUESTIONS) {
    const card = el("button", { type: "button", class: "example" });
    card.appendChild(el("div", { class: "q" }, item.q));
    card.appendChild(el("div", { class: "hint" }, item.hint));
    card.addEventListener("click", () => {
      inputEl.value = item.q;
      autoGrow();
      send();
    });
    ex.appendChild(card);
  }
  wrap.appendChild(ex);
  streamEl.appendChild(wrap);
}

function renderEmptyConversation() {
  clearStream();
  streamEl.appendChild(el("div", { class: "placeholder" }, "开始对话"));
}

function isEmpty() {
  // 当前视图是空状态/占位（非消息气泡）
  return ![...streamEl.children].some((c) => c.classList?.contains("msg"));
}

// ───────────────────────── 滚动控制 ─────────────────────────
const NEAR_BOTTOM_THRESHOLD = 80; // 阈值 80px

function isNearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight <= NEAR_BOTTOM_THRESHOLD;
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function maybeScrollToBottom() {
  if (isNearBottom()) {
    scrollToBottom();
    state.newMessageCount = 0;
    hideScrollButton();
  } else {
    // 用户不在底部，流式期间累积新消息计数
    state.newMessageCount += 1;
    showScrollButton();
  }
}

function showScrollButton() {
  scrollBtn.classList.add("show");
  if (state.newMessageCount > 0) {
    scrollBadge.textContent = String(state.newMessageCount);
    scrollBtn.classList.add("has-badge");
  }
}
function hideScrollButton() {
  scrollBtn.classList.remove("show", "has-badge");
  state.newMessageCount = 0;
}

scrollBtn.addEventListener("click", () => {
  scrollToBottom();
  hideScrollButton();
});

messagesEl.addEventListener("scroll", () => {
  if (isNearBottom()) hideScrollButton();
});

// ───────────────────────── 侧边栏 ─────────────────────────
function renderSidebar() {
  listEl.innerHTML = "";
  if (!state.conversations.length) {
    listEl.appendChild(el("div", { class: "sidebar-empty" }, "暂无会话\n点击“新建对话”开始"));
    return;
  }
  for (const c of state.conversations) {
    const row = el("div", {
      class: "item" + (c.id === state.conversationId ? " active" : ""),
      tabindex: "0",
      role: "button",
      "aria-label": `打开会话 ${c.title || "新对话"}`,
    });
    row.appendChild(el("span", { class: "title" }, c.title || "新对话"));
    const del = el("button", {
      class: "del", type: "button", "aria-label": `删除会话 ${c.title || "新对话"}`,
      title: "删除会话",
    });
    del.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await deleteConversation(c.id);
        if (c.id === state.conversationId) startNew();
        await refreshSidebar();
      } catch (err) {
        toast(err.message || "删除失败");
      }
    });
    row.appendChild(del);
    row.addEventListener("click", () => openConversation(c.id));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openConversation(c.id); }
    });
    listEl.appendChild(row);
  }
}

async function refreshSidebar() {
  try {
    state.conversations = await loadConversations();
    renderSidebar();
  } catch (err) {
    toast(err.message || "会话列表加载失败");
  }
}

// ───────────────────────── 会话切换 ─────────────────────────
async function openConversation(id) {
  if (state.sending) return; // 流式进行中禁止切换
  state.conversationId = id;
  clearStream();
  renderSidebar(); // 先刷新高亮，避免感知延迟
  try {
    const messages = await loadMessages(id);
    if (!messages.length) {
      renderEmptyConversation();
      return;
    }
    for (const m of messages) {
      const node = renderMessage(m);
      if (node) streamEl.appendChild(node);
    }
    requestAnimationFrame(scrollToBottom);
  } catch (err) {
    appendErrorBar(err.message || "历史消息加载失败", () => openConversation(id));
  }
  closeSidebarDrawer();
  inputEl.focus();
}

function startNew() {
  if (state.sending) return;
  state.conversationId = null;
  clearStream();
  renderEmptyApp();
  renderSidebar();
  inputEl.focus();
  closeSidebarDrawer();
}

// ───────────────────────── 模型名（底部） ─────────────────────────
function loadModelName() {
  // 配置层未单独暴露模型端点；从 /static 元数据或 /api/chat 推断都不可行，
  // 此处用服务端注入的 <meta name="llm-model">；缺失则退化为占位文本。
  const meta = document.querySelector('meta[name="llm-model"]');
  state.modelName = meta?.content || "Simple Chat";
  modelLabel.textContent = state.modelName;
  modelLabel.title = state.modelName;
}

// ───────────────────────── 发送主流程 ─────────────────────────
function setSending(v) {
  state.sending = v;
  inputEl.disabled = v;
  sendBtn.hidden = v;
  stopBtn.hidden = !v;
  if (!v) { inputEl.focus(); }
}

async function send() {
  if (state.sending || composing) return;
  const text = inputEl.value.trim();
  if (!text) return;
  if (text.length > 4000) { toast("消息长度不能超过 4000 字符"); return; }

  // 若当前在空状态/占位，先清掉
  if (isEmpty()) clearStream();

  state.lastUserText = text;
  inputEl.value = "";
  inputEl.style.height = "auto";
  setSending(true);

  appendUserBubble(text);
  appendAssistantBubble();

  const wasNew = state.conversationId === null;
  state.controller = new AbortController();

  try {
    const stream = await sendMessage(state.conversationId, text, { signal: state.controller.signal });
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = splitFrames(buffer);
      buffer = rest;
      for (const frame of frames) {
        const parsed = parseFrame(frame);
        if (!parsed) continue;
        if (parsed.event === "token") {
          appendDelta(parsed.data.delta || "");
        } else if (parsed.event === "done") {
          if (parsed.data.conversation_id) state.conversationId = parsed.data.conversation_id;
        } else if (parsed.event === "error") {
          // 带内错误：在消息流末尾追加红色错误条 + 重试
          finalizeAbortedAssistant();
          appendErrorBar(parsed.data.message || "生成失败", () => retry());
          return;
        }
      }
    }
    // 流正常结束：一次性 markdown 渲染
    finalizeAssistantBubble();
  } catch (err) {
    handleSendError(err);
  } finally {
    setSending(false);
    state.controller = null;
    if (wasNew || true) refreshSidebar(); // 新建会话后侧边栏需刷新标题
  }
}

function handleSendError(err) {
  // AbortController 主动中止：不当作错误展示
  if (err?.name === "AbortError") {
    finalizeAbortedAssistant();
    return;
  }
  // 网络断开（fetch 抛 TypeError）
  if (err?.name === "TypeError" || /Failed to fetch|NetworkError/i.test(err.message)) {
    finalizeAbortedAssistant();
    appendErrorBar("连接已中断，请重试", () => retry());
    return;
  }
  // 429 / 限流
  if (err?.httpStatus === 429 || err?.code === "RATE_LIMITED") {
    finalizeAbortedAssistant();
    appendErrorBar("请求过于频繁，请稍后再试", () => retry());
    return;
  }
  // 其它：用错误体 message
  finalizeAbortedAssistant();
  appendErrorBar(err.message || "发送失败", () => retry());
}

// 主动停止 / 异常时把流式气泡收尾成已有文本（去掉 typing dots）
function finalizeAbortedAssistant() {
  const node = state.lastAssistantEl;
  if (!node) return;
  const hasText = node._textNode && node._textNode.data;
  if (!hasText) {
    // 没有任何 token：移除空 assistant 气泡
    node.remove();
    state.lastAssistantEl = null;
    return;
  }
  finalizeAssistantBubble();
}

// 重试：用上一次的用户输入重新发起（保留 conversationId）
function retry() {
  if (state.sending) return;
  if (!state.lastUserText) return;
  // 清理上一次失败残留：末尾的错误条 + assistant 气泡 + user 气泡，
  // 再由 send() 重新追加，避免重复堆叠。
  const kids = [...streamEl.children];
  for (let i = kids.length - 1; i >= 0 && i >= kids.length - 3; i--) {
    const k = kids[i];
    if (k.classList.contains("err-bar") || k.classList.contains("msg")) k.remove();
    else break;
  }
  state.lastAssistantEl = null;
  inputEl.value = state.lastUserText;
  autoGrow();
  send();
}

// 停止生成
stopBtn.addEventListener("click", () => {
  if (state.controller) state.controller.abort();
});

// ───────────────────────── 输入交互 ─────────────────────────
function autoGrow() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
}

inputEl.addEventListener("input", autoGrow);

// 中文输入法 composing 期间 Enter 不触发发送
inputEl.addEventListener("compositionstart", () => { composing = true; });
inputEl.addEventListener("compositionend", () => { composing = false; autoGrow(); });

inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !composing) {
    e.preventDefault();
    send();
  }
});

sendBtn.addEventListener("click", send);
btnNew.addEventListener("click", startNew);

btnClear.addEventListener("click", async () => {
  if (!state.conversations.length) return;
  if (!confirm("确定清空所有会话吗？此操作不可撤销。")) return;
  // 后端无“批量清空”端点；逐个删除（量小可接受）
  try {
    await Promise.all(state.conversations.map((c) => deleteConversation(c.id)));
    await refreshSidebar();
    startNew();
    toast("已清空所有会话");
  } catch (err) {
    toast(err.message || "清空失败");
    refreshSidebar();
  }
});

// 移动端侧边栏抽屉
function openSidebarDrawer() {
  sidebarEl.classList.add("open");
  scrim.classList.add("show");
  sidebarToggle.setAttribute("aria-expanded", "true");
}
function closeSidebarDrawer() {
  sidebarEl.classList.remove("open");
  scrim.classList.remove("show");
  sidebarToggle.setAttribute("aria-expanded", "false");
}
sidebarToggle.addEventListener("click", () => {
  if (sidebarEl.classList.contains("open")) closeSidebarDrawer();
  else openSidebarDrawer();
});
scrim.addEventListener("click", closeSidebarDrawer);

// ───────────────────────── 轻量 toast ─────────────────────────
let toastTimer = null;
function toast(message) {
  let t = document.getElementById("__toast");
  if (!t) {
    t = el("div", { id: "__toast", role: "status", "aria-live": "polite" });
    t.style.cssText = [
      "position:fixed", "left:50%", "bottom:24px", "transform:translateX(-50%)",
      "background:#0f172a", "color:#fff", "padding:8px 14px", "border-radius:8px",
      "font-size:13px", "z-index:9999", "box-shadow:0 4px 12px rgba(0,0,0,.2)",
      "opacity:0", "transition:opacity .2s ease", "pointer-events:none", "max-width:80vw",
    ].map((s) => s + ";").join("");
    document.body.appendChild(t);
  }
  t.textContent = message;
  t.style.opacity = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.style.opacity = "0"; }, 2400);
}

// ───────────────────────── 侧边栏持久化（仅展开状态） ─────────────────────────
// 不缓存消息（以服务端为准），仅缓存 sidebar 抽屉的展开偏好（移动端）
function loadSidebarPref() {
  try {
    if (localStorage.getItem("sc:sidebar") === "open") openSidebarDrawer();
  } catch { /* ignore */ }
}
function bindSidebarPref() {
  const obs = () => {
    try { localStorage.setItem("sc:sidebar", sidebarEl.classList.contains("open") ? "open" : "closed"); }
    catch { /* ignore */ }
  };
  sidebarToggle.addEventListener("click", obs);
  scrim.addEventListener("click", obs);
}

// ───────────────────────── 启动 ─────────────────────────
async function boot() {
  loadModelName();
  bindSidebarPref();
  renderEmptyApp();
  await refreshSidebar();
  loadSidebarPref();
  inputEl.focus();
}

boot();
