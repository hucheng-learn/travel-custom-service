"use strict";

const byId = (id) => document.getElementById(id);
const messages = byId("messages");
const scroller = messages.closest(".chat-body");
const composer = byId("message-text");
const sessionForm = byId("session-form");
const textForm = byId("text-form");
const flowNames = {
  order_status_query: "订单与支付查询",
  itinerary_query: "订单行程查询",
  itinerary_date_query: "按日期查行程",
  product_query: "商品咨询",
  refund_request: "退款申请",
  ticket_submit: "客服工单",
};
const routeNames = { task: "任务", clarify: "澄清", knowledge: "知识", chitchat: "闲聊" };
const storageKey = "travel-demo-session";
const uuidPattern = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

let current = null;
let busy = false;
let olderCursor = null;
let pending = null;
let streamController = null;
const rendered = new Set();

class ApiError extends Error {
  constructor(message, retryable = false) {
    super(message);
    this.retryable = retryable;
  }
}

function responseError(data, status) {
  const error = data?.error;
  const details = [error?.code, error?.traceId && "追踪号 " + error.traceId].filter(Boolean).join(" · ");
  const message = error?.message || `服务未返回预期结果（HTTP ${status}）。`;
  return new ApiError(details ? message + " " + details : message, error?.retryable || status >= 500 || status === 200);
}

async function api(path, userId, { method = "GET", body } = {}) {
  const headers = { "X-User-Id": userId };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch("/api/v1/support" + path, {
      method, headers, signal: AbortSignal.timeout(35000),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("没收到服务响应。若发送的是消息，可以重试上一条。", true);
  }
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) {
    throw responseError(data, response.status);
  }
  return data;
}

async function readStream(path, userId, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 35000);
  const parts = new Map();
  let reader;
  streamController = controller;
  syncControls();
  try {
    const response = await fetch("/api/v1/support" + path + "/stream", {
      method: "POST", signal: controller.signal, body: JSON.stringify(body),
      headers: {"X-User-Id": userId, "Content-Type": "application/json", Accept: "text/event-stream"},
    });
    if (!response.ok) throw responseError(await response.json().catch(() => null), response.status);
    reader = response.body?.getReader();
    if (!reader) throw new ApiError("浏览器未提供可读响应流。", true);
    // 浏览器可能把一个中文字或一条事件切到不同网络块，用增量解码拼回完整内容。
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) throw new ApiError("响应流结束但没有收到最终结果；请重试上一条。", true);
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const lines = buffer.slice(0, boundary.index).split(/\r?\n/);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
        const content = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
        if (!event || !content) continue; // 心跳注释不用展示。
        const data = JSON.parse(content);
        if (event === "error") throw responseError(data, 0);
        if (event === "done") return data;
        if (event === "delta") {
          parts.set(data.messageIndex, (parts.get(data.messageIndex) || "") + data.text);
          changeStatus("stream-preview", [...parts.values()].join("\n"));
        }
      }
    }
  } catch (error) {
    if (error instanceof ApiError) throw error;
    const message = controller.signal.aborted ? "已断开响应流；可以重试上一条。" : "未能读取完整响应；请重试上一条。";
    throw new ApiError(message, true);
  } finally {
    clearTimeout(timeout);
    if (reader) reader.cancel().catch(() => {});
    streamController = null;
    changeStatus("stream-preview", "");
    syncControls();
  }
}

function changeStatus(id, text) {
  const element = byId(id);
  element.textContent = text;
  element.hidden = !text;
}

function syncControls() {
  for (const button of document.querySelectorAll("button")) button.disabled = busy;
  for (const id of ["refresh", "retry", "older"]) byId(id).disabled = busy || !current;
  byId("retry").hidden = !pending;
  byId("older").hidden = olderCursor === null;
  byId("disconnect").hidden = !streamController;
  byId("disconnect").disabled = !streamController;
  byId("stream-mode").disabled = busy;
}

