"use strict";

const byId = (id) => document.getElementById(id);
const historyView = byId("history");
const feed = byId("events");
const draft = byId("draft");
const older = byId("older");
const send = byId("send");
const attach = byId("attach");
const picker = byId("image-file");
const imageDraft = byId("image-draft");
const maxImages = 4;
const imageDialog = byId("image-dialog");
const jump = byId("jump");
const reviewDialog = byId("review-dialog");
const reviewInbox = byId("review-inbox");
const reviewAnswer = byId("review-answer");
const reviewSubmit = byId("review-submit");
const concernDialog = byId("concern-dialog");
const concernToggle = byId("concern-toggle");
const concernList = byId("concern-list");
const exportDialog = byId("export-dialog");
const exportRange = byId("export-range");
const exportDays = byId("export-days");
const exportSave = byId("export-save");
let exportRequest = null;
const idleHint = matchMedia("(pointer: fine)").matches ? "Enter to send · Shift + Enter for a new line" : "";
const storageKey = (name) => "lantern.room." + name;
const cookieRoom = location.protocol === "https:";
const signOut = byId("sign-out");
let authenticated = cookieRoom;

function clearPrivateStorage() {
  try {
    for (const name of Object.keys(sessionStorage)) {
      if (name.startsWith(storageKey(""))) sessionStorage.removeItem(name);
    }
  } catch { /* Private browsing can disable storage. */ }
}

function stored(name) {
  try { return sessionStorage.getItem(storageKey(name)); } catch { return null; }
}

function remember(name, value) {
  if (cookieRoom && !authenticated) return false;
  try {
    if (value === null) sessionStorage.removeItem(storageKey(name));
    else sessionStorage.setItem(storageKey(name), value);
    return true;
  } catch { return false; }
}

// The HTTPS room uses only its HttpOnly session cookie. The loopback SSH
// room retains its separate fragment-key and bearer-header flow.
const fragmentKey = !cookieRoom && new URLSearchParams(location.hash.slice(1)).get("key");
if (location.hash) history.replaceState(null, "", location.pathname);
if (fragmentKey) remember("key", fragmentKey);
const linkKey = cookieRoom ? null : fragmentKey || stored("key");
authenticated = cookieRoom || Boolean(linkKey);
if (cookieRoom) {
  try {
    const epoch = localStorage.getItem("lantern.signin.epoch");
    if (epoch !== stored("epoch")) {
      clearPrivateStorage();
      remember("epoch", epoch);
    }
  } catch { /* Cookies suffice when browser storage is disabled. */ }
}
let attachments = [];
let pending = null;
let draftWarning = "";
// One atomic, tab-private record holds the ordered PNGs and any unconfirmed
// receipt ID. Four 1 MiB PNGs fit as binary strings without base64 expansion.
function saveComposer() {
  let value = null;
  if (attachments.length || pending) {
    const bytes = attachments.map(image => atob(image.data));
    const images = attachments.map(({data, ...metadata}, i) => ({...metadata, length: bytes[i].length}));
    // Keep the small header ASCII even with Unicode filenames and captions.
    // Otherwise the browser can store the entire binary record as two-byte characters.
    value = [encodeURIComponent(JSON.stringify({pending, images})), "\n", ...bytes].join("");
  }
  const saved = remember("composer", value);
  draftWarning = saved ? "" : "This browser couldn’t save the draft. Keep this tab open.";
  if (saved) {
    remember("image-draft", null);
    remember("pending", null);
  }
  return saved;
}

try {
  const saved = stored("composer");
  if (saved) {
    const split = saved.indexOf("\n");
    const record = JSON.parse(decodeURIComponent(saved.slice(0, split)));
    let offset = split + 1;
    attachments = record.images.map(({length, ...metadata}) => {
      const bytes = saved.slice(offset, offset + length);
      if (bytes.length !== length) throw Error("Incomplete draft");
      offset += length;
      return {...metadata, data: btoa(bytes)};
    });
    pending = record.pending;
  } else {
    // Recover drafts and uncertain deliveries made by the single-image client.
    const oldPending = JSON.parse(stored("pending"));
    const image = oldPending ? oldPending.image : JSON.parse(stored("image-draft"));
    if (image) attachments.push({name: "Attached image", ...image});
    if (oldPending) pending = {client_id: oldPending.client_id, text: oldPending.text};
    if (image || pending) saveComposer();
  }
} catch {
  draftWarning = "Couldn’t restore the saved attachment draft. Check the conversation before resending.";
}
draft.value = pending ? pending.text : (stored("draft") || "");
const filesToPrepare = [];

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
let concernItems = [];
let concernAsOf = null;
let selectedConcern = null;
let releasing = false;
let fetchingConcerns = false;
let nextConcernPoll = 0;
let concernRevision = 0;
const deferredReviews = new Set();
const shortTime = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function connection(state, label) {
  const node = byId("connection");
  node.dataset.state = state;
  const text = node.querySelector("span");
  if (text.textContent !== label) text.textContent = label;
}

