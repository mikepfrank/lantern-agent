// Standing concerns in two room layouts. All pages, ledger reads and releases are fixtures.
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");

async function exercise(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: 760 } });
  try {
    const page = await context.newPage();
    const posts = [], errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const item = {id: 13, version: 1, status: "watch", tools: ["workspace_write"], reason: "Keep quotes brief.",
      summary: "Private reading notes", evidence: "<i>fixture evidence</i>", release_pending: false};
    let items = [], asOf = null, failRead = false;
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, "http://127.0.0.1:18765");
      const reply = (json, status = 200) => route.fulfill({json, status});
      if (url.pathname === "/api/room") return reply({events: [], more: false, status: {online: true}});
      if (url.pathname === "/api/review-questions") return reply({questions: []});
      if (url.pathname === "/api/file-imports") return reply({requests: [], grants: []});
      if (url.pathname === "/api/concerns") return failRead ? reply({}, 503) : reply({concerns: items, as_of: asOf});
      if (url.pathname === "/api/concerns/13/release") {
        assert.equal(route.request().method(), "POST");
        assert.equal((await route.request().allHeaders()).authorization, "Bearer fixture-key");
        const body = route.request().postDataJSON();
        posts.push(body);
        if (body.version !== items[0].version) return reply({}, 409);
        items = [{...items[0], release_pending: true}];
        return reply({id: 123}, 202);
      }
      const name = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      assert.ok(["index.html", "room.css", "room.js", "message-format.js", "markdown-it-15.0.3.min.js"].includes(name));
      return route.fulfill({path: path.join(__dirname, "../lantern/web_assets", name)});
    });
    await page.goto("http://127.0.0.1:18765/#key=fixture-key");
    const toggle = page.locator("#concern-toggle");
    assert.equal(await toggle.getAttribute("aria-expanded"), String(width > 864));
    if (width < 865) await toggle.click();
    await page.getByText("Waiting for the first worker snapshot…").waitFor();
    const refreshPanel = async () => { await toggle.click(); await toggle.click(); };
    asOf = new Date(Date.now() - 6 * 60000).toISOString();
    await refreshPanel();
    await page.getByText("The last snapshot showed no standing concerns. Waiting for a current worker snapshot.").waitFor();
    assert.match(await toggle.textContent(), /0 last seen/);
    await page.getByLabel("Message Lantern").fill("Keep this draft.");
    items = [item];
    asOf = new Date().toISOString();
    // An ordinary poll must pick this up when the room has zero chat events.
    await page.getByText(item.summary, {exact: true}).waitFor({timeout: 15000});
    assert.equal(await page.locator("#draft").evaluate(node => node === document.activeElement), true);
    const toggleDetails = page.getByText("Review details", {exact: true});
    await toggleDetails.click();
    await page.getByText(item.evidence).waitFor();
    assert.equal(await page.locator(".concern-evidence i").count(), 0);
    failRead = true;
    await refreshPanel();
    await page.getByText("Couldn’t refresh concerns. Retrying…").waitFor();
    assert.equal(await page.getByText(item.evidence).isVisible(), true);
    failRead = false;
    await refreshPanel();
    await page.waitForFunction(() => !document.querySelector("#concern-state").textContent);
    const overrule = page.getByRole("button", {name: "Overrule concern #13", exact: true});
    await overrule.click();
    const dialog = page.getByRole("dialog", {name: "Overrule concern #13?"});
    const cancel = dialog.getByRole("button", {name: "Cancel"});
    assert.equal(await cancel.evaluate(node => node === document.activeElement), true);
    await cancel.click();
    assert.deepEqual(posts, []);
    await overrule.click();
    const reason = dialog.getByLabel("Reason (optional)");
    await reason.fill("Operator ruling.");
    items = [{...item, version: 2, reason: "New wording."}];
    const submit = dialog.getByRole("button", {name: "Overrule concern", exact: true});
    await submit.click();
    await dialog.getByText("The concern has changed. Close this dialog to see its current status.").waitFor();
    assert.equal(await submit.isDisabled(), true);
    await cancel.click();
    await page.getByText("v2 · New wording.", {exact: true}).waitFor();
    await overrule.click();
    await reason.fill("Resolved privately.");
    await submit.click();
    await page.getByText("Release requested", {exact: true}).waitFor();
    assert.equal(await overrule.isDisabled(), true);
    assert.deepEqual(posts, [{version: 1, reason: "Operator ruling."}, {version: 2, reason: "Resolved privately."}]);
    items = [];
    await refreshPanel();
    await page.getByText("No standing concerns.").waitFor();
    assert.equal(await overrule.count(), 0);
    assert.equal(await page.getByLabel("Message Lantern").inputValue(), "Keep this draft.");
    const room = await page.locator("main").boundingBox(), sidebar = await page.locator(".concerns").boundingBox();
    if (width > 864) assert.ok(sidebar.x >= room.x + room.width);
    else assert.ok(room.y >= sidebar.y + sidebar.height - 1);
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.LANTERN_CHROME, headless: true});
  try {
    await Promise.all([exercise(browser, 1280), exercise(browser, 390)]);
    console.log("Desktop and mobile concern polling, release, cancellation and stale-version handling passed.");
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