async function run(label, operation) {
  if (busy) return;
  busy = true;
  syncControls();
  changeStatus("notice", label);
  changeStatus("error", "");
  try {
    await operation();
  } catch (error) {
    changeStatus("error", error.message || "操作失败，请稍后重试。");
  } finally {
    busy = false;
    changeStatus("notice", "");
    syncControls();
  }
}

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  // 回复、订单名等都是数据，不用 innerHTML 拼接，以免误把文本当页面代码。
  if (text !== undefined) element.textContent = text;
  return element;
}

function positiveId(text) {
  return /^[0-9]{1,19}$/.test(text) && BigInt(text) > 0n && BigInt(text) < 9223372036854775808n;
}

function readUserId() {
  const field = byId("user-id");
  const userId = field.value.trim();
  if (!positiveId(userId)) {
    field.focus();
    throw new ApiError("请先在左侧填写有效的中台测试用户 ID，再发送消息。");
  }
  return userId;
}

function sessionPath(session) {
  return "/sessions/" + encodeURIComponent(session.sessionId);
}

function showTask(snapshot) {
  const panel = byId("task-state");
  panel.replaceChildren();
  byId("version").textContent = `状态版本 ${snapshot.version}`;
  byId("snapshot").textContent = JSON.stringify(snapshot, null, 2);
  const active = snapshot.activeTask;
  if (active) {
    panel.append(makeElement("strong", "", flowNames[active.flowId] || active.flowId));
    panel.append(makeElement("p", "", "当前步骤：" + active.stepId));
    if (active.pendingConfirmationId) panel.append(makeElement("p", "", "等待确认提交"));
    if (active.missingSlots?.length) panel.append(makeElement("p", "", "待补：" + active.missingSlots.join("、")));
  } else {
    panel.append(makeElement("p", "", "当前无活动任务"));
  }
  if (snapshot.pausedTasks?.length) {
    panel.append(makeElement("p", "", "暂停任务"));
    const list = makeElement("ul");
    for (const task of snapshot.pausedTasks) {
      list.append(makeElement("li", "", `${flowNames[task.flowId] || task.flowId} · ${task.stepId}`));
    }
    panel.append(list);
  }
}

function orderCard(object) {
  const card = makeElement("div", "order-card");
  const attrs = object.attributes || {};
  card.append(makeElement("strong", "", object.title || "订单卡片"));
  card.append(makeElement("p", "", [attrs.orderNo && "订单号 " + attrs.orderNo, "订单 ID " + object.id].filter(Boolean).join(" · ")));
  if (attrs.paidAmount != null) card.append(makeElement("p", "", `实付 ${attrs.paidAmount} ${attrs.currencyCode || ""}`));
  if (object.type === "order" && positiveId(String(object.id))) {
    const button = makeElement("button", "", "使用此订单");
    button.type = "button";
    button.addEventListener("click", () => queueMessage({ type: "object", object: { type: "order", id: String(object.id) } }));
    card.append(button);
  }
  return card;
}

function salesCard(object) {
  const card = makeElement("div", "order-card");
  const attrs = object.attributes || {};
  card.append(makeElement("strong", "", object.title || object.type));
  if (attrs.summary) card.append(makeElement("p", "", attrs.summary));
  if (attrs.status) card.append(makeElement("p", "", "状态：" + attrs.status));
  if (attrs.amount) card.append(makeElement("p", "", "申请金额：" + attrs.amount));
  if (object.type === "confirmation" && uuidPattern.test(object.id)) {
    const buttons = makeElement("div", "actions");
    for (const [decision, label] of [["confirm", "确认提交"], ["reject", "不提交"]]) {
      const button = makeElement("button", decision === "confirm" ? "primary" : "", label);
      button.type = "button";
      button.addEventListener("click", () => queueMessage({type: "object", object: {type: "confirmation", id: object.id, decision}}));
      buttons.append(button);
    }
    card.append(buttons);
  } else if (object.type === "ticket" && positiveId(String(object.id))) {
    card.append(makeElement("p", "", "工单 ID：" + object.id));
    const button = makeElement("button", "", "查工单");
    button.type = "button";
    button.addEventListener("click", () => queryTicket(String(object.id)));
    card.append(button);
  } else if (object.type === "refund_request" && positiveId(String(object.id))) {
    const button = makeElement("button", "", "查退款进度");
    button.type = "button";
    button.addEventListener("click", () => queueMessage({type: "text", text: "查退款进度 " + object.id}));
    card.append(button);
  }
  return card;
}

