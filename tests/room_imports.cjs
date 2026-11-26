// Exercise the operator's import controls without contacting an external source.
const assert = require("node:assert/strict");
const path = require("node:path");
const {chromium} = require("playwright");

async function exercise(browser, width) {
  const context = await browser.newContext({viewport: {width, height: 760}});
  try {
    const page = await context.newPage();
    const errors = [], changes = [];
    page.on("pageerror", error => errors.push(error.message));
    const overview = {requests: [], grants: [], receipts: []};
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, "https://fixture.test");
      const reply = json => route.fulfill({json});
      if (url.pathname === "/api/room") return reply({events: [], status: {online: true}});
      if (url.pathname === "/api/concerns") return reply({concerns: [], as_of: null});
      if (url.pathname === "/api/review-questions") return reply({questions: []});
      if (url.pathname.startsWith("/api/file-imports")) {
        if (route.request().method() === "GET") return reply(overview);
        const body = route.request().postDataJSON();
        changes.push([url.pathname, body]);
        if (url.pathname === "/api/file-imports") overview.requests.push({id: 1, ...body});
        else if (url.pathname.endsWith("/decide")) overview.grants.push({...overview.requests.pop(), mode: "standing"});
        else if (url.pathname.endsWith("/revoke")) overview.grants.pop();
        return reply({ok: true});
      }
      const name = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      assert.ok(["index.html", "room.css", "room.js", "message-format.js", "markdown-it-15.0.3.min.js"].includes(name));
      return route.fulfill({path: path.join(__dirname, "../lantern/web_assets", name)});
    });
    await page.goto("https://fixture.test/");
    await page.locator("#imports-panel > summary").click();
    await page.getByRole("button", {name: "Add files…"}).click();
    const dialog = page.getByRole("dialog", {name: "Request a file import"});
    await dialog.getByRole("button", {name: "Use NASA’s WMAP spectrum"}).click();
    assert.equal(await dialog.getByLabel("Filename").inputValue(), "wmap-spectrum.txt");
    assert.match(await dialog.getByLabel("Expected SHA-256").inputValue(), /^[a-f0-9]{64}$/);
    await dialog.getByRole("button", {name: "Add another file"}).click();
    const extra = dialog.getByRole("group", {name: "File 2"});
    await extra.getByLabel("Filename").fill("readme.txt");
    await extra.getByLabel("Exact HTTPS URL").fill("https://example.org/science/readme.txt");
    await extra.getByText("Checksum and redirects").click();
    await extra.getByLabel("Permitted redirect URLs").fill("https://example.org/science/v2/readme.txt");
    assert.deepEqual(changes, []);
    await dialog.getByRole("button", {name: "Review request"}).click();
    await page.getByRole("heading", {name: "Pending requests"}).waitFor();
    assert.equal(changes.length, 1);
    assert.equal(changes[0][1].files.length, 2);
    await page.getByText("Allowed redirect · https://example.org/science/v2/readme.txt").waitFor();
    await page.getByRole("button", {name: "Allow standing"}).click();
    await page.getByRole("heading", {name: "Active grants"}).waitFor();
    await page.getByRole("button", {name: "Revoke"}).click();
    await page.getByText("No pending requests or active grants.").waitFor();
    assert.deepEqual(changes.slice(1), [
      ["/api/file-imports/1/decide", {decision: "allow_standing"}], ["/api/file-imports/1/revoke", {}]
    ]);
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({executablePath: process.env.LANTERN_CHROME, headless: true});
  try {
    await Promise.all([exercise(browser, 1280), exercise(browser, 390)]);
    console.log("Desktop and mobile import proposal, approval and revocation passed.");
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
