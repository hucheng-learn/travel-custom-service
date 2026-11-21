"use strict";

const byId = (id) => document.getElementById(id);
const messages = byId("messages");
const scroller = messages.closest(".chat-body");
const composer = byId("message-text");
const textForm = byId("text-form");
const newChatDialog = byId("new-chat-dialog");
const previewForm = byId("preview-form");
const previewMode = true;
const previewKey = "travel-support-preview-user";
const recentUsersKey = "travel-support-recent-users";
const uuidPattern = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const ticketStatuses = { open: "待处理", in_progress: "处理中", resolved: "已解决", closed: "已关闭" };
const refundStatuses = { pending: "待处理", approved: "审核通过", rejected: "审核驳回", success: "退款完成" };
const orderStatuses = { pending_payment: "待支付", paid: "已支付", in_progress: "进行中", finished: "已完成", cancelled: "已取消" };
const productTypes = { hotel_room: "酒店住宿", scenic_ticket: "景点门票", flight_ticket: "机票", train_seat: "火车票",
  bus_seat: "汽车票", transfer: "接送服务" };
const rendered = new Set();

let userId = null;
let sessionId = null;
let sessions = [];
let sessionNextPage = 1;
let objectKind = "orders";
let objectPage = 0;
let olderCursor = null;
let confirmationId = null;
let busy = false;
let pending = null;
let requestError = "";

class ApiError extends Error {
  constructor(text, { code, retryable = false } = {}) {
    super(text);
    this.code = code;
    this.retryable = retryable;
  }
}

function responseError(data, status) {
  const error = data?.error;
  const messages = {
    UNAUTHENTICATED: previewMode ? "该用户不可用，请核对用户 ID 后再查询。" : "请从旅行平台登录后重新打开客服。",
    SESSION_NOT_FOUND: "当前对话已失效，请开始新对话。",
    TICKET_NOT_FOUND: "未找到该工单。",
    TURN_IN_PROGRESS: "客服正在处理这条消息，请稍后重试。",
  };
  const code = error?.code ?? ([401, 403].includes(status) ? "UNAUTHENTICATED" : undefined);
  const message = messages[code] || "客服暂时无法处理，请稍后再试。";
  return new ApiError(message, { code, retryable: error?.retryable ?? (status >= 500 || status === 200) });
}

async function api(path, { previewUser = userId, ...options } = {}) {
  let response;
  try {
    // 所有访问地址均支持选择测试用户，服务端继续校验用户是否有效。
    const headers = new Headers(options.headers);
    if (previewMode && previewUser) headers.set("X-Preview-User-Id", previewUser);
    response = await fetch("/api/v1/support" + path, {
      ...options, headers, credentials: "same-origin", cache: "no-store",
      signal: options.signal ?? AbortSignal.timeout(10000),
    });
  } catch {
    throw new ApiError("暂时连不上客服，请稍后重试。", { retryable: true });
  }
  if (response.ok && response.headers.get("Content-Type")?.startsWith("text/event-stream")) return response;
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) throw responseError(data, response.status);
  return data;
}

async function readReply(path, body, preview) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 35000);
  let reader;
  try {
    const response = await api(path + "/stream", {
      method: "POST", signal: controller.signal, body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    });
    reader = response.body?.getReader();
    // 一个中文字或一条事件可能被拆到多个网络块，收到完整事件后才读内容。
    const decoder = new TextDecoder();
    const parts = new Map();
    let buffer = "";
    while (reader) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const lines = buffer.slice(0, boundary.index).split(/\r?\n/);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
        const content = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
        if (!["delta", "done", "error"].includes(event) || !content) continue;
        const data = JSON.parse(content);
        if (event === "error") throw responseError(data, 0);
        if (event === "done") return data;
        parts.set(data.messageIndex, (parts.get(data.messageIndex) || "") + data.text);
        const followReply = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 35;
        preview.textContent = [...parts.values()].join("\n");
        if (followReply) scrollToLatest();
      }
    }
  } catch (error) {
    if (error instanceof ApiError) throw error;
  } finally {
    clearTimeout(timeout);
    if (reader) reader.cancel().catch(() => {});
  }
  throw new ApiError("客服回复暂未完整送达，请重试。", { retryable: true });
}

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  // 聊天内容和业务卡片都是外部数据，只作为文本插入页面。
  if (text !== undefined) element.textContent = text;
  return element;
}