function renderTurn(turn) {
  if (rendered.has(turn.messageId)) return null;
  rendered.add(turn.messageId);
  const entry = makeElement("article", "turn");
  const sent = turn.input.type === "text" ? turn.input.text : (turn.input.object.type === "confirmation"
    ? (turn.input.object.decision === "confirm" ? "确认提交" : "不提交") : "已发订单卡片 · " + turn.input.object.id);
  entry.append(makeElement("p", "bubble user", sent));
  entry.append(makeElement("p", "turn-label", `客服 · ${routeNames[turn.route] || turn.route}`));
  for (const item of turn.messages) {
    if (item.type === "text") entry.append(makeElement("p", "bubble assistant", item.text));
    if (item.type === "object") entry.append(item.object.type === "order" ? orderCard(item.object) : salesCard(item.object));
  }
  return entry;
}

function showHistory(history, { prepend = false } = {}) {
  const oldHeight = scroller.scrollHeight;
  const oldTop = scroller.scrollTop;
  if (!prepend) {
    rendered.clear();
    messages.replaceChildren();
  }
  const fragment = document.createDocumentFragment();
  for (const turn of history.list) {
    const entry = renderTurn(turn);
    if (entry) fragment.append(entry);
  }
  if (prepend) messages.prepend(fragment);
  else messages.append(fragment);
  if (!rendered.size) messages.append(makeElement("p", "empty", "会话已就绪，发一条消息开始测试。"));
  olderCursor = history.nextBeforeSeq ?? null;
  scroller.scrollTop = prepend ? oldTop + scroller.scrollHeight - oldHeight : scroller.scrollHeight;
}

async function connect(session) {
  const path = sessionPath(session);
  const [snapshot, history] = await Promise.all([
    api(path + "/state", session.userId),
    api(path + "/messages", session.userId),
  ]);
  if (!current || current.userId !== session.userId || current.sessionId !== session.sessionId) pending = null;
  current = session;
  byId("user-id").value = session.userId;
  byId("session-id").value = session.sessionId;
  byId("connection-label").textContent = "用户 " + session.userId;
  showTask(snapshot);
  showHistory(history);
  changeStatus("ticket-result", "");
  // 刷新同一个标签页能恢复会话；浏览器不缓存消息内容或服务端密钥。
  try { sessionStorage.setItem(storageKey, JSON.stringify(current)); } catch { /* 不影响本次使用 */ }
}

async function createSession(userId) {
  const response = await api("/sessions", userId, { method: "POST" });
  byId("session-id").value = response.sessionId;
  await connect({ userId, sessionId: response.sessionId });
}

function newMessageId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // 普通 HTTP 的内网 IP 有时不提供 randomUUID；仍用浏览器随机数生成标准 UUID。
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

function queueMessage(input) {
  return run("客服正在处理…", async () => {
    if (!current) {
      // 第一次发送可以直接建会话；填过旧会话 ID 时先加载，不另建会话。
      const userId = readUserId();
      const sessionId = byId("session-id").value.trim();
      if (sessionId) {
        if (!uuidPattern.test(sessionId)) throw new ApiError("请填写完整的会话 ID，或清空后新建。");
        await connect({ userId, sessionId });
      } else {
        await createSession(userId);
      }
    }
    pending = { session: current, body: { ...input, messageId: newMessageId() } };
    await deliver(pending);
  });
}