function controls() {
  byId("export-open").disabled = !authenticated || !initialized;
  draft.disabled = !authenticated || !initialized;
  draft.readOnly = Boolean(pending);
  attach.disabled = !authenticated || !initialized || Boolean(pending) || attachments.length + filesToPrepare.length >= maxImages;
  for (const button of imageDraft.querySelectorAll("button")) button.disabled = !authenticated || Boolean(pending);
  send.disabled = !authenticated || !initialized || sending || filesToPrepare.length > 0 || (!draft.value.trim() && !attachments.length);
  send.firstChild.textContent = sending ? "Sending " : pending ? "Retry send " : "Send ";
  const state = byId("send-state");
  state.dataset.error = String(Boolean(sendError || draftWarning));
  const label = sendError || draftWarning || (filesToPrepare.length ? "Preparing images…" : sending ? "Sending…" : idleHint);
  if (state.textContent !== label) state.textContent = label;
}

function locked() {
  authenticated = false;
  showCompaction(false);
  closeExport();
  importsDialog.close();
  importsForm.reset();
  byId("imports-fields").replaceChildren();
  for (const panel of [imports, clones]) {
    byId(panel.id + "-list").replaceChildren();
    byId(panel.id + "-count").textContent = "";
    byId(panel.id + "-action-error").textContent = "";
    byId(panel.id + "-state").textContent = "Sign in to view " + panel.label + ".";
    panel.snapshot = "";
    panel.revision++;
  }
  byId("imports-error").textContent = "";
  byId("imports-new").disabled = true;
  concernRevision++;
  concernItems = [];
  concernAsOf = null;
  selectedConcern = null;
  if (concernDialog.open) concernDialog.close();
  for (const id of ["concern-title", "concern-confirm-summary", "concern-confirm-tools", "concern-confirm-ruling", "concern-error"]) {
    byId(id).textContent = "";
  }
  byId("concern-reason").value = "";
  concernList.replaceChildren();
  byId("concern-state").textContent = "Sign in to view standing concerns.";
  byId("concern-updated").textContent = "";
  byId("concern-count").textContent = "";
  if (cookieRoom) {
    // Conceal the previous session immediately while the server selects the
    // new landing page. Also remove private drafts, images and pending sends.
    document.body.hidden = true;
    clearTimeout(timer);
    clearPrivateStorage();
    pending = null;
    attachments = [];
    filesToPrepare.length = 0;
    draft.value = "";
    reviewAnswer.value = "";
    if (reviewDialog.open) reviewDialog.close();
    if (imageDialog.open) imageDialog.close();
    feed.replaceChildren();
    imageDraft.replaceChildren();
    byId("image-full").removeAttribute("src");
    location.replace("/");
    return;
  }
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

async function request(path, body, responseType = "json", signal) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  signal?.addEventListener("abort", abort, {once: true});
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const headers = cookieRoom ? {} : { Authorization: "Bearer " + linkKey };
    const options = { headers, signal: controller.signal, cache: "no-store",
      credentials: cookieRoom ? "same-origin" : "omit", redirect: "error" };
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
    const result = responseType === "image" ? await response.blob() : await response.json();
    if (!authenticated) throw new RequestError(401, "Session ended.");
    return result;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

function displayAttachments() {
  const count = byId("image-count");
  count.hidden = imageDraft.hidden = !attachments.length;
  count.textContent = attachments.length + " of " + maxImages + " images";
  imageDraft.replaceChildren(...attachments.map((attachment, index) => {
    const card = byId("image-card").content.firstElementChild.cloneNode(true);
    card.querySelector("img").src = "data:image/png;base64," + attachment.data;
    const name = card.querySelector("strong");
    name.textContent = attachment.name;
    name.title = attachment.name;
    const size = card.querySelector(".image-details span");
    if (attachment.width) size.textContent = attachment.width + " × " + attachment.height + (attachment.resized ? " · Resized" : "");
    const remove = card.querySelector("button");
    remove.setAttribute("aria-label", "Remove image " + (index + 1) + ", " + attachment.name);
    remove.title = "Remove image";
    remove.addEventListener("click", () => {
      attachments.splice(index, 1);
      sendError = "";
      saveComposer();
      displayAttachments();
      byId("announcer").textContent = "Removed " + attachment.name;
      (imageDraft.querySelectorAll("button")[index] || attach).focus();
    });
    return card;
  }));
  controls();
}

async function prepareImage(file) {
  if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) throw Error("Choose a PNG, JPEG or WebP image.");
  if (file.size > 32 * 1024 * 1024) throw Error("Choose an image under 32 MiB.");
  const source = new Image();
  const url = URL.createObjectURL(file);
  try {
    source.src = url;
    await source.decode();
    const originalWidth = source.naturalWidth, originalHeight = source.naturalHeight;
    let scale = Math.min(1, 1600 / Math.max(originalWidth, originalHeight));
    const canvas = document.createElement("canvas");
    const drawing = canvas.getContext("2d");
    while (true) {
      canvas.width = Math.max(1, Math.round(originalWidth * scale));
      canvas.height = Math.max(1, Math.round(originalHeight * scale));
      drawing.imageSmoothingQuality = "high";
      drawing.drawImage(source, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
      if (!blob) throw Error("Couldn’t prepare this image. Try a different file.");
      if (blob.size <= 1024 * 1024) {
        const reader = new FileReader();
        const dataURL = await new Promise((resolve, reject) => {
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(Error("Couldn’t read this image."));
          reader.readAsDataURL(blob);
        });
        return { mime_type: "image/png", data: dataURL.split(",")[1], name: file.name || "Pasted image",
          width: canvas.width, height: canvas.height,
          resized: canvas.width !== originalWidth || canvas.height !== originalHeight };
      }
      scale *= 0.9 * Math.sqrt((1024 * 1024) / blob.size);
    }
  } finally {
    URL.revokeObjectURL(url);
  }
}

attach.addEventListener("click", () => picker.click());
picker.addEventListener("change", () => {
  const files = Array.from(picker.files);
  picker.value = "";
  void addImages(files);
  if (files.length && !draft.disabled) draft.focus();
});
draft.addEventListener("paste", event => {
  const clipboard = event.clipboardData;
  if (!clipboard || pending) return;
  let files = Array.from(clipboard.files);
  if (!files.length) files = Array.from(clipboard.items).filter(item => item.kind === "file").map(item => item.getAsFile()).filter(Boolean);
  const images = files.filter(file => file.type.startsWith("image/"));
  if (!images.length) return;
  // Keep native plain-text editing (including undo) when a paste also has text.
  if (!clipboard.getData("text/plain")) event.preventDefault();
  void addImages(images);
});

async function addImages(files) {
  if (!authenticated || !initialized || pending || !files.length) return;
  const running = filesToPrepare.length > 0;
  const slots = maxImages - attachments.length - filesToPrepare.length;
  filesToPrepare.push(...files.slice(0, slots));
  sendError = files.length > slots ? "Up to 4 images per message. Extra images weren’t attached." : "";
  controls();
  if (running) return;
  // Repeated pastes and picker batches share one queue to preserve their order.
  while (filesToPrepare.length && authenticated) {
    const file = filesToPrepare[0];
    try {
      const attachment = await prepareImage(file);
      if (!authenticated) return;
      attachments.push(attachment);
      saveComposer();
      byId("announcer").textContent = "Attached " + attachment.name;
    } catch (error) {
      sendError = error instanceof DOMException ? "Couldn’t open this image. Try another file." : error.message;
    } finally {
      filesToPrepare.shift();
      if (authenticated) displayAttachments();
    }
  }
}

function showCompaction(active) {
  const node = byId("compaction-status");
  const text = active ? "Lantern’s agent harness is compacting the context…" : "";
  if (node.textContent === text) return;
  node.textContent = text;
  if (follow) latest();
  else syncJump();
}

function updateWorker(status) {
  const node = byId("worker");
  let label = "Worker offline";
  if (status.online) label = status.working ? "Lantern is working" : "Ready";
  if (status.queued) label += " · " + status.queued + " queued";
  node.dataset.state = status.working ? "working" : "idle";
  if (node.textContent !== label) node.textContent = label;
  showCompaction(status.compacting);
}

function expandConcerns(expanded) {
  concernToggle.setAttribute("aria-expanded", String(expanded));
  byId("concern-panel").hidden = !expanded;
}
const wideRoom = matchMedia("(min-width: 54.01rem)");
expandConcerns(wideRoom.matches);
wideRoom.addEventListener("change", event => expandConcerns(event.matches));
concernToggle.addEventListener("click", () => {
  const expanding = byId("concern-panel").hidden;
  expandConcerns(expanding);
  if (expanding) void pollConcerns(true);
});

function concernScope(item) {
  if (item.status === "pending" || item.tools.includes("*")) return "Scope covers all tools.";
  return item.tools.length ? "Tools in scope · " + item.tools.join(", ") : "No tools specified.";
}

function openConcern(id) {
  const item = concernItems.find(item => item.id === id);
  if (!item || item.release_pending) return;
  selectedConcern = item;
  byId("concern-title").textContent = "Overrule concern #" + item.id + "?";
  byId("concern-confirm-summary").textContent = item.summary;
  byId("concern-confirm-tools").textContent = concernScope(item);
  const ruling = byId("concern-confirm-ruling");
  ruling.hidden = !item.reason;
  ruling.textContent = item.reason ? "Ruling v" + item.version + " · " + item.reason : "";
  byId("concern-reason").value = "";
  byId("concern-error").textContent = "";
  byId("concern-submit").disabled = false;
  concernDialog.showModal();
}

function setConcernNotice(text, error = false) {
  const state = byId("concern-state");
  state.dataset.error = String(error);
  if (state.textContent !== text) state.textContent = text;
}

function renderConcerns(items, asOf) {
  concernItems = items;
  concernAsOf = asOf;
  // Keep disclosure, focus and scroll state when ordinary room polling returns the same rulings.
  const cards = new Map(Array.from(concernList.children, node => [Number(node.dataset.id), node]));
  for (const item of items) {
    let card = cards.get(item.id);
    if (!card) {
      card = byId("concern-card").content.firstElementChild.cloneNode(true);
      card.dataset.id = item.id;
      card.querySelector("button").addEventListener("click", () => openConcern(item.id));
      concernList.append(card);
    }
    cards.delete(item.id);
    const key = item.version + "/" + Boolean(item.release_pending);
    if (card.dataset.state === key) continue;
    card.dataset.state = key;
    card.querySelector("h3").textContent = "Concern #" + item.id;
    const status = {watch: "Watching", pending: "Awaiting review", question: "Awaiting clarification"};
    card.querySelector(".concern-status").textContent = item.release_pending ? "Release requested" : status[item.status];
    card.querySelector(".concern-summary").textContent = item.summary;
    card.querySelector(".concern-tools").textContent = concernScope(item);
    card.querySelector(".concern-reason").textContent = item.reason ? "v" + item.version + " · " + item.reason : "Awaiting the first ruling.";
    const evidence = card.querySelector(".concern-evidence");
    evidence.textContent = item.evidence || "";
    evidence.parentElement.hidden = !item.evidence;
    const button = card.querySelector("button");
    button.setAttribute("aria-label", "Overrule concern #" + item.id);
    if (item.release_pending && document.activeElement === button) concernToggle.focus({preventScroll: true});
    button.disabled = Boolean(item.release_pending);
  }
  for (const card of cards.values()) {
    if (card.contains(document.activeElement)) concernToggle.focus({preventScroll: true});
    card.remove();
  }
  const snapshotTime = asOf && new Date(asOf);
  const stale = snapshotTime && Date.now() - snapshotTime.getTime() > 90000;
  byId("concern-count").textContent = snapshotTime ? "(" + items.length + (stale ? " last seen)" : ")") : "";
  byId("concern-updated").textContent = snapshotTime ? "Worker snapshot · " + shortTime.format(snapshotTime) : "";
  if (!snapshotTime) setConcernNotice("Waiting for the first worker snapshot…");
  else if (stale) setConcernNotice(items.length ? "Showing the last known concerns. Waiting for a current worker snapshot."
    : "The last snapshot showed no standing concerns. Waiting for a current worker snapshot.", true);
  else setConcernNotice(items.length ? "" : "No standing concerns.");
  if (selectedConcern && !releasing) {
    const current = items.find(item => item.id === selectedConcern.id);
    if (!current || current.version !== selectedConcern.version || current.release_pending) {
      byId("concern-submit").disabled = true;
      byId("concern-error").textContent = "The concern has changed. Close this dialog to see its current status.";
    }
  }
}

async function pollConcerns(force = false) {
  if (!authenticated || document.hidden || fetchingConcerns || releasing || (!force && Date.now() < nextConcernPoll)) return;
  const revision = concernRevision;
  fetchingConcerns = true;
  nextConcernPoll = Date.now() + 8000;
  try {
    const page = await request("/api/concerns");
    if (authenticated && revision === concernRevision) renderConcerns(page.concerns, page.as_of);
  } catch {
    if (authenticated && revision === concernRevision) {
      if (concernAsOf) byId("concern-count").textContent = "(" + concernItems.length + " last seen)";
      setConcernNotice("Couldn’t refresh concerns. Retrying…", true);
    }
  } finally {
    fetchingConcerns = false;
  }
}

function closeConcern() {
  selectedConcern = null;
  concernDialog.close();
  if (authenticated && document.activeElement === document.body) concernToggle.focus({preventScroll: true});
}
byId("concern-cancel").addEventListener("click", closeConcern);
concernDialog.addEventListener("cancel", event => {
  event.preventDefault();
  if (!releasing) closeConcern();
});
byId("concern-form").addEventListener("submit", async event => {
  event.preventDefault();
  const item = selectedConcern;
  if (!item || releasing || byId("concern-submit").disabled) return;
  releasing = true;
  concernRevision++;
  const reason = byId("concern-reason");
  reason.readOnly = true;
  byId("concern-submit").disabled = true;
  byId("concern-cancel").disabled = true;
  byId("concern-error").textContent = "";
  try {
    await request("/api/concerns/" + item.id + "/release", {version: item.version, reason: reason.value.trim()});
    closeConcern();
    renderConcerns(concernItems.map(current => current.id === item.id ? {...current, release_pending: true} : current), concernAsOf);
    byId("announcer").textContent = "Release requested for concern #" + item.id + ".";
  } catch (error) {
    if (!authenticated) return;
    const stale = error.status === 404 || error.status === 409;
    byId("concern-error").textContent = stale ? "The concern has changed. Close this dialog to see its current status."
      : error.status === 400 ? error.message : "Release unconfirmed. Check the concern’s status before retrying.";
    byId("concern-submit").disabled = stale;
  } finally {
    releasing = false;
    reason.readOnly = false;
    byId("concern-cancel").disabled = false;
    nextConcernPoll = 0;
    if (authenticated) {
      void pollConcerns(true);
      void poll();
    }
  }
});

const importsDialog = byId("imports-dialog");
const importsForm = byId("imports-form");
function grantPanel(id, endpoint, label) {
  return {id, endpoint, label, snapshot: "", revision: 0, fetching: false, changing: false, nextPoll: 0};
}
const imports = grantPanel("imports", "/api/file-imports", "file imports");
const clones = grantPanel("clones", "/api/repository-clones", "repository clones");

function importText(parent, tag, value) {
  const child = document.createElement(tag);
  child.textContent = value;
  parent.append(child);
  return child;
}

function renderFileGrant(card, item, pending) {
  importText(card, "strong", (pending ? "Request #" : "Grant #") + item.id + " · " + item.project + " · " + item.collection);
  const files = document.createElement("ul");
  card.append(files);
  for (const file of item.files) {
    const line = document.createElement("li");
    files.append(line);
    importText(line, "strong", file.filename);
    importText(line, "p", file.url);
    for (const url of file.redirect_urls || []) importText(line, "p", "Allowed redirect · " + url);
    if (file.sha256) importText(line, "code", "Expected SHA-256 · " + file.sha256);
  }
  const scope = pending ? "Allow once delivers each listed file once. Standing permission allows repeat imports from these exact URLs."
    : item.mode === "standing" ? "Standing permission for the listed files." : "One delivery per listed file.";
  importText(card, "p", scope).className = "import-scope";
}

function renderCloneGrant(card, item) {
  importText(card, "strong", (item.status === "approved" ? "Grant #" : "Request #") + item.id);
  const target = importText(card, "p", "");
  const repoURL = "https://github.com/" + item.repository.split("/").map(encodeURIComponent).join("/");
  const link = importText(target, "a", repoURL);
  link.href = repoURL + "/tree/" + encodeURIComponent(item.commit);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  importText(card, "p", "Branch · " + item.branch);
  importText(card, "code", "Requested commit · " + item.commit);
  importText(card, "p", "Lantern’s purpose · " + item.purpose);
  const scope = item.status === "pending" ? "Approve the displayed commit once, or allow future commits from this branch."
    : item.status === "denied" ? "Denied." : item.status === "revoked" ? "Revoked."
    : item.mode === "standing" ? "Updates approved for this branch." : "Only this commit is approved.";
  importText(card, "p", scope).className = "import-scope";
}

function renderGrants(panel, page) {
  const receipts = page.receipts || [];
  const closed = page.closed || [];
  const snapshot = JSON.stringify([page.requests, page.grants, receipts, closed]);
  byId(panel.id + "-count").textContent = page.requests.length ? " · " + page.requests.length + " pending" : "";
  byId(panel.id + "-state").textContent = page.requests.length || page.grants.length ? ""
    : panel === clones ? "No pending repository requests or active grants." : "No pending requests or active grants.";
  if (snapshot === panel.snapshot) return;
  panel.snapshot = snapshot;
  const list = byId(panel.id + "-list");
  list.replaceChildren();
  const groups = [["Pending requests", page.requests], ["Active grants", page.grants]];
  for (const [heading, items] of groups) {
    if (!items.length) continue;
    importText(list, "h3", heading);
    for (const item of items) {
      const pending = items === page.requests;
      const card = document.createElement("section");
      card.className = "import-card";
      list.append(card);
      if (panel === imports) renderFileGrant(card, item, pending);
      else renderCloneGrant(card, item);
      const buttons = document.createElement("div");
      buttons.className = "review-buttons";
      card.append(buttons);
      const choices = pending ? [[panel === clones ? "Allow this commit" : "Allow once", "allow_once"],
                                 [panel === clones ? "Allow branch updates" : "Allow standing", "allow_standing"], ["Deny", "deny"]]
        : [["Revoke", "revoke"]];
      for (const [label, action] of choices) {
        const button = importText(buttons, "button", label);
        button.type = "button";
        button.addEventListener("click", () => {
          const endpoint = panel.endpoint + "/" + item.id + (pending ? "/decide" : "/revoke");
          void changeGrant(panel, endpoint, pending ? {decision: action} : {});
        });
      }
    }
  }
  if (closed.length) {
    const details = document.createElement("details");
    importText(details, "summary", "Recently denied or revoked");
    for (const item of closed) {
      const card = document.createElement("section");
      card.className = "import-card";
      details.append(card);
      renderCloneGrant(card, item);
      const button = importText(card, "button", "Reopen request");
      button.type = "button";
      button.className = "concern-release";
      button.addEventListener("click", () => void changeGrant(panel, panel.endpoint + "/" + item.id + "/reopen", {}));
    }
    list.append(details);
  }
  if (receipts.length) {
    const details = document.createElement("details");
    importText(details, "summary", "Recent deliveries");
    for (const receipt of receipts) {
      const record = document.createElement("div");
      record.className = "import-card";
      details.append(record);
      const name = panel === imports ? receipt.filename : receipt.repository + " · " + receipt.branch;
      importText(record, "strong", name + " · " + receipt.size_bytes.toLocaleString() + " bytes");
      importText(record, "p", receipt.path);
      if (panel === clones) {
        if (receipt.previous_commit) importText(record, "p", "Previous commit · " + receipt.previous_commit);
        importText(record, "code", "Delivered commit · " + receipt.commit);
      } else {
        importText(record, "p", receipt.url);
        if (receipt.final_url !== receipt.url) importText(record, "p", "Final URL · " + receipt.final_url);
        importText(record, "code", "Received SHA-256 · " + receipt.sha256);
      }
    }
    list.append(details);
  }
}

async function pollGrants(panel, force = false) {
  if (!authenticated || document.hidden || panel.changing || (!force && (panel.fetching || Date.now() < panel.nextPoll))) return;
  const revision = ++panel.revision;
  panel.fetching = true;
  panel.nextPoll = Date.now() + 10000;
  try {
    const page = await request(panel.endpoint);
    if (authenticated && revision === panel.revision) renderGrants(panel, page);
  } catch {
    if (authenticated && revision === panel.revision) byId(panel.id + "-state").textContent = "Couldn’t refresh " + panel.label + ". Retrying…";
  } finally {
    if (revision === panel.revision) panel.fetching = false;
  }
}

async function changeGrant(panel, path, body, fromForm = false) {
  if (panel.changing || !authenticated) return;
  panel.changing = true;
  ++panel.revision;
  const controls = [...byId(panel.id + "-list").querySelectorAll("button")];
  if (panel === imports) controls.push(...importsForm.elements, byId("imports-new"));
  const enabled = controls.filter(control => !control.disabled);
  for (const control of enabled) control.disabled = true;
  const feedback = byId(fromForm ? "imports-error" : panel.id + "-action-error");
  feedback.textContent = "";
  try {
    await request(path, body);
    if (fromForm) importsDialog.close();
  } catch (error) {
    if (authenticated) feedback.textContent = error.message || "Update unconfirmed. Check the list before retrying.";
  } finally {
    panel.changing = false;
    panel.fetching = false;
    if (authenticated) {
      for (const control of enabled) control.disabled = false;
      await pollGrants(panel, true);
    }
  }
}

function updateImportFields() {
  const rows = [...byId("imports-fields").children];
  rows.forEach((row, index) => {
    row.querySelector("legend").textContent = "File " + (index + 1);
    row.querySelector("button").hidden = rows.length === 1;
  });
  byId("imports-add").disabled = rows.length >= 12;
}

function addImportFile() {
  const row = byId("import-file-fields").content.firstElementChild.cloneNode(true);
  byId("imports-fields").append(row);
  row.querySelector("button").addEventListener("click", () => { row.remove(); updateImportFields(); });
  updateImportFields();
  return row;
}

byId("imports-new").addEventListener("click", () => {
  importsForm.reset();
  byId("imports-fields").replaceChildren();
  addImportFile();
  byId("imports-error").textContent = "";
  byId("imports-sample").hidden = false;
  importsDialog.showModal();
  importsForm.elements.project.focus();
});
for (const panel of [imports, clones]) {
  const element = byId(panel.id + "-panel");
  element.addEventListener("toggle", () => { if (element.open) void pollGrants(panel, true); });
}
byId("imports-cancel").addEventListener("click", () => importsDialog.close());
importsDialog.addEventListener("cancel", event => { if (imports.changing) event.preventDefault(); });
importsForm.addEventListener("input", () => { byId("imports-sample").hidden = true; });
byId("imports-add").addEventListener("click", () => {
  byId("imports-sample").hidden = true;
  addImportFile().querySelector("input").focus();
});
byId("imports-sample").addEventListener("click", () => {
  const row = byId("imports-fields").firstElementChild;
  importsForm.elements.project.value = "cosmology";
  importsForm.elements.collection.value = "WMAP nine-year temperature spectrum";
  row.querySelector("[name=filename]").value = "wmap-spectrum.txt";
  row.querySelector("[name=url]").value = "https://lambda.gsfc.nasa.gov/data/map/dr5/dcp/spectra/wmap_binned_tt_spectrum_9yr_v5.txt";
  row.querySelector("[name=sha256]").value = "6fcf09b3a1dbeb85ae9687bd43e80686ce85017b0b5ce019c44ae5a1337f2616";
  row.querySelector("details").open = true;
  byId("imports-sample").hidden = true;
});
importsForm.addEventListener("submit", event => {
  event.preventDefault();
  const files = [...byId("imports-fields").children].map(row => {
    const field = name => row.querySelector("[name=" + name + "]").value.trim();
    const file = {filename: field("filename"), url: field("url")};
    if (field("sha256")) file.sha256 = field("sha256");
    if (field("redirect_urls")) file.redirect_urls = field("redirect_urls").split(/\r?\n/).map(url => url.trim()).filter(Boolean);
    return file;
  });
  void changeGrant(imports, imports.endpoint, {project: importsForm.elements.project.value.trim(),
    collection: importsForm.elements.collection.value.trim(), files}, true);
});

function closeReview() {
  reviewId = null;
  if (reviewDialog.open) reviewDialog.close();
}

function openReview(item) {
  reviewId = item.id;
  const priorDenial = item.source === "prior_denial";
  const concern = item.tool === "context_concern";
  byId("review-title").textContent = priorDenial ? "Clarify a previous Opus denial" : "Opus needs clarification";
  byId("review-about").textContent = (priorDenial ? "The host is asking about an earlier review. " : "")
    + "Your reply is recorded under your operator identity and quoted to Opus when this "
    + (concern ? "context concern" : "action") + " is reviewed. Lantern will be notified.";
  byId("review-subject").textContent = concern ? "Context concern under review" : "Action under review";
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
  if (!items.length || reviewDialog.open || concernDialog.open || exportDialog.open || document.hidden) return;
  const next = items.find((item) => !deferredReviews.has(item.id) && !stored("review-later." + item.id));
  if (next && !(document.activeElement === draft && draft.value)) openReview(next);
}

async function pollReviews() {
  if (!authenticated || document.hidden || reviewSending || reviewFetching || Date.now() < nextReviewPoll) return;
  const revision = reviewRevision;
  reviewFetching = true;
  try {
    const page = await request("/api/review-questions");
    // A fetch started before a submission can still include the answered question.
    if (revision === reviewRevision && authenticated) {
      const notice = byId("review-notice");
      if (notice.dataset.offline) {
        notice.textContent = "";
        delete notice.dataset.offline;
      }
      updateReviewQueue(page.questions);
    }
  } catch {
    if (authenticated && revision === reviewRevision) {
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
    if (!authenticated) return;
    updateReviewQueue(reviewQuestions.filter((item) => item.id !== id));
    byId("review-notice").textContent = "Your answer to Opus was recorded.";
  } catch (error) {
    if (!authenticated) return;
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

const imageLoads = new WeakMap();
const imageObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (entry.isIntersecting) {
      imageObserver.unobserve(entry.target);
      imageLoads.get(entry.target)();
    }
  }
}, { root: historyView, rootMargin: "160px" });

function messageImage(path, metadata, description) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "message-image";
  button.setAttribute("aria-label", "Open " + description);
  const image = document.createElement("img");
  const scale = Math.min(1, 304 / metadata.width, 192 / metadata.height);
  image.width = Math.max(1, Math.round(scale * metadata.width));
  image.height = Math.max(1, Math.round(scale * metadata.height));
  image.alt = description;
  const retry = document.createElement("span");
  retry.textContent = "Retry loading image";
  retry.hidden = true;
  button.append(image, retry);
  let loading = false;
  async function load() {
    if (loading || !authenticated) return;
    loading = true;
    try {
      const blob = await request(path, undefined, "image");
      if (!authenticated) return;
      image.src = URL.createObjectURL(blob);
      image.hidden = false;
      retry.hidden = true;
      button.setAttribute("aria-label", "Open " + description);
    } catch {
      image.hidden = true;
      retry.hidden = false;
      button.setAttribute("aria-label", retry.textContent);
    } finally {
      loading = false;
    }
  }
  imageLoads.set(button, load);
  imageObserver.observe(button);
  button.addEventListener("click", () => {
    if (image.hasAttribute("src")) {
      byId("image-full").src = image.src;
      imageDialog.showModal();
    } else {
      void load();
    }
  });
  return button;
}
byId("image-close").addEventListener("click", () => imageDialog.close());
imageDialog.addEventListener("close", () => byId("image-full").removeAttribute("src"));

