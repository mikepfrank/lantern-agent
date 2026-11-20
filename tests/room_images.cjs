// Optional browser regression: node tests/room_images.cjs with playwright installed.
// Set LANTERN_CHROME to use a system Chromium executable. All requests are fixtures.
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");

async function exercise(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: 750 } });
  try {
    const page = await context.newPage();
    const errors = [], posts = [], rows = [];
    let lastImage, loseReceipt = true, imageReads = 0;
    page.on("pageerror", error => errors.push(error.message));
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, "http://127.0.0.1:18765");
      const send = (value, status = 200) => route.fulfill({ status, contentType: "application/json", json: value });
      if (url.pathname.startsWith("/api/")) {
        assert.equal((await route.request().allHeaders()).authorization, "Bearer fixture-key");
        if (url.pathname === "/api/room") {
          const after = Number(url.searchParams.get("after")) || 0;
          return send({ events: rows.filter(row => row.id > after), more: false, status: { online: true } });
        }
        if (url.pathname === "/api/review-questions") return send({ questions: [] });
        if (url.pathname === "/api/messages") {
          const body = route.request().postDataJSON();
          posts.push(body);
          if (!rows.some(row => row.client_id === body.client_id)) {
            const image = body.image && { mime_type: "image/png", width: 1600, height: 640 };
            if (image) lastImage = Buffer.from(body.image.data, "base64");
            rows.push({ id: rows.length + 1, text: body.text, image, role: "operator", client_id: body.client_id,
              at: "2026-11-19T12:00:00Z" });
          }
          if (loseReceipt) { loseReceipt = false; return route.abort(); }
          return send({ id: rows.length });
        }
        if (/^\/api\/messages\/\d+\/image$/.test(url.pathname)) {
          imageReads++;
          return route.fulfill({ contentType: "image/png", body: lastImage });
        }
        throw Error(url.pathname);
      }
      const file = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      assert.ok(["index.html", "room.css", "room.js", "message-format.js", "markdown-it-15.0.3.min.js"].includes(file));
      return route.fulfill({ path: path.join(__dirname, "../lantern/web_assets", file), headers: {
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data: blob:"
      } });
    });
    await page.goto("http://127.0.0.1:18765/#key=fixture-key");
    const draft = page.locator("#draft");
    const picker = page.locator("#image-file");
    await page.waitForFunction(() => !document.querySelector("#attach").disabled);
    const data = await page.evaluate(() => {
      const canvas = document.createElement("canvas");
      canvas.width = 2000;
      canvas.height = 800;
      const draw = canvas.getContext("2d");
      draw.fillStyle = "#192f31";
      draw.fillRect(0, 0, 2000, 800);
      draw.strokeStyle = "#e9bd7d";
      draw.lineWidth = 22;
      draw.beginPath();
      draw.moveTo(430, 550);
      draw.lineTo(1570, 250);
      draw.stroke();
      draw.fillStyle = "#ebc595";
      draw.fillRect(330, 450, 200, 200);
      draw.fillStyle = "#a1c6ae";
      draw.beginPath();
      draw.arc(1570, 250, 115, 0, Math.PI * 2);
      draw.fill();
      return canvas.toDataURL("image/jpeg").split(",")[1];
    });
    const file = { name: "diagram.jpg", mimeType: "image/jpeg", buffer: Buffer.from(data, "base64") };
    const choose = async () => { await picker.setInputFiles(file); await page.getByText("1600 × 640 · Resized").waitFor(); };
    await picker.setInputFiles({ name: "bad.txt", mimeType: "text/plain", buffer: Buffer.from("sample") });
    await page.getByText("Choose a PNG, JPEG or WebP image.").waitFor();
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Attach image", exact: true }).click();
    await (await chooser).setFiles(file);
    await page.getByText("1600 × 640 · Resized").waitFor();
    await page.getByRole("button", { name: "Remove attached image" }).click();
    assert.equal(await page.locator("#send").isEnabled(), false);
    await choose();
    await draft.fill("Which two shapes are connected?");
    if (process.env.LANTERN_UI_SCREENSHOTS) {
      await page.screenshot({ path: path.join(process.env.LANTERN_UI_SCREENSHOTS, "image-compose-" + width + ".png") });
    }
    await page.reload();
    await page.getByText("diagram.jpg").waitFor();
    await page.locator("#send").click();
    await page.getByText("Delivery unconfirmed. Retry send to check or deliver this message.").waitFor();
    await page.reload();
    await page.getByRole("button", { name: "Retry send" }).click();
    await page.waitForFunction(() => !document.querySelector("#draft").value);
    assert.equal(posts.length, 2);
    assert.deepEqual(posts[0], posts[1]);
    assert.equal(posts[0].text, "Which two shapes are connected?");
    assert.equal(posts[0].image.mime_type, "image/png");
    assert.ok(lastImage.length <= 1024 * 1024);
    assert.equal(lastImage.readUInt32BE(16), 1600);
    assert.equal(lastImage.readUInt32BE(20), 640);
    await page.reload();
    const thumb = page.getByRole("button", { name: "Open attached image" }).first();
    await thumb.locator("img[src^='blob:']").waitFor();
    await thumb.click();
    await page.locator("#image-dialog[open]").waitFor();
    assert.ok((await page.locator("#image-dialog").boundingBox()).width <= width);
    await page.keyboard.press("Escape");
    await choose();
    await page.locator("#send").click();
    await page.waitForFunction(() => document.querySelector("#image-draft").hidden);
    assert.equal(posts[2].text, "");
    assert.ok(posts[2].image);
    await draft.fill("Plain text follow-up");
    await page.locator("#send").click();
    await page.waitForFunction(() => !document.querySelector("#draft").value);
    assert.equal(posts[3].text, "Plain text follow-up");
    assert.equal(posts[3].image, undefined);
    assert.ok(imageReads >= 1);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    console.log(width + "px: image picker, resize, removal, caption, durable retry, image-only, history, zoom, and text passed");
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.LANTERN_CHROME || undefined, headless: true });
  try {
    for (const width of [1100, 390]) await exercise(browser, width);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
