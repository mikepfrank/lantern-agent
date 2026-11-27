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
    const payloads = new Map();
    let loseReceipt = true, imageReads = 0;
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
        if (["/api/file-imports", "/api/repository-clones"].includes(url.pathname)) return send({ requests: [], grants: [] });
        if (url.pathname === "/api/concerns") return send({ concerns: [], as_of: null });
        if (url.pathname === "/api/messages") {
          const body = route.request().postDataJSON();
          posts.push(body);
          if (!rows.some(row => row.client_id === body.client_id)) {
            const id = rows.length + 1;
            const images = (body.images || []).map((image, index) => {
              const bytes = Buffer.from(image.data, "base64");
              payloads.set(id + "/images/" + index, bytes);
              return {mime_type: image.mime_type, width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20)};
            });
            rows.push({ id, text: body.text, images, role: "operator", client_id: body.client_id,
              at: "2026-11-19T12:00:00Z" });
          }
          if (loseReceipt) { loseReceipt = false; return route.abort(); }
          return send({ id: rows.length });
        }
        if (/^\/api\/messages\/\d+\/images\/\d+$/.test(url.pathname)) {
          imageReads++;
          return route.fulfill({ contentType: "image/png", body: payloads.get(url.pathname.slice("/api/messages/".length)) });
        }
        throw Error(url.pathname);
      }
      const file = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      assert.ok(["index.html", "room.css", "room.js", "transcript-rtf.js", "message-format.js", "markdown-it-15.0.3.min.js"].includes(file));
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
    const counts = async n => page.waitForFunction(n => document.querySelectorAll("#image-draft li").length === n, n);
    const choose = async () => { await picker.setInputFiles(file); await counts(1); };
    const paste = async (names) => page.evaluate(names => {
      const clipboard = new DataTransfer();
      for (const name of names) {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 16;
        const draw = canvas.getContext("2d");
        draw.fillStyle = name.startsWith("blue") ? "blue" : "red";
        draw.fillRect(0, 0, 16, 16);
        const bytes = Uint8Array.from(atob(canvas.toDataURL().split(",")[1]), char => char.charCodeAt(0));
        clipboard.items.add(new File([bytes], name, {type: "image/png"}));
      }
      return document.querySelector("#draft").dispatchEvent(new ClipboardEvent("paste", {
        bubbles: true, cancelable: true, clipboardData: clipboard
      }));
    }, names);
    await picker.setInputFiles({ name: "bad.txt", mimeType: "text/plain", buffer: Buffer.from("sample") });
    await page.getByText("Choose a PNG, JPEG or WebP image.").waitFor();
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Attach images", exact: true }).click();
    await (await chooser).setFiles(file);
    await page.getByText("1600 × 640 · Resized").waitFor();
    await page.getByRole("button", { name: "Remove image 1, diagram.jpg" }).click();
    assert.equal(await page.locator("#send").isEnabled(), false);
    await draft.fill("Compare these two screenshots.");
    await picker.setInputFiles([file, {...file, name: "remove-me.jpg"}]);
    await counts(2);
    assert.equal(await paste(["blue-shot.png", "red-shot.png", "over-limit.png"]), false);
    await counts(4);
    await page.getByText("Up to 4 images per message. Extra images weren’t attached.").waitFor();
    assert.equal(await page.locator("#attach").isDisabled(), true);
    await page.getByRole("button", {name: "Remove image 2, remove-me.jpg"}).click();
    await page.getByRole("button", {name: "Remove image 3, red-shot.png"}).click();
    await counts(2);
    assert.deepEqual(await page.locator("#image-draft strong").allTextContents(), ["diagram.jpg", "blue-shot.png"]);
    assert.equal(await draft.inputValue(), "Compare these two screenshots.");
    const expected = await page.locator("#image-draft img").evaluateAll(imgs => imgs.map(img => img.src.split(",")[1]));
    if (process.env.LANTERN_UI_SCREENSHOTS) {
      await page.screenshot({ path: path.join(process.env.LANTERN_UI_SCREENSHOTS, "image-compose-" + width + ".png") });
    }
    await page.reload();
    await counts(2);
    assert.deepEqual(await page.locator("#image-draft strong").allTextContents(), ["diagram.jpg", "blue-shot.png"]);
    await page.locator("#send").click();
    await page.getByText("Delivery unconfirmed. Retry send to check or deliver this message.").waitFor();
    await page.reload();
    await page.getByRole("button", { name: "Retry send" }).click();
    await page.waitForFunction(() => !document.querySelector("#draft").value);
    assert.equal(posts.length, 2);
    assert.deepEqual(posts[0], posts[1]);
    assert.equal(posts[0].text, "Compare these two screenshots.");
    assert.deepEqual(posts[0].images.map(i => i.data), expected);
    assert.equal(posts[0].images[0].mime_type, "image/png");
    const lastImage = payloads.get("1/images/0");
    assert.ok(lastImage.length <= 1024 * 1024);
    assert.equal(lastImage.readUInt32BE(16), 1600);
    assert.equal(lastImage.readUInt32BE(20), 640);
    await page.reload();
    const thumb = page.getByRole("button", { name: "Open attached image 2 of 2" });
    await thumb.locator("img[src^='blob:']").waitFor();
    await thumb.click();
    await page.locator("#image-dialog[open]").waitFor();
    assert.ok((await page.locator("#image-dialog").boundingBox()).width <= width);
    await page.keyboard.press("Escape");
    await choose();
    await page.locator("#send").click();
    await page.waitForFunction(() => document.querySelector("#image-draft").hidden);
    assert.equal(posts[2].text, "");
    assert.equal(posts[2].images.length, 1);
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await draft.focus();
    await page.evaluate(() => navigator.clipboard.writeText("Plain text follow-up"));
    await page.keyboard.press("Control+v");
    await page.waitForFunction(() => document.querySelector("#draft").value === "Plain text follow-up");
    await page.locator("#send").click();
    await page.waitForFunction(() => !document.querySelector("#draft").value);
    assert.equal(posts[3].text, "Plain text follow-up");
    assert.equal(posts[3].images, undefined);
    await draft.focus();
    await page.evaluate(async () => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 32;
      const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
      await navigator.clipboard.write([new ClipboardItem({"image/png": blob})]);
    });
    await page.keyboard.press("Control+v");
    await counts(1);
    assert.equal(await draft.inputValue(), "");
    await page.locator(".image-remove").click();
    if (width === 1100) {
      // Exercise a real storage boundary: four near-limit PNGs expand beyond
      // 5 MiB in their wire representation. Unicode metadata must survive too.
      await page.evaluate(async () => {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 496;
        const draw = canvas.getContext("2d"), pixels = draw.createImageData(canvas.width, canvas.height);
        for (let i = 0; i < pixels.data.length; i += 65536) crypto.getRandomValues(pixels.data.subarray(i, i + 65536));
        draw.putImageData(pixels, 0, 0);
        const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
        const files = new DataTransfer();
        for (let i = 0; i < 4; i++) files.items.add(new File([blob], "灯 🪔 " + i + ".png", {type: "image/png"}));
        const picker = document.querySelector("#image-file");
        picker.files = files.files;
        picker.dispatchEvent(new Event("change", {bubbles: true}));
      });
      await counts(4);
      const lengths = await page.locator("#image-draft img").evaluateAll(imgs => imgs.map(img => img.src.length));
      assert.ok(lengths.reduce((sum, n) => sum + n, 0) > 5 * 1024 * 1024, "exercise beyond the base64 session quota: " + lengths);
      const caption = "Compare my screenshots — 🪔.";
      await draft.fill(caption);
      await page.reload();
      await counts(4);
      assert.equal(await draft.inputValue(), caption);
      assert.equal(await page.locator("#image-draft strong").last().textContent(), "灯 🪔 3.png");
      // If quota shrinks between selecting files and sending, the browser must
      // not dispatch then restore the prior editable draft under a new ID.
      await page.evaluate(() => {
        const original = Storage.prototype.setItem;
        Storage.prototype.setItem = function (key, value) {
          if (key === "lantern.room.composer") throw new DOMException("Test quota", "QuotaExceededError");
          return original.call(this, key, value);
        };
      });
      await page.locator("#send").click();
      await page.getByText(/Nothing was sent/).waitFor();
      assert.equal(posts.length, 4);
      await page.reload();
      await counts(4);
      loseReceipt = true;
      await page.locator("#send").click();
      await page.getByText(/Delivery unconfirmed/).waitFor();
      await page.reload();
      await counts(4);
      await page.getByRole("button", {name: "Retry send"}).click();
      await page.waitForFunction(() => !document.querySelector("#draft").value);
      assert.deepEqual(posts[4], posts[5]);
      assert.equal(posts[5].text, caption);
      console.log("Four large Unicode-named screenshots and a Unicode caption survived reload and an unconfirmed delivery.");
    }
    assert.ok(imageReads >= 1);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    console.log(width + "px: multi-select, clipboard images, four-image limit, remove, caption, durable ordered retry, image-only, zoom, and native text paste passed");
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