function imageMetadata(event) {
  return event.images || (event.image ? [event.image] : []);
}

function imagePath(event, index) {
  return "/api/messages/" + event.id + (event.images ? "/images/" + index : "/image");
}

function resetExport() {
  const url = exportSave.getAttribute("href");
  if (url) URL.revokeObjectURL(url);
  exportSave.removeAttribute("href");
  exportSave.hidden = true;
  byId("export-submit").hidden = false;
  byId("export-submit").disabled = false;
  byId("export-cancel").textContent = "Cancel";
  byId("export-error").textContent = "";
  byId("export-status").textContent = "";
  exportRange.disabled = false;
  exportDays.disabled = !byId("export-recent").checked;
  byId("export-day-field").hidden = exportDays.disabled;
}

function closeExport() {
  exportRequest?.abort();
  exportRequest = null;
  resetExport();
  if (exportDialog.open) exportDialog.close();
}

byId("export-open").addEventListener("click", () => exportDialog.showModal());
byId("export-cancel").addEventListener("click", closeExport);
exportDialog.addEventListener("cancel", event => { event.preventDefault(); closeExport(); });
window.addEventListener("pagehide", closeExport);
exportRange.addEventListener("input", resetExport);

byId("export-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!authenticated || exportRequest) return;
  resetExport();
  const days = byId("export-recent").checked ? exportDays.valueAsNumber : null;
  const controller = new AbortController();
  const {signal} = controller;
  exportRequest = controller;
  exportRange.disabled = true;
  byId("export-submit").disabled = true;
  const status = byId("export-status");
  status.textContent = "Preparing conversation…";
  let failureMessage = "Couldn’t prepare the Word file. Check the connection and try again.";
  try {
    const transcript = createTranscriptRtf({timeZone: shortTime.resolvedOptions().timeZone});
    let query = days === null ? "" : "?days=" + days;
    let count = 0;
    do {
      const page = await request("/api/export" + query, undefined, "json", signal);
      for (const item of page.events) {
        if (signal.aborted) return;
        const images = await Promise.all(imageMetadata(item).map(async (metadata, index) => {
          try {
            const blob = await request(imagePath(item, index), undefined, "image", signal);
            return {...metadata, bytes: new Uint8Array(await blob.arrayBuffer())};
          } catch (error) {
            failureMessage = "Couldn’t load attachment " + (index + 1) + " for event " + item.id + ". Please retry the export.";
            throw error;
          }
        }));
        if (signal.aborted) return;
        transcript.addEvent(item, images);
        count++;
      }
      if (signal.aborted) return;
      status.textContent = "Prepared " + count + (count === 1 ? " event…" : " events…");
      query = page.next_cursor ? "?" + new URLSearchParams({cursor: page.next_cursor}) : "";
    } while (query);
    if (!count) {
      status.textContent = "No conversation events in this range.";
      return;
    }
    exportSave.href = URL.createObjectURL(transcript.finish());
    exportSave.download = "Lantern-conversation-" + (days === null ? "all" : "last-" + days + (days === 1 ? "-day" : "-days")) + ".rtf";
    exportSave.hidden = false;
    byId("export-submit").hidden = true;
    byId("export-cancel").textContent = "Close";
    status.textContent = "Word file ready · " + count + (count === 1 ? " event." : " events.");
    exportSave.click();
  } catch {
    if (signal.aborted) return;
    controller.abort();
    status.textContent = "";
    byId("export-error").textContent = failureMessage;
  } finally {
    if (exportRequest === controller) {
      exportRequest = null;
      exportRange.disabled = false;
      byId("export-submit").disabled = false;
    }
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
  item.append(header);
  if (role === "operator") {
    const images = imageMetadata(event);
    if (images.length) {
      const gallery = document.createElement("div");
      gallery.className = "message-gallery";
      images.forEach((metadata, i) => {
        const path = imagePath(event, i);
        const description = images.length === 1 ? "attached image" : "attached image " + (i + 1) + " of " + images.length;
        gallery.append(messageImage(path, metadata, description));
      });
      item.append(gallery);
    }
  }
  if (event.text) item.append(body);
  return item;
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
  if (polling || !authenticated) return;
  clearTimeout(timer);
  polling = true;
  let delay = 1500;
  void pollConcerns();
  void pollGrants(imports);
  void pollGrants(clones);
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
    if (authenticated) {
      connection("offline", "Reconnecting");
      byId("worker").textContent = "";
      showCompaction(false);
      if (!initialized) byId("empty").textContent = cookieRoom ? "Waiting for Lantern…" : "Waiting for the SSH connection…";
      delay = 3500;
    }
  } finally {
    polling = false;
    controls();
    if (authenticated) timer = setTimeout(poll, delay);
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
    if (authenticated) byId("announcer").textContent = "Earlier messages could not be loaded. Try again.";
  } finally {
    older.disabled = !authenticated;
  }
});

