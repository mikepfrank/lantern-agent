// Browser regression for the standalone Word writer, using the room's real formatters.
const assert = require("node:assert/strict");
const path = require("node:path");
const {chromium} = require("playwright");

async function run() {
  const browser = await chromium.launch({executablePath: process.env.LANTERN_CHROME});
  try {
    const page = await browser.newPage();
    const requests = [];
    page.on("request", req => requests.push(req.url()));
    for (const name of ["markdown-it-15.0.3.min.js", "message-format.js", "transcript-rtf.js"]) {
      await page.addScriptTag({path: path.join(__dirname, "../lantern/web_assets", name)});
    }
    const {rtf, images, empty} = await page.evaluate(async () => {
      const options = {timeZone: "America/Chicago"};
      const writer = createTranscriptRtf(options);
      const at = "2026-11-26T19:15:00Z";
      const images = [];
      for (const [w,h] of [[40,20],[1000,500]]) {
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const file = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
        images.push({width: w, height: h, bytes: new Uint8Array(await file.arrayBuffer())});
      }
      writer.addEvent({role:"operator", at, images, text:[
        "Literal **bold** \t A {value} 😀", "\x60\x60\x60python", "    return '\\\\path'", "\x60\x60\x60"
      ].join("\n")}, images);
      writer.addEvent({role:"lantern", at, text:[
        "# A heading", "", "Both **bold and *italic***, ~~strike~~ and \x60inline\x60.", "",
        "3. Three", "   - Nested", "4. Four", "",
        "> Quoted", "", "| Label | Value |", "| --- | --- |", "| A | **Native** |", "",
        "[Link](https://example.com/docs?q=a%22b) and ![Alt](https://example.com/remote.png).",
        "", "A soft", "break  ", "A hard break."
      ].join("\n")}, []);
      writer.addEvent({role:"system", at, text:"Service {\\object\\objdata evil}.\nNext line."}, []);
      const doc = writer.finish();
      if (doc.type !== "application/rtf") throw Error("Unexpected document content type");
      try {
        writer.addEvent({role:"operator", at, text:"", images:[{}]}, []);
        throw Error("Missing image unexpectedly accepted");
      } catch (e) {
        if (!e.message.includes("attachments are missing")) throw e;
      }
      return {
        rtf: await doc.text(),
        empty: await createTranscriptRtf(options).finish().text(),
        images: images.map(img => Array.from(img.bytes))
      };
    });
    assert.deepEqual(requests, []);
    assert.match(rtf, /^\{\\rtf1/);
    assert.match(empty, /No conversation events/);
    assert.match(rtf, /Nov 26, 2026.*1:15:00 PM CST/);
    assert.ok(rtf.indexOf("{\\b You}") < rtf.indexOf("{\\b Lantern}"));
    assert.ok(rtf.indexOf("{\\b Lantern}") < rtf.indexOf("{\\b Service}"));
    assert.match(rtf, /Literal \*\*bold\*\* \\tab  A \\\{value\\\} \\u-10179\?\\u-8704\?/);
    assert.ok(rtf.includes( "return '\\\\\\\\path'"));
    assert.match(rtf, /\\outlinelevel0.*A heading/);
    assert.ok(rtf.includes("{\\b bold and {\\i italic}}"));
    assert.ok(rtf.includes("{\\strike strike}"));
    assert.ok(rtf.includes("{\\f1 inline}"));
    assert.match(rtf, /\\levelstartat3/);
    assert.match(rtf, /\\levelnfc23/);
    assert.match(rtf, /\\ls2\\ilvl0\\fi-360\\tx840/);
    assert.match(rtf, /\\brdrl.*Quoted/);
    assert.equal((rtf.match(/\\trowd/g) || []).length, 2);
    assert.equal((rtf.match(/\\cell}/g) || []).length, 4);
    assert.match(rtf, /\\trhdr/);
    assert.match(rtf, /\\fldinst HYPERLINK "https:\/\/example.com\/docs\?q=a%22b"/);
    assert.doesNotMatch(rtf, /remote\.png/);
    assert.match(rtf, /A soft break\\line A hard break/);
    assert.ok(rtf.includes("Service \\{\\\\object\\\\objdata evil\\}.\\line Next line."));
    const pictures = [...rtf.matchAll(/\\pngblip\\picw(\d+)\\pich(\d+)\\picwgoal(\d+)\\pichgoal(\d+)\n([\da-f\n]+)}/g)];
    assert.equal(pictures.length, 2);
    assert.deepEqual(pictures.map(m => m.slice(1,5).map(Number)), [[40,20,600,300],[1000,500,9360,4680]]);
    pictures.forEach((m,i) => assert.deepEqual(Buffer.from(m[5].replace(/\n/g,""),"hex"), Buffer.from(images[i])));
    assert.ok(rtf.indexOf("\\pngblip") < rtf.indexOf("Literal **bold**"));
    let depth = 0;
    for (let i = 0; i < rtf.length; i++) {
      if (rtf[i] === "\\") i++;
      else if (rtf[i] === "{") depth++;
      else if (rtf[i] === "}") depth--;
      assert.ok(depth >= 0);
    }
    assert.equal(depth, 0);
    console.log("Transcript RTF writer regression passed.");
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
