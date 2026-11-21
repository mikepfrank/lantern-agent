"use strict";

const form = document.getElementById("login-form");
const password = document.getElementById("password");
const submit = document.getElementById("login-submit");
const error = document.getElementById("login-error");

// The server serves this page only to a signed-out browser. A revoked or
// expired session may have left private drafts or a pending send in this tab.
try {
  for (const key of Object.keys(sessionStorage)) {
    if (key.startsWith("lantern.room.")) sessionStorage.removeItem(key);
  }
} catch { /* Signing in also works when browser storage is disabled. */ }
if (location.hash) history.replaceState(null, "", location.pathname);
submit.disabled = false;

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submit.disabled) return;
  error.textContent = "";
  submit.disabled = true;
  submit.textContent = "Signing in…";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch("/api/sign-in", {
      method: "POST", credentials: "same-origin", cache: "no-store", redirect: "error",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password.value }),
    });
    if (response.ok) {
      password.value = "";
      // This local marker separates browser drafts across sign-ins. It grants no access.
      try { localStorage.setItem("lantern.signin.epoch", crypto.randomUUID()); } catch { /* Optional. */ }
      location.replace("/");
      return;
    }
    if (response.status === 401) {
      error.textContent = "That password wasn’t accepted. Try again.";
      password.focus();
      password.select();
    } else if (response.status === 429) {
      error.textContent = "Too many sign-in attempts. Try again later.";
    } else {
      error.textContent = "Sign-in is temporarily unavailable. Try again shortly.";
    }
  } catch {
    error.textContent = "Couldn’t reach Lantern. Check your connection and try again.";
  } finally {
    clearTimeout(timeout);
  }
  submit.textContent = "Sign in";
  submit.disabled = false;
});

window.addEventListener("pagehide", () => { password.value = ""; });
window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