byId("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (send.disabled) return;
  if (!pending) {
    pending = { text: draft.value, client_id: crypto.randomUUID() };
    if (!saveComposer() && ["composer", "draft", "image-draft", "pending"].some(name => stored(name))) {
      // A reload could restore an older editable draft and issue a new ID.
      // Only submit once the durable state identifies this specific delivery.
      pending = null;
      sendError = "Couldn’t save the send for retry. Nothing was sent. Free browser storage and try again.";
      controls();
      return;
    }
  }
  sending = true;
  sendError = "";
  controls();
  try {
    const body = {...pending};
    if (attachments.length) body.images = attachments.map(({mime_type, data}) => ({mime_type, data}));
    await request("/api/messages", body);
    pending = null;
    attachments = [];
    saveComposer();
    remember("draft", null);
    draft.value = "";
    displayAttachments();
    latest();
    void poll();
  } catch (error) {
    if (error.status === 400 || error.status === 413 || error.status === 409) {
      pending = null;
      saveComposer();
      sendError = error.message;
    } else if (authenticated) {
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
if (cookieRoom) {
  signOut.hidden = false;
  signOut.addEventListener("click", async () => {
    signOut.disabled = true;
    try {
      await request("/api/sign-out", {});
      try { localStorage.removeItem("lantern.signin.epoch"); } catch { /* Optional. */ }
      locked();
    } catch {
      if (authenticated) {
        byId("review-notice").textContent = "Couldn’t confirm sign-out. Check your connection and try again.";
        signOut.disabled = false;
      }
    }
  });
  window.addEventListener("storage", event => {
    if (event.key === "lantern.signin.epoch") locked();
  });
  // A back/forward cache can retain an entire rendered page. Recheck the
  // server before displaying that page on a restored navigation.
  window.addEventListener("pagehide", () => { document.body.hidden = true; });
  window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
} else {
  window.addEventListener("hashchange", () => {
    // Opening a fresh SSH link in this same tab can be a same-document navigation.
    if (new URLSearchParams(location.hash.slice(1)).has("key")) location.reload();
  });
}
if (pending) sendError = "A previous send is unconfirmed. Retry send to check delivery.";
displayAttachments();
if (authenticated) void poll();
else locked();
