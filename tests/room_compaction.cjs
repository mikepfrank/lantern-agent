// Exercise the room's normal poll loop with an idle timeline during compaction.
const assert = require("node:assert/strict");
const path = require("node:path");
const {chromium} = require("playwright");

async function exercise(browser, width) {
  const context = await browser.newContext({viewport: {width, height: 760}});
  try {
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const at = "2026-11-27T19:00:00Z";
    const rows = Array.from({length: 24}, (_, i) => ({id: i + 1, role: "lantern", at, text: `Message ${i + 1}`}));
    const markers = [0, 2, 3].map((anchor, i) => ({after_event_id: anchor || null, at,
      ref: "annotation:" + (i + 1), role: "annotation", text: "Reconstructed checkpoint " + (i + 1)}));
    let compacting = false, connected = true;
    await context.route("http://127.0.0.1:18765/**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/room") {
        if (!connected) return route.fulfill({status: 503, json: {error: "Fixture disconnected"}});
        const after = url.searchParams.get("after"), before = url.searchParams.get("before");
        const events = after !== null ? rows.filter(row => row.id > Number(after))
          : before !== null ? rows.filter(row => row.id < Number(before)) : rows.slice(2);
        const annotations = after !== null ? [] : before !== null ? markers.slice(0, 2) : markers.slice(2);
        return route.fulfill({json: {events, annotations, more: after === null && before === null,
          status: {online: true, working: true, compacting, queued: 0}}});
      }
      if (url.pathname.startsWith("/api/")) {
        return route.fulfill({json: {questions: [], concerns: [], requests: [], grants: []}});
      }
      const name = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      return route.fulfill({path: path.join(__dirname, "../lantern/web_assets", name)});
    });
    await page.goto("http://127.0.0.1:18765/#key=fixture-key");
    await page.getByText("Message 24", {exact: true}).waitFor();
    await page.reload();
    await page.getByText("Reconstructed checkpoint 3", {exact: true}).waitFor();
    await page.locator("#older").click();
    await page.getByText("Reconstructed checkpoint 1", {exact: true}).waitFor();
    assert.deepEqual((await page.locator("#events .body").allTextContents()).slice(0, 7),
      ["Reconstructed checkpoint 1", "Message 1", "Message 2", "Reconstructed checkpoint 2",
        "Message 3", "Reconstructed checkpoint 3", "Message 4"]);
    assert.equal(await page.locator(".annotation .speaker").first().textContent(), "Historical annotation");
    assert.equal(await page.evaluate(() => sessionStorage.getItem("lantern.room.cursor")), "24");
    await page.locator("#history").evaluate(h => h.scrollTop = h.scrollHeight);
    const label = page.locator("#compaction-status"), composer = page.locator("#composer");
    assert.equal(await label.textContent(), "");
    await page.getByLabel("Message Lantern").fill("Keep my draft.");
    compacting = true;
    await page.getByText("Lantern’s agent harness is compacting the context…", {exact: true}).waitFor();
    assert.equal(await label.getAttribute("role"), "status");
    const indicator = await label.boundingBox(), input = await composer.boundingBox();
    assert.ok(indicator.height > 0 && indicator.y + indicator.height <= input.y + 1);
    assert.equal(await page.locator("#events article").count(), 27);
    await page.waitForFunction(() => {
      const h = document.querySelector("#history");
      return h.scrollHeight - h.clientHeight - h.scrollTop < 2;
    });
    await page.locator("#history").evaluate(h => h.scrollTop = 0);
    await page.locator("#jump").waitFor();
    connected = false;
    await page.getByText("Reconnecting", {exact: true}).waitFor();
    assert.equal(await label.textContent(), "");
    connected = true;
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await page.getByText("Lantern’s agent harness is compacting the context…", {exact: true}).waitFor();
    rows.push({id: 25, role: "system", at, text: "Context compaction completed."});
    compacting = false;
    await page.locator(".message.system").getByText("Context compaction completed.").waitFor();
    assert.equal(await label.textContent(), "");
    assert.equal(await page.locator(".message.system .speaker").textContent(), "Service");
    assert.equal(await page.locator("#history").evaluate(h => h.scrollTop), 0);
    assert.equal(await page.getByLabel("Message Lantern").inputValue(), "Keep my draft.");
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.LANTERN_CHROME, headless: true});
  try {
    await Promise.all([exercise(browser, 1280), exercise(browser, 390)]);
    console.log("Desktop and mobile compaction status, reconnect and Service completion passed.");
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