function sendPending() {
  if (pending) return run("正在重试上一条…", () => deliver(pending));
}

async function deliver({ session, body }) {
  const path = sessionPath(session);
  let reply;
  try {
    // 网络出错保留原正文和 messageId；“重试”不会新建一轮。
    reply = byId("stream-mode").checked
      ? await readStream(path + "/messages", session.userId, body)
      : await api(path + "/messages", session.userId, { method: "POST", body });
  } catch (error) {
    if (!error.retryable) pending = null;
    throw error;
  }
  pending = null;
  const userInput = body.type === "text" ? { type: "text", text: body.text } : { type: "object", object: body.object };
  const entry = renderTurn({ ...reply, input: userInput });
  if (entry) {
    messages.querySelector(".empty")?.remove();
    messages.append(entry);
    scroller.scrollTop = scroller.scrollHeight;
  }
  if (body.type === "text" && composer.value.trim() === body.text) composer.value = "";
  if (body.type === "object" && body.object.type === "order" && byId("order-id").value.trim() === body.object.id) {
    byId("order-id").value = "";
  }
  try {
    showTask(await api(path + "/state", session.userId));
  } catch (error) {
    throw new ApiError("消息已收到，任务状态刷新失败：" + error.message);
  }
}

sessionForm.addEventListener("submit", (event) => {
  event.preventDefault();
  run("正在新建会话…", () => createSession(readUserId()));
});
byId("restore").addEventListener("click", () => run("正在加载会话…", async () => {
  const sessionId = byId("session-id").value.trim();
  if (!uuidPattern.test(sessionId)) throw new ApiError("请填写完整的会话 ID。");
  await connect({ userId: readUserId(), sessionId });
}));
byId("refresh").addEventListener("click", () => run("正在刷新记录和任务状态…", () => connect(current)));
byId("older").addEventListener("click", () => run("正在读取更早的记录…", async () => {
  const history = await api(sessionPath(current) + "/messages?beforeSeq=" + olderCursor, current.userId);
  showHistory(history, { prepend: true });
}));
byId("retry").addEventListener("click", sendPending);
byId("disconnect").addEventListener("click", () => streamController?.abort());
function queryTicket(id) {
  return run("正在查询工单…", async () => {
    if (!current) throw new ApiError("请先创建或加载会话。");
    if (!positiveId(id)) throw new ApiError("请填写有效工单 ID。");
    const ticket = await api("/tickets/" + encodeURIComponent(id), current.userId);
    byId("ticket-id").value = id;
    changeStatus("ticket-result", `${ticket.ticketNo} · ${ticket.status} · ${ticket.nextStep}`);
  });
}
byId("ticket-form").addEventListener("submit", (event) => {
  event.preventDefault();
  queryTicket(byId("ticket-id").value.trim());
});
textForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = composer.value.trim();
  if (text) queueMessage({ type: "text", text });
});
composer.addEventListener("keydown", (event) => {
  // 中文输入法确认候选词也可能按回车，不能在那时把半句发出去。
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229 && !busy) {
    event.preventDefault();
    textForm.requestSubmit();
  }
});
byId("order-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const id = byId("order-id").value.trim();
  if (!positiveId(id)) return changeStatus("error", "请填写有效的订单 ID（orderId）。");
  queueMessage({ type: "object", object: { type: "order", id } });
});
for (const button of document.querySelectorAll("[data-prompt]")) {
  button.addEventListener("click", () => {
    composer.value = button.dataset.prompt;
    composer.focus();
  });
}

syncControls();
try {
  const saved = JSON.parse(sessionStorage.getItem(storageKey));
  if (saved && positiveId(saved.userId) && uuidPattern.test(saved.sessionId)) {
    byId("user-id").value = saved.userId;
    byId("session-id").value = saved.sessionId;
    run("正在恢复上次会话…", () => connect(saved));
  }
} catch { /* 本地会话记录损坏时可以手动重新加载。 */ }