function showNotice(text) {
  const notice = byId("notice");
  notice.textContent = text;
  notice.hidden = !text;
}

function showError(text) {
  byId("error").textContent = text;
  byId("request-error").hidden = !text;
}

function syncControls() {
  const unavailable = busy || !userId;
  for (const button of document.querySelectorAll(
    "[data-prompt], .message-card button, #new-chat, #older, #retry, .sessions-list button, [data-kind], " +
    "#more-sessions, #refresh-objects, #more-objects",
  )) {
    button.disabled = unavailable;
  }
  byId("preview-user").disabled = busy;
  byId("preview-submit").disabled = busy;
  composer.disabled = !userId;
  for (const actions of messages.querySelectorAll("[data-confirmation]")) {
    const isActive = actions.dataset.confirmation === confirmationId;
    actions.hidden = !isActive;
    for (const button of actions.querySelectorAll("button")) button.disabled = busy || !isActive;
  }
  byId("send").disabled = unavailable || !composer.value.trim();
  byId("retry").hidden = !pending || !requestError;
  byId("history-bar").hidden = olderCursor === null;
  byId("welcome").hidden = messages.childElementCount > 0;
  messages.hidden = !byId("welcome").hidden;
}

async function run(operation, label = "", onError = showError) {
  if (busy) return;
  busy = true;
  syncControls();
  showError("");
  showNotice(label);
  try {
    await operation();
  } catch (error) {
    onError(error instanceof ApiError ? error.message : "操作暂未完成，请稍后再试。");
  } finally {
    busy = false;
    showNotice("");
    // 读取工单或历史后，仍可重试未送达的消息。
    if (requestError && byId("request-error").hidden) showError(requestError);
    syncControls();
  }
}

