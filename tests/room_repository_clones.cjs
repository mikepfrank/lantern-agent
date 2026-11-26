// An agent proposes repositories while the operator's private room is open.
const assert = require("node:assert/strict");
const path = require("node:path");
const {chromium} = require("playwright");

async function exercise(browser, width) {
  const context = await browser.newContext({viewport: {width, height: 760}});
  try {
    const page = await context.newPage();
    const errors = [], changes = [];
    page.on("pageerror", error => errors.push(error.message));
    const base = "/api/repository-clones";
    const overview = {requests: [], grants: [], closed: [], receipts: []};
    const first = {id: 1, repository: "fictional/ephemeris", branch: "release/v1",
      commit: "a1".repeat(20), purpose: "Study <tables> and orbital corrections.", status: "pending"};
    const second = {...first, id: 2, repository: "example/land-survey", branch: "maps"};
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, "https://fixture.test");
      const reply = json => route.fulfill({json});
      if (url.pathname === "/api/room") return reply({events: [], status: {online: true}});
      if (url.pathname === "/api/concerns") return reply({concerns: [], as_of: null});
      if (url.pathname === "/api/review-questions") return reply({questions: []});
      if (url.pathname === "/api/file-imports") return reply({requests: [], grants: []});
      if (url.pathname === base) return reply(overview);
      if (url.pathname.startsWith(base + "/")) {
        const [id, action] = url.pathname.slice(base.length + 1).split("/");
        const body = route.request().postDataJSON();
        changes.push([Number(id), action, body]);
        const take = items => items.splice(items.findIndex(item => item.id === Number(id)), 1)[0];
        if (action === "reopen") overview.requests.push({...overview.closed[0], id: 3, status: "pending"});
        else if (action === "revoke") overview.closed.unshift({...take(overview.grants), status: "revoked"});
        else if (body.decision === "deny") overview.closed.unshift({...take(overview.requests), status: "denied"});
        else overview.grants.push({...take(overview.requests), status: "approved", mode: body.decision === "allow_once" ? "once" : "standing"});
        return reply({ok: true});
      }
      const name = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      assert.ok(["index.html", "room.css", "room.js", "message-format.js", "markdown-it-15.0.3.min.js"].includes(name));
      return route.fulfill({path: path.join(__dirname, "../lantern/web_assets", name)});
    });
    await page.goto("https://fixture.test/");
    const panel = page.locator("#clones-panel");
    await page.waitForFunction(() => document.getElementById("clones-state").textContent.startsWith("No pending"));
    overview.requests.push(first, second);
    await page.waitForFunction(() => document.getElementById("clones-count").textContent.includes("2 pending"));
    await panel.locator(":scope > summary").click();
    const card = panel.locator("section").filter({hasText: first.repository});
    await card.getByText("Branch · release/v1", {exact: true}).waitFor();
    await card.getByText("Requested commit · " + first.commit).waitFor();
    await card.getByText("Lantern’s purpose · " + first.purpose, {exact: true}).waitFor();
    assert.equal(await card.getByRole("link").getAttribute("href"), "https://github.com/" + first.repository + "/tree/" + first.commit);
    await card.getByRole("button", {name: "Allow branch updates"}).click();
    await card.getByText("Updates approved for this branch.").waitFor();
    await card.getByRole("button", {name: "Revoke"}).click();
    await panel.locator("summary").filter({hasText: "Recently denied or revoked"}).click();
    await card.getByText("Revoked.", {exact: true}).waitFor();
    await card.getByRole("button", {name: "Reopen request"}).click();
    const reopened = panel.locator("section").filter({hasText: "Request #3"});
    await reopened.getByRole("button", {name: "Allow this commit"}).click();
    await panel.getByText("Only this commit is approved.").waitFor();
    const delivered = "b2".repeat(20);
    const location = "/work/repos/" + first.repository + "/" + delivered;
    overview.receipts.push({...first, previous_commit: first.commit, commit: delivered, size_bytes: 420,
      path: location, grant_id: 1});
    await panel.locator("section").filter({hasText: second.repository}).getByRole("button", {name: "Deny"}).click();
    await panel.getByText("Recent deliveries", {exact: true}).click();
    await panel.getByText("Previous commit · " + first.commit).waitFor();
    await panel.getByText("Delivered commit · " + delivered).waitFor();
    await panel.getByText(location, {exact: true}).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(changes, [[1, "decide", {decision: "allow_standing"}], [1, "revoke", {}],
      [1, "reopen", {}], [3, "decide", {decision: "allow_once"}], [2, "decide", {decision: "deny"}]]);
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.LANTERN_CHROME, headless: true});
  try {
    await Promise.all([exercise(browser, 1280), exercise(browser, 390)]);
    console.log("Desktop and mobile repository requests, decisions, reconsideration and checkout receipts passed.");
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
