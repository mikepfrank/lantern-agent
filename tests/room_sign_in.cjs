// Optional Chromium regression. Uses only local fixtures and a disposable browser profile.
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.LANTERN_CHROME, headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 390, height: 750 } });
    const origin = "https://lantern.test";
    const assets = path.join(__dirname, "../lantern/web_assets");
    let session = 0, active = false;
    const errors = [];
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    await context.route("**/*", async route => {
      const req = route.request(), url = new URL(req.url());
      assert.equal(url.origin, origin);
      const headers = await req.allHeaders();
      assert.equal(headers.authorization, undefined);
      const signedIn = active && headers.cookie === "room=" + session;
      const reply = (status, json) => route.fulfill({ status, json });
      if (url.pathname === "/api/sign-in") {
        if (req.postDataJSON().password !== "test password") return reply(401, {});
        active = true;
        session++;
        await context.addCookies([{ url: origin, name: "room", value: String(session), secure: true, httpOnly: true }]);
        return reply(200, { ok: true });
      }
      if (url.pathname.startsWith("/api/") && !signedIn) return reply(401, {});
      if (url.pathname === "/api/sign-out") {
        assert.equal(req.method(), "POST");
        active = false;
        await context.clearCookies();
        return reply(200, { ok: true });
      }
      if (url.pathname === "/api/room") {
        const events = url.search ? [] : [{ id: 1, at: "2026-11-21T12:00:00Z", role: "operator", text: "Private fixture" }];
        return reply(200, { events, more: false, status: { online: true } });
      }
      if (url.pathname === "/api/review-questions") return reply(200, { questions: [] });
      if (url.pathname === "/api/file-imports") return reply(200, { requests: [], grants: [] });
      if (url.pathname === "/api/concerns") return reply(200, { concerns: [], as_of: null });
      const file = url.pathname === "/" ? (signedIn ? "index.html" : "login.html") : url.pathname.slice(1);
      assert.ok(["login.html", "login.js", "index.html", "room.css", "room.js", "message-format.js", "markdown-it-15.0.3.min.js"].includes(file));
      return route.fulfill({ path: path.join(assets, file), headers: { "Cache-Control": "no-store" } });
    });
    const password = page.getByLabel("Password");
    const signIn = async () => {
      await password.fill("test password");
      await page.getByRole("button", { name: "Sign in" }).click();
      await page.getByText("Private fixture").waitFor();
    };
    await page.goto(origin + "/#key=unused");
    assert.equal(await page.title(), "Lantern · Administrator sign-in");
    assert.equal(page.url(), origin + "/");
    await password.fill("invalid");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.getByText("That password wasn’t accepted. Try again.").waitFor();
    await signIn();
    await page.getByLabel("Message Lantern").fill("Unsent private draft");
    await page.reload();
    await page.getByText("Private fixture").waitFor();
    assert.equal(await page.getByLabel("Message Lantern").inputValue(), "Unsent private draft");
    const other = await context.newPage();
    await other.goto(origin);
    await other.getByText("Private fixture").waitFor();
    await other.getByRole("button", { name: "Sign out" }).click();
    await other.getByLabel("Password").waitFor();
    await password.waitFor();
    assert.equal(await page.getByText("Private fixture").count(), 0);
    assert.equal(await page.evaluate(() => Object.keys(sessionStorage).some(key => key.startsWith("lantern.room."))), false);
    await signIn();
    assert.equal(await page.getByLabel("Message Lantern").inputValue(), "");
    await page.getByLabel("Message Lantern").fill("Another private draft");
    active = false;
    await password.waitFor();
    assert.equal(await page.evaluate(() => sessionStorage.getItem("lantern.room.draft")), null);
    assert.deepEqual(errors, []);
    console.log("Administrator sign-in, refresh, cross-tab sign-out, expiry, and draft cleanup passed.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
