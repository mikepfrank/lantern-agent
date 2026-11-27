// Test the complete user download with mock room events and real PNG bytes.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const {chromium} = require("playwright");

async function exercise(browser, cookieRoom) {
  const context = await browser.newContext({viewport: {width: cookieRoom ? 1100 : 360, height: 760}});
  try {
    const page = await context.newPage();
    const errors = [], exports = [], downloads = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("download", file => downloads.push(file));
    const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jKWQAAAAASUVORK5CYII=", "base64");
    const image = {mime_type: "image/png", width: 1, height: 1};
    const at = "2026-11-26T13:15:00Z";
    const old = {at, id: 1, role: "operator", text: "Earliest event", image};
    const latest = {at, id: 7, role: "lantern", text: "Latest **message**"};
    const service = {at, id: 6, role: "system", text: "Service notice"};
    const origin = cookieRoom ? "https://fixture.test" : "http://127.0.0.1:18765";
    const assets = ["index.html", "room.css", "room.js", "transcript-rtf.js", "message-format.js", "markdown-it-15.0.3.min.js"];
    let missingImage = false, pendingPage;
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin);
      if (url.pathname.startsWith("/api/")) {
        const headers = await route.request().allHeaders();
        assert.equal(headers.authorization, cookieRoom ? undefined : "Bearer fixture-key");
      }
      const reply = json => route.fulfill({json});
      if (url.pathname === "/api/room") return reply({events: [latest], status: {online: true}});
      if (url.pathname === "/api/concerns") return reply({concerns: [], as_of: null});
      if (url.pathname === "/api/review-questions") return reply({questions: []});
      if (["/api/file-imports","/api/repository-clones"].includes(url.pathname)) return reply({requests:[],grants:[]});
      if (url.pathname === "/api/messages/1/image") {
        return missingImage ? route.fulfill({status: 404, json: {error: "Missing image"}})
          : route.fulfill({body: png, contentType: "image/png"});
      }
      if (url.pathname === "/api/export") {
        exports.push(url.search);
        if (url.searchParams.get("cursor") === "fixed+page/2") return reply({events: [service, latest], next_cursor: null});
        if (url.searchParams.get("days") === "2") return reply({events: [], next_cursor: null});
        if (url.searchParams.get("days") === "3") { pendingPage = route; return; }
        if (url.searchParams.get("days") === "7") return reply({events: [service, latest], next_cursor: null});
        assert.equal(url.search, "");
        return reply({events: [old], next_cursor: "fixed+page/2"});
      }
      const file = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      assert.ok(assets.includes(file), file);
      return route.fulfill({path: path.join(__dirname, "../lantern/web_assets", file)});
    });
    await page.goto(origin + (cookieRoom ? "/" : "/#key=fixture-key"));
    const open = page.getByRole("button", {name: "Export conversation"});
    await open.click();
    const dialog = page.getByRole("dialog", {name: "Export conversation"});
    const submit = dialog.getByRole("button", {name: /Download Word file/});
    const download = async () => {
      const next = page.waitForEvent("download");
      await submit.click();
      const file = await next;
      return {name: file.suggestedFilename(), rtf: await fs.readFile(await file.path(), "ascii")};
    };
    const all = await download();
    assert.equal(all.name, "Lantern-conversation-all.rtf");
    assert.deepEqual(exports, ["", "?cursor=fixed%2Bpage%2F2"]);
    assert.ok(all.rtf.startsWith("{\\rtf1"));
    assert.ok(all.rtf.includes("\\pngblip"));
    assert.ok(all.rtf.includes("Earliest event"));
    assert.ok(all.rtf.indexOf("Earliest event") < all.rtf.indexOf("Service notice"));
    assert.ok(all.rtf.indexOf("Service notice") < all.rtf.indexOf("Latest "));
    await dialog.getByLabel("Last N days").check();
    const days = dialog.getByLabel("Number of days");
    await days.fill("0");
    await submit.click();
    assert.equal(exports.length, 2, "invalid range cannot request an export");
    await days.fill("7");
    const recent = await download();
    assert.equal(recent.name, "Lantern-conversation-last-7-days.rtf");
    assert.ok(recent.rtf.includes("Service notice"));
    assert.ok(!recent.rtf.includes("Earliest event"));
    await days.fill("2");
    await submit.click();
    await dialog.getByText("No conversation events in this range.").waitFor();
    await days.fill("3");
    const pending = page.waitForRequest(req => req.url().endsWith("/api/export?days=3"));
    await submit.click();
    await pending;
    await page.keyboard.press("Escape");
    await open.click();
    await dialog.getByLabel("Entire conversation").check();
    missingImage = true;
    await submit.click();
    await dialog.getByText(/Couldn’t load attachment 1 for event 1/).waitFor();
    await pendingPage.fulfill({json: {events: [latest], next_cursor: null}}).catch(() => {});
    assert.equal(downloads.length, 2);
    assert.equal(await dialog.getByRole("link", {name: "Download again"}).count(), 0);
    assert.deepEqual(errors, []);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.LANTERN_CHROME, headless: true});
  try {
    await Promise.all([exercise(browser, true), exercise(browser, false)]);
    console.log("Full and filtered downloads, pagination, attachments, empty range, cancellation and retry passed in both rooms.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
