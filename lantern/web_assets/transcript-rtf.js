"use strict";

// Convert the room's already-rendered, allowlisted markup into native Word
// paragraphs, lists, tables and fields. Only authenticated room PNGs are embedded.
function createTranscriptRtf({timeZone} = {}) {
  const clock = new Intl.DateTimeFormat("en-US", {
    year: "numeric", month: "short", day: "numeric", hour: "numeric",
    minute: "2-digit", second: "2-digit", timeZone, timeZoneName: "short"
  });
  const width = 9360; // Printable twips on Letter with one-inch margins.
  const events = [];
  const lists = [];
  const overrides = [];

  function escapeText(value) {
    // RTF \u takes a signed UTF-16 code unit. The non-Unicode regex visits
    // both surrogate halves of characters outside the BMP.
    return value.replace(/\r\n|\r|\n|\t|[\\{}]|[^\x20-\x7e]/g, ch => {
      if (ch === "\r\n" || ch === "\r" || ch === "\n") return "\\line ";
      if (ch === "\t") return "\\tab ";
      if (ch === "\\" || ch === "{" || ch === "}") return "\\" + ch;
      const n = ch.charCodeAt(0);
      return "\\u" + (n > 32767 ? n - 65536 : n) + "?";
    });
  }

  function inlines(nodes, literal) {
    return Array.from(nodes, node => {
      if (node.nodeType === 3) {
        const text = node.nodeValue;
        return escapeText(literal ? text : text.replace(/[\t\r\n ]+/g, " "));
      }
      const tag = node.localName;
      if (tag === "br") return "\\line ";
      if (tag === "code") return "{\\f1 " + escapeText(node.textContent) + "}";
      const inner = inlines(node.childNodes, literal);
      if (tag === "a" && /^https?:\/\//i.test(node.href)) {
        // The Word field parser interprets quotes and backslashes after the
        // RTF parser. Percent-encode these in the URL, as well as delimiters.
        const url = node.href.replace(/["\\{}\x00-\x20\x7f]/g, c =>
          "%" + c.charCodeAt(0).toString(16).padStart(2, "0"));
        return '{\\field{\\*\\fldinst HYPERLINK "' + escapeText(url) +
          '"}{\\fldrslt{\\ul\\cf3 ' + inner + "}}}";
      }
      const style = {strong: "\\b", em: "\\i", s: "\\strike"}[tag];
      return style ? "{" + style + " " + inner + "}" : inner;
    }).join("");
  }

  function listId(ordered, start, left) {
    const id = lists.length + 1;
    const format = ordered
      ? "\\levelnfc0\\levelnfcn0"
      : "\\levelnfc23\\levelnfcn23";
    const pattern = ordered
      ? "{\\leveltext\\'02\\'00.;}{\\levelnumbers\\'01;}"
      : "{\\leveltext\\'01\\u8226?;}{\\levelnumbers;}";
    lists.push("{\\list\\listtemplateid" + id + "\\listsimple1{\\listlevel" + format +
      "\\leveljc0\\levelfollow0\\levelstartat" + start + pattern +
      "\\fi-360\\li" + left + "\\jclisttab\\tx" + left + "}\\listid" + id + "}");
    overrides.push("{\\listoverride\\listid" + id + "\\listoverridecount0\\ls" + id + "}");
    return id;
  }

  function writeImage(parts, image) {
    const bytes = image?.bytes;
    if (!bytes || bytes.length < 24 || bytes[0] !== 137 || bytes[1] !== 80 ||
        bytes[2] !== 78 || bytes[3] !== 71) throw Error("An attached image could not be exported.");
    const header = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const w = header.getUint32(16), h = header.getUint32(20);
    if (!w || !h) throw Error("An attached image has invalid dimensions.");
    const scale = Math.min(15, width / w, 10080 / h);
    parts.push("{\\pard\\sa90 {\\pict\\pngblip\\picw" + w + "\\pich" + h +
      "\\picwgoal" + Math.max(1, Math.round(w * scale)) +
      "\\pichgoal" + Math.max(1, Math.round(h * scale)) + "\n");
    const digits = "0123456789abcdef";
    for (let pos = 0; pos < bytes.length; pos += 16384) {
      const n = Math.min(16384, bytes.length - pos);
      const hex = new Uint8Array(2 * n);
      for (let i = 0; i < n; i++) {
        const b = bytes[pos + i];
        hex[i * 2] = digits.charCodeAt(b >> 4);
        hex[i * 2 + 1] = digits.charCodeAt(b & 15);
      }
      parts.push(hex, "\n");
    }
    parts.push("}\\par}\n");
  }

  function addEvent(event, images = []) {
    if (images.length !== (event.images?.length ?? (event.image ? 1 : 0))) {
      throw Error("Some of the message attachments are missing.");
    }
    const isLantern = event.role === "lantern";
    const author = isLantern ? "Lantern" : event.role === "operator" ? "You" : "Service";
    const date = new Date(event.at);
    const at = Number.isNaN(date.getTime()) ? String(event.at) : clock.format(date);
    const parts = ["{\\pard\\keepn\\sb200\\sa60\\plain\\f0\\fs22 {\\b " +
      author + "}{\\fs19\\cf1 \\~\\~\\u183?\\~\\~" + escapeText(at) + "}\\par}\n"];
    for (const image of images) writeImage(parts, image);
    const body = document.createElement("div");
    if (isLantern) renderLanternMarkdown(body, event.text);
    else appendPlainMessage(body, event.text);

    function paragraph(text, context, format = "") {
      let indent = "\\li" + context.left;
      let label = "";
      if (context.marker) {
        const {id, text} = context.marker;
        label = "{\\listtext " + escapeText(text) + "\\tab}";
        indent += "\\ls" + id + "\\ilvl0\\fi-360\\tx" + context.left;
        context.marker = null;
      }
      parts.push("{\\pard\\plain\\f0\\fs22\\sa85" + indent +
        (context.quote ? "\\brdrl\\brdrs\\brdrw12\\brdrcf2\\brsp65" : "") +
        format + " " + label + text + "\\par}\n");
    }

    function table(element, context) {
      for (const row of element.rows) {
        const cells = Array.from(row.cells);
        if (!cells.length) continue;
        if (cells.length > 63) { // Word's native table column limit.
          paragraph(cells.map(cell => inlines(cell.childNodes, false)).join("\\tab "), context);
          continue;
        }
        const header = row.parentNode.localName === "thead";
        const border = "\\brdrs\\brdrw8\\brdrcf2";
        const cellStyle = "\\clbrdrt" + border + "\\clbrdrl" + border +
          "\\clbrdrb" + border + "\\clbrdrr" + border;
        parts.push("\\trowd\\trgaph70\\trleft", String(context.left), header ? "\\trhdr" : "");
        for (let i = 1; i <= cells.length; i++) {
          const edge = context.left + Math.round((width - context.left) * i / cells.length);
          parts.push(cellStyle, header ? "\\clcbpat4" : "", "\\cellx" + edge);
        }
        for (const cell of cells) {
          parts.push("{\\pard\\intbl\\plain\\f0\\fs21\\sa40 ", header ? "\\b " : "",
            inlines(cell.childNodes, false), "\\cell}");
        }
        parts.push("\\row\n");
      }
    }

    function blocks(parent, context) {
      let inline = [];
      function flush() {
        if (inline.length) {
          const text = inlines(inline, !isLantern);
          if (text) paragraph(text, context);
          inline = [];
        }
      }
      for (const node of parent.childNodes) {
        const tag = node.localName;
        if (!["p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "hr", "blockquote",
              "ul", "ol", "table", "div"].includes(tag)) {
          inline.push(node);
          continue;
        }
        flush();
        if (tag === "p") {
          paragraph(inlines(node.childNodes, !isLantern), context);
        } else if (/^h[1-6]$/.test(tag)) {
          const level = Number(tag[1]) - 1;
          paragraph(inlines(node.childNodes, false), context,
            "\\keepn\\outlinelevel" + level + "\\sb100\\b\\fs" + [32,29,26,24,22,22][level]);
        } else if (tag === "pre") {
          paragraph(escapeText(node.textContent), context, "\\f1\\fs20");
        } else if (tag === "hr") {
          paragraph("", context, "\\brdrb\\brdrs\\brdrw10\\brdrcf2");
        } else {
          // A list item may start with a nested block without any preceding text.
          if (context.marker) paragraph("", context);
          if (tag === "ul" || tag === "ol") {
            const ordered = tag === "ol";
            let number = ordered ? node.start : 1;
            const left = Math.min(context.left + 420, width / 2);
            const id = listId(ordered, number, left);
            for (const item of node.children) {
              const itemContext = {...context, left, marker: {id, text: ordered ? number++ + "." : "•"}};
              blocks(item, itemContext);
              if (itemContext.marker) paragraph("", itemContext);
            }
          } else if (tag === "table") {
            table(node, context);
          } else if (tag === "blockquote") {
            blocks(node, {...context, left: Math.min(context.left + 360, width / 2), quote: true});
          } else {
            blocks(node, context);
          }
        }
      }
      flush();
    }
    blocks(body, {left: 0, quote: false, marker: null});
    // Snapshot each event now, allowing the DOM, original PNG and temporary
    // hex buffers to be reclaimed while the next page of history is fetched.
    events.push(new Blob(parts));
  }

  function finish() {
    const header = "{\\rtf1\\ansi\\ansicpg1252\\deff0\\uc1\n" +
      "{\\fonttbl{\\f0\\fswiss Calibri;}{\\f1\\fmodern Consolas;}}\n" +
      "{\\colortbl;\\red83\\green88\\blue94;\\red201\\green206\\blue210;" +
      "\\red21\\green83\\blue133;\\red233\\green238\\blue242;}\n";
    const parts = [header];
    if (lists.length) parts.push("{\\*\\listtable", new Blob(lists),
      "}{\\*\\listoverridetable", new Blob(overrides), "}\n");
    parts.push("\\paperw12240\\paperh15840\\margl1440\\margr1440\\margt1080\\margb1080\\deftab360\n",
      "{\\pard\\keepn\\sa140\\plain\\f0\\fs36\\b Lantern conversation\\par}\n");
    if (!events.length) parts.push("{\\pard\\f0\\fs22 No conversation events in this period.\\par}\n");
    return new Blob(parts.concat(events, "}"), {type: "application/rtf"});
  }

  return {addEvent, finish};
}
