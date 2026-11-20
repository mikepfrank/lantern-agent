"use strict";

const byId = (id) => document.getElementById(id);
const historyView = byId("history");
const feed = byId("events");
const draft = byId("draft");
const older = byId("older");
const send = byId("send");
const jump = byId("jump");
const reviewDialog = byId("review-dialog");
const reviewInbox = byId("review-inbox");
const reviewAnswer = byId("review-answer");
const reviewSubmit = byId("review-submit");
const idleHint = matchMedia("(pointer: fine)").matches ? "Enter to send · Shift + Enter for a new line" : "";
const storageKey = (name) => "lantern.room." + name;

function stored(name) {
  try { return sessionStorage.getItem(storageKey(name)); } catch { return null; }
}

function remember(name, value) {
  try {
    if (value === null) sessionStorage.removeItem(storageKey(name));
    else sessionStorage.setItem(storageKey(name), value);
  } catch { /* Chat still works for the lifetime of this tab. */ }
}

const fragmentKey = new URLSearchParams(location.hash.slice(1)).get("key");
if (location.hash) history.replaceState(null, "", location.pathname);
if (fragmentKey) remember("key", fragmentKey);
let key = fragmentKey || stored("key");
let pending;
try { pending = JSON.parse(stored("pending")); } catch { pending = null; }
draft.value = pending ? pending.text : (stored("draft") || "");

let initialized = false;
let earliest = 0;
let cursor = 0;
let polling = false;
let sending = false;
let timer;
let follow = true;
let sendError = "";
let reviewQuestions = [];
let reviewId = null;
let reviewSending = false;
let reviewFetching = false;
let reviewRevision = 0;
let nextReviewPoll = 0;
const deferredReviews = new Set();
const shortTime = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function connection(state, label) {
  const node = byId("connection");
  node.dataset.state = state;
  const text = node.querySelector("span");
  if (text.textContent !== label) text.textContent = label;
}

function controls() {
  draft.disabled = !key || !initialized;
  draft.readOnly = Boolean(pending);
  send.disabled = !key || !initialized || sending || !draft.value.trim();
  send.firstChild.textContent = sending ? "Sending " : pending ? "Retry send " : "Send ";
  const state = byId("send-state");
  state.dataset.error = String(Boolean(sendError));
  const label = sendError || (sending ? "Sending…" : idleHint);
  if (state.textContent !== label) state.textContent = label;
}

function locked() {
  key = null;
  remember("key", null);
  connection("locked", "Link required");
  byId("worker").textContent = "";
  sendError = "Open a fresh link using lantern web-link.";
  if (!feed.childElementCount) byId("empty").textContent = "An operator link is required to open this conversation.";
  older.disabled = true;
  reviewRevision++;
  reviewQuestions = [];
  closeReview();
  reviewInbox.hidden = true;
  byId("review-notice").textContent = "";
  controls();
}