function actionButton(label, onClick, className = "") {
  const button = makeElement("button", className, label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function renderCard(object, sidebar = false) {
  const { type, id, title, attributes: attrs = {} } = object;
  const card = makeElement("div", "message-card");
  const actions = makeElement("div", "card-actions");
  const titles = { order: "订单", product: "已订商品", confirmation: "确认提交", ticket: "客服工单", refund_request: "退款申请" };
  card.append(makeElement("strong", "", title || titles[type] || "服务信息"));
  if (attrs.summary) card.append(makeElement("p", "", attrs.summary));

  if (type === "order") {
    if (attrs.status) card.append(makeElement("p", "", orderStatuses[attrs.status] || "状态待确认"));
    if (attrs.payableAmount != null) card.append(makeElement("p", "", "应付 ¥" + attrs.payableAmount));
    if (attrs.paidAmount != null) {
      card.append(makeElement("p", "", `实付 ${attrs.paidAmount} ${attrs.currencyCode || ""}`.trim()));
    }
    actions.append(actionButton(sidebar ? "发送订单" : "使用此订单", () => sendOrder(id)));
  } else if (type === "product") {
    const details = [productTypes[attrs.productType], attrs.amount != null ? "预订金额 ¥" + attrs.amount : ""];
    if (details.some(Boolean)) card.append(makeElement("p", "", details.filter(Boolean).join(" · ")));
    card.append(makeElement("p", "", "关联订单 " + attrs.orderNo));
    actions.append(actionButton("发送关联订单", () => sendOrder(attrs.orderId)));
  } else if (type === "confirmation") {
    actions.dataset.confirmation = id;
    for (const [decision, label] of [["confirm", "确认提交"], ["reject", "不提交"]]) {
      actions.append(actionButton(label, () => queueMessage({ type: "object", object: { type, id, decision } }),
        decision === "confirm" ? "primary" : ""));
    }
  } else if (type === "ticket") {
    const progress = makeElement("p", "", ticketStatuses[attrs.status] || "");
    progress.setAttribute("role", "status");
    card.append(progress);
    actions.append(actionButton("查看处理进度", () => run(async () => {
      const info = await api("/tickets/" + encodeURIComponent(id));
      progress.textContent = [ticketStatuses[info.status], info.nextStep].filter(Boolean).join("\n");
    }, "正在查询工单…")));
  } else if (type === "refund_request") {
    const details = [refundStatuses[attrs.status], attrs.amount != null ? "申请金额 " + attrs.amount : ""];
    card.append(makeElement("p", "", details.filter(Boolean).join(" · ")));
    actions.append(actionButton("查看退款进度", () => queueMessage({ type: "text", text: "查退款进度 " + id })));
  }
  if (actions.childElementCount) card.append(actions);
  return card;
}

function renderUserMessage(input) {
  const entry = makeElement("article", "turn");
  const text = input.type === "text" ? input.text : (input.object.type === "confirmation"
    ? (input.object.decision === "confirm" ? "确认提交" : "不提交") : "已选择订单");
  entry.append(makeElement("p", "bubble user", text));
  return entry;
}

function renderTurn(turn, entry) {
  if (rendered.has(turn.messageId)) return null;
  rendered.add(turn.messageId);
  entry ??= renderUserMessage(turn.input);
  entry.append(makeElement("p", "turn-label", "旅行助手"));
  for (const item of turn.messages) {
    if (item.type === "text") entry.append(makeElement("p", "bubble assistant", item.text));
    if (item.type === "object") entry.append(renderCard(item.object));
  }
  return entry;
}

function scrollToLatest() {
  scroller.scrollTop = scroller.scrollHeight;
}

function sessionPath() {
  return "/sessions/" + encodeURIComponent(sessionId);
}

function rememberSession(id) {
  sessionId = id;
  // 浏览器只记住每个账号最后打开的会话，消息和会话列表由服务端按归属读取。
  writeStorage("travel-support-session:" + userId, id);
}

function showHistory(history, prepend = false) {
  const oldHeight = scroller.scrollHeight;
  const oldTop = scroller.scrollTop;
  if (!prepend) {
    rendered.clear();
    messages.replaceChildren();
  }
  const entries = history.list.map((turn) => renderTurn(turn)).filter(Boolean);
  messages.prepend(...entries);
  olderCursor = history.nextBeforeSeq ?? null;
  syncControls();
  if (prepend) scroller.scrollTop = oldTop + scroller.scrollHeight - oldHeight;
  else scrollToLatest();
}

function readStorage(key) {
  try { return sessionStorage.getItem(key); } catch { return null; }
}

function writeStorage(key, value) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch { /* 禁用浏览器存储时仍可继续使用。 */ }
}

function validUserId(id) {
  return typeof id === "string" && /^[0-9]{1,19}$/.test(id) && BigInt(id) > 0n && BigInt(id) < 2n ** 63n;
}

function recentUsers() {
  try { return JSON.parse(readStorage(recentUsersKey) || "[]").filter(validUserId).slice(0, 10); }
  catch { return []; }
}

function showRecentUsers() {
  byId("recent-users").replaceChildren(...recentUsers().map((id) => {
    const option = makeElement("option");
    option.value = id;
    return option;
  }));
}

function showPreviewStatus(text, isError = false) {
  const status = byId("preview-status");
  status.textContent = text;
  status.toggleAttribute("data-error", isError);
}

function renderSessions() {
  const dateFormat = new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  byId("sessions-list").replaceChildren(...sessions.map((session) => {
    const button = actionButton("", () => {
      if (session.sessionId !== sessionId) run(() => openSession(session.sessionId), "正在读取历史消息…");
    });
    button.setAttribute("aria-current", String(session.sessionId === sessionId));
    button.append(makeElement("span", "session-title", session.title));
    const updated = new Date(session.updatedAt);
    if (!Number.isNaN(updated.valueOf())) button.append(makeElement("span", "session-time", dateFormat.format(updated)));
    return button;
  }));
}

async function loadSessions(page = 1) {
  const status = byId("sessions-status");
  const more = byId("more-sessions");
  status.textContent = "正在读取会话…";
  sessionNextPage = page;
  try {
    const result = await api("/sessions?pageNo=" + page);
    // 如果其他标签页刚更新过历史，翻页结果可能重复，合并时按会话 ID 去重。
    const rows = page === 1 ? result.list : [...sessions, ...result.list];
    sessions = [...new Map(rows.map((row) => [row.sessionId, row])).values()];
    sessionNextPage = page + 1;
    renderSessions();
    status.textContent = sessions.length ? "" : "暂无会话。发送消息或点击新对话开始。";
    more.textContent = "更多会话";
    more.hidden = !result.hasMore;
  } catch (error) {
    status.textContent = error.message;
    more.textContent = "重试";
    more.hidden = false;
  }
}

function recordSession(summary) {
  sessions = [summary, ...sessions.filter((item) => item.sessionId !== summary.sessionId)];
  byId("sessions-status").textContent = "";
  renderSessions();
}

function startSession(session) {
  rememberSession(session.sessionId);
  recordSession({ sessionId, title: "新对话", updatedAt: session.createdAt });
}

async function openSession(id) {
  const history = await api("/sessions/" + encodeURIComponent(id) + "/messages");
  resetConversation();
  rememberSession(id);
  showHistory(history);
  renderSessions();
  await readConfirmation();
}

function selectObjectKind(kind) {
  objectKind = kind;
  objectPage = 0;
  byId("objects-list").replaceChildren();
  byId("more-objects").hidden = true;
  for (const button of document.querySelectorAll("[data-kind]")) {
    button.setAttribute("aria-pressed", String(button.dataset.kind === kind));
  }
}

async function loadObjects(page = 1) {
  const list = byId("objects-list");
  const status = byId("objects-status");
  status.textContent = "正在查询卡片…";
  try {
    const response = await api(`/objects/${objectKind}?pageNo=${page}`);
    const cards = response.list.map((item) => renderCard(item, true));
    if (page === 1) list.replaceChildren(...cards);
    else list.append(...cards);
    objectPage = page;
    byId("more-objects").hidden = !response.hasMore;
    status.textContent = list.childElementCount ? "" : "当前用户暂无这类卡片。";
  } catch (error) {
    status.textContent = error.message;
  }
}

async function selectUser(previewUser) {
  const identity = await api("/me", { previewUser });
  const changed = userId !== identity.userId;

  if (changed) {
    // 先清除上一位用户的消息、重试请求和卡片，再加载新身份对应的内容。
    resetConversation();
    userId = identity.userId;
    sessions = [];
    sessionNextPage = 1;
    byId("more-sessions").hidden = true;
    renderSessions();
    selectObjectKind("orders");
  }
  if (previewMode) {
    byId("preview-user").value = userId;
    writeStorage(previewKey, userId);
    writeStorage(recentUsersKey, JSON.stringify([userId, ...recentUsers().filter((id) => id !== userId)].slice(0, 10)));
    showRecentUsers();
    showPreviewStatus("当前用户 " + userId);
  }
  await Promise.all([loadSessions(), loadObjects()]);
  if (changed) await restoreConversation();
}

async function readConfirmation() {
  try {
    const snapshot = await api(sessionPath() + "/state");
    confirmationId = snapshot.activeTask?.pendingConfirmationId ?? null;
  } catch {
    // 状态暂不可用时仍展示回复，确认入口等服务端返回有效的确认 ID 再显示。
  }
}

function newMessageId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // 内网 HTTP 可能不提供 randomUUID，仍使用浏览器随机数生成请求标识。
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

function queueMessage(input) {
  if (busy || !userId) return;
  // 按回车重发相同内容时也复用消息 ID，防止一次确认变成两次写操作。
  const key = JSON.stringify(input);
  if (pending?.key !== key) pending = { key, body: { ...input, messageId: newMessageId() } };
  sendPending();
}

function sendOrder(id) {
  queueMessage({ type: "object", object: { type: "order", id: String(id) } });
}

function sendPending() {
  if (pending) run(() => deliver(pending.body));
}

async function deliver(body) {
  const entry = renderUserMessage(body);
  const preview = makeElement("p", "bubble assistant", "旅行助手正在处理…");
  preview.setAttribute("aria-live", "off");
  entry.append(preview);
  messages.append(entry);
  syncControls();
  scrollToLatest();

  requestError = "";
  if (body.type === "text" && composer.value.trim() === body.text) composer.value = "";
  let reply;
  try {
    if (!sessionId) {
      const session = await api("/sessions", { method: "POST" });
      startSession(session);
    }
    reply = await readReply(sessionPath() + "/messages", body, preview);
  } catch (error) {
    entry.remove();
    if (body.type === "text" && !composer.value) composer.value = body.text;
    if (error.retryable) requestError = error.message;
    else pending = null;
    throw error;
  }

  // 只在服务端确认轮次完成后展示业务卡片，避免流中断时给出未落库的操作。
  pending = null;
  preview.remove();
  const previousConfirmation = confirmationId;
  confirmationId = reply.messages.find((item) => item.object?.type === "confirmation")?.object.id ?? null;
  renderTurn({ ...reply, input: body }, entry);
  const current = sessions.find((item) => item.sessionId === sessionId);
  if (current) {
    const title = rendered.size === 1 && olderCursor === null
      ? (body.type === "text" ? Array.from(body.text.trim().replace(/\s+/g, " ")).slice(0, 60).join("") : "订单咨询")
      : current.title;
    recordSession({ sessionId, title, updatedAt: new Date().toISOString() });
  }
  scrollToLatest();
  // 没有新确认卡时，只有上一轮还有待确认事项才需要查它是否有效。
  if (previousConfirmation && !confirmationId) await readConfirmation();
}

function resetConversation() {
  sessionId = null;
  confirmationId = null;
  pending = null;
  requestError = "";
  composer.value = "";
  showError("");
  showHistory({ list: [] });
}

async function startNewChat() {
  const session = await api("/sessions", { method: "POST" });
  resetConversation();
  startSession(session);
  composer.focus();
}

byId("new-chat").addEventListener("click", () => {
  if (!pending && !composer.value.trim()) {
    run(startNewChat, "正在创建新对话…");
    return;
  }
  newChatDialog.returnValue = "cancel";
  newChatDialog.showModal();
});
newChatDialog.addEventListener("close", () => {
  if (newChatDialog.returnValue === "start") run(startNewChat, "正在创建新对话…");
});
byId("more-sessions").addEventListener("click", () => run(() => loadSessions(sessionNextPage)));
byId("refresh-objects").addEventListener("click", () => run(() => loadObjects()));
byId("more-objects").addEventListener("click", () => run(() => loadObjects(objectPage + 1)));
for (const button of document.querySelectorAll("[data-kind]")) {
  button.addEventListener("click", () => run(async () => {
    if (button.dataset.kind !== objectKind) selectObjectKind(button.dataset.kind);
    await loadObjects();
  }));
}
previewForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const id = byId("preview-user").value.trim();
  if (!validUserId(id)) {
    showPreviewStatus("请输入有效的用户 ID。", true);
    return;
  }
  showPreviewStatus("正在确认用户…");
  run(() => selectUser(BigInt(id).toString()), "", (message) => {
    showPreviewStatus(userId ? `${message} 当前仍为用户 ${userId}。` : message, true);
  });
});
byId("retry").addEventListener("click", sendPending);
byId("older").addEventListener("click", () => run(async () => {
  const history = await api(sessionPath() + "/messages?beforeSeq=" + olderCursor);
  showHistory(history, true);
}, "正在读取历史消息…"));
textForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = composer.value.trim();
  if (text) queueMessage({ type: "text", text });
});
composer.addEventListener("input", syncControls);
composer.addEventListener("keydown", (event) => {
  // 中文输入法确认候选词时也可能按回车，等确认结束后再发送。
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!busy && composer.value.trim()) textForm.requestSubmit();
  }
});
for (const button of document.querySelectorAll("[data-prompt]")) {
  button.addEventListener("click", () => {
    composer.value = button.dataset.prompt;
    button.closest("details")?.removeAttribute("open");
    syncControls();
    composer.focus();
  });
}

async function restoreConversation() {
  const saved = readStorage("travel-support-session:" + userId);
  const latest = sessions[0]?.sessionId;
  const id = uuidPattern.test(saved || "") ? saved : latest;
  if (id) {
    try {
      await openSession(id);
    } catch (error) {
      if (error.code !== "SESSION_NOT_FOUND") {
        showError(error.message);
        return;
      }
      rememberSession(null);
      if (latest && latest !== id) await openSession(latest);
    }
  }
}

async function initialize() {
  // 用户选择不依赖访问地址，局域网变化后无需重新配置。
  previewForm.hidden = !previewMode;
  byId("sessions-status").textContent = previewMode ? "选择用户后查看历史会话。" : "";
  byId("objects-status").textContent = previewMode ? "选择用户并查询后显示卡片。" : "";
  showRecentUsers();
  syncControls();
  const savedUser = readStorage(previewKey);
  if (previewMode && validUserId(savedUser)) {
    byId("preview-user").value = savedUser;
    await selectUser(savedUser);
  } else if (!previewMode) {
    await selectUser(null);
  }
}

syncControls();
run(initialize, "正在连接客服…");