class RequestError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function request(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const headers = { Authorization: "Bearer " + key };
    const options = { headers, signal: controller.signal, cache: "no-store", credentials: "omit", redirect: "error" };
    if (body) {
      options.method = "POST";
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    if (!response.ok) {
      if (response.status === 401) locked();
      const detail = await response.json().catch(() => ({}));
      throw new RequestError(response.status, detail.error || "Request failed.");
    }
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

function updateWorker(status) {
  const node = byId("worker");
  let label = "Worker offline";
  if (status.online) label = status.working ? "Lantern is working" : "Ready";
  if (status.queued) label += " · " + status.queued + " queued";
  node.dataset.state = status.working ? "working" : "idle";
  if (node.textContent !== label) node.textContent = label;
}

function closeReview() {
  reviewId = null;
  if (reviewDialog.open) reviewDialog.close();
}

function openReview(item) {
  reviewId = item.id;
  const priorDenial = item.source === "prior_denial";
  byId("review-title").textContent = priorDenial ? "Clarify a previous Opus denial" : "Opus needs clarification";
  byId("review-about").textContent = (priorDenial ? "The host is asking about an earlier review. " : "")
    + "Your reply is recorded under your operator identity and quoted to Opus when this action is reviewed. Lantern will be notified.";
  byId("review-number").textContent = "Question #" + item.id;
  byId("review-question").textContent = item.question;
  byId("review-action").textContent = item.action;
  byId("review-excerpt").textContent = item.excerpt || "";
  byId("review-evidence").hidden = !item.excerpt;
  const time = byId("review-time");
  const date = new Date(item.asked_at);
  time.dateTime = item.asked_at;
  time.textContent = Number.isFinite(date.getTime()) ? shortTime.format(date) : "";
  reviewAnswer.value = stored("review-answer." + item.id) || "";
  reviewSubmit.disabled = !reviewAnswer.value.trim();
  byId("review-error").textContent = "";
  byId("review-notice").textContent = "";
  if (!reviewDialog.open) reviewDialog.showModal();
  else reviewAnswer.focus();
}

function updateReviewQueue(items) {
  for (const item of reviewQuestions) {
    if (items.some((current) => current.id === item.id)) continue;
    deferredReviews.delete(item.id);
    remember("review-later." + item.id, null);
    remember("review-answer." + item.id, null);
    if (reviewId === item.id) {
      closeReview();
      byId("review-notice").textContent = "That question is no longer pending.";
    }
  }
  reviewQuestions = items;
  byId("review-next").hidden = items.length < 2;
  reviewInbox.hidden = !items.length;
  reviewInbox.textContent = items.length > 1 ? "Answer Opus (" + items.length + ")" : "Answer Opus";
  if (!items.length || reviewDialog.open || document.hidden) return;
  const next = items.find((item) => !deferredReviews.has(item.id) && !stored("review-later." + item.id));
  if (next && !(document.activeElement === draft && draft.value)) openReview(next);
}

async function pollReviews() {
  if (!key || document.hidden || reviewSending || reviewFetching || Date.now() < nextReviewPoll) return;
  const revision = reviewRevision;
  reviewFetching = true;
  try {
    const page = await request("/api/review-questions");
    // A fetch started before a submission can still include the answered question.
    if (revision === reviewRevision && key) {
      const notice = byId("review-notice");
      if (notice.dataset.offline) {
        notice.textContent = "";
        delete notice.dataset.offline;
      }
      updateReviewQueue(page.questions);
    }
  } catch {
    if (key && revision === reviewRevision) {
      const notice = byId("review-notice");
      notice.dataset.offline = "true";
      notice.textContent = "Couldn’t check for Opus questions. Retrying…";
    }
  } finally {
    reviewFetching = false;
    nextReviewPoll = Date.now() + 8000;
  }
}

function deferReview() {
  deferredReviews.add(reviewId);
  remember("review-later." + reviewId, "1");
  closeReview();
  reviewInbox.focus();
}

reviewInbox.addEventListener("click", () => {
  if (reviewQuestions.length) openReview(reviewQuestions[0]);
});
byId("review-next").addEventListener("click", () => {
  const index = reviewQuestions.findIndex((item) => item.id === reviewId);
  if (index >= 0) openReview(reviewQuestions[(index + 1) % reviewQuestions.length]);
});
byId("review-later").addEventListener("click", deferReview);
reviewDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  if (!reviewSending) deferReview();
});
reviewAnswer.addEventListener("input", () => {
  remember("review-answer." + reviewId, reviewAnswer.value);
  reviewSubmit.disabled = !reviewAnswer.value.trim();
});
byId("review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (reviewSending || !reviewId || !reviewAnswer.value.trim()) return;
  const id = reviewId;
  reviewRevision++;
  reviewSending = true;
  reviewAnswer.readOnly = true;
  byId("review-next").disabled = true;
  byId("review-later").disabled = true;
  reviewSubmit.disabled = true;
  byId("review-error").textContent = "";
  try {
    await request("/api/review-questions/" + encodeURIComponent(id) + "/answer", { answer: reviewAnswer.value });
    if (!key) return;
    updateReviewQueue(reviewQuestions.filter((item) => item.id !== id));
    byId("review-notice").textContent = "Your answer to Opus was recorded.";
  } catch (error) {
    if (!key) return;
    if ([404, 409, 410].includes(error.status)) {
      updateReviewQueue(reviewQuestions.filter((item) => item.id !== id));
      byId("review-notice").textContent = "That question is no longer pending.";
    } else {
      byId("review-error").textContent = error.status === 400 || error.status === 413
        ? error.message : "Delivery unconfirmed. You can retry your answer.";
    }
  } finally {
    reviewSending = false;
    reviewAnswer.readOnly = false;
    byId("review-next").disabled = false;
    byId("review-later").disabled = false;
    reviewSubmit.disabled = !reviewAnswer.value.trim();
    nextReviewPoll = 0;
  }
});

function messageNode(event) {
  const speakers = { operator: "You", lantern: "Lantern", system: "Service" };
  const role = Object.hasOwn(speakers, event.role) ? event.role : "system";
  const item = document.createElement("article");
  item.className = "message " + role;
  const header = document.createElement("header");
  const speaker = document.createElement("span");
  speaker.className = "speaker";
  speaker.textContent = speakers[role];
  const time = document.createElement("time");
  time.dateTime = event.at;
  const date = new Date(event.at);
  time.textContent = shortTime.format(date);
  time.title = date.toLocaleString();
  header.append(speaker, time);
  const body = document.createElement("div");
  body.className = "body";
  if (role === "lantern" && typeof renderLanternMarkdown === "function") {
    renderLanternMarkdown(body, event.text);
  } else {
    appendPlainMessage(body, event.text);
  }
  item.append(header, body);
  return item;
}

function appendPlainMessage(body, text) {
  // Preserve existing literal text and fenced code in operator and service messages.
  const blocks = text.split(/^\x60{3}[^\r\n]*\r?\n([\s\S]*?)^\x60{3}[ \t]*(?:\r?\n|$)/gm);
  blocks.forEach((part, i) => {
    if (i % 2) {
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = part.replace(/\r?\n$/, "");
      pre.append(code);
      body.append(pre);
    } else {
      body.append(document.createTextNode(part));
    }
  });
}

function syncJump() {
  follow = historyView.scrollHeight - historyView.scrollTop - historyView.clientHeight < 36;
  jump.hidden = follow;
}

function latest() {
  historyView.scrollTop = historyView.scrollHeight;
  syncJump();
}

function appendEvents(rows) {
  const newRows = rows.filter((row) => row.id > cursor);
  if (!newRows.length) return;
  const sticky = follow;
  byId("empty").hidden = true;
  feed.append(...newRows.map(messageNode));
  earliest ||= newRows[0].id;
  cursor = newRows[newRows.length - 1].id;
  remember("cursor", String(cursor));
  if (sticky) latest();
  else syncJump();
  if (initialized && newRows.some((row) => row.role === "lantern")) {
    byId("announcer").textContent = "New Lantern message at " + shortTime.format(new Date(newRows[newRows.length - 1].at));
  }
}

async function poll() {
  if (polling || !key) return;
  clearTimeout(timer);
  polling = true;
  let delay = 1500;
  try {
    let page;
    if (!initialized) {
      page = await request("/api/room");
      const saved = Number(stored("cursor"));
      if (saved > 0 && page.events.length && saved < page.events[0].id - 1) {
        // Rebuild the tab at its last delivery, then drain every intervening event.
        const anchor = await request("/api/room?before=" + (saved + 1));
        if (anchor.events.length) page = anchor;
      }
      older.hidden = !page.more;
      appendEvents(page.events);
      if (!cursor) byId("empty").textContent = "Send your first message to Lantern.";
      initialized = true;
      delay = 50;
    } else {
      page = await request("/api/room?after=" + cursor);
      appendEvents(page.events);
      if (page.more) delay = 50;
    }
    connection("live", "Connected");
    updateWorker(page.status);
    void pollReviews();
  } catch {
    if (key) {
      connection("offline", "Reconnecting");
      byId("worker").textContent = "";
      if (!initialized) byId("empty").textContent = "Waiting for the SSH connection…";
      delay = 3500;
    }
  } finally {
    polling = false;
    controls();
    if (key) timer = setTimeout(poll, delay);
  }
}

older.addEventListener("click", async () => {
  if (!earliest || older.disabled) return;
  older.disabled = true;
  try {
    const page = await request("/api/room?before=" + earliest);
    const anchor = feed.firstElementChild;
    const offset = anchor.getBoundingClientRect().top;
    feed.prepend(...page.events.map(messageNode));
    if (page.events.length) earliest = page.events[0].id;
    older.hidden = !page.more;
    historyView.scrollTop += anchor.getBoundingClientRect().top - offset;
    syncJump();
  } catch {
    if (key) byId("announcer").textContent = "Earlier messages could not be loaded. Try again.";
  } finally {
    older.disabled = !key;
  }
});

byId("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (send.disabled) return;
  if (!pending) {
    pending = { text: draft.value, client_id: crypto.randomUUID() };
    remember("pending", JSON.stringify(pending));
  }
  sending = true;
  sendError = "";
  controls();
  try {
    await request("/api/messages", pending);
    pending = null;
    remember("pending", null);
    remember("draft", null);
    draft.value = "";
    latest();
    void poll();
  } catch (error) {
    if (error.status === 400 || error.status === 413 || error.status === 409) {
      pending = null;
      remember("pending", null);
      sendError = error.message;
    } else if (key) {
      sendError = "Delivery unconfirmed. Retry send to check or deliver this message.";
    }
  } finally {
    sending = false;
    controls();
    draft.focus();
  }
});

draft.addEventListener("input", () => {
  remember("draft", draft.value);
  sendError = "";
  controls();
});
draft.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && matchMedia("(pointer: fine)").matches) {
    event.preventDefault();
    byId("composer").requestSubmit();
  }
});
jump.addEventListener("click", latest);
historyView.addEventListener("scroll", syncJump, { passive: true });
document.addEventListener("visibilitychange", () => { if (!document.hidden) void poll(); });
window.addEventListener("online", poll);
window.addEventListener("hashchange", () => {
  // Opening a fresh link in this same tab can be a same-document navigation.
  if (new URLSearchParams(location.hash.slice(1)).has("key")) location.reload();
});
if (pending) sendError = "A previous send is unconfirmed. Retry send to check delivery.";
if (key) void poll();
else locked();
