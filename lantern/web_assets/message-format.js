"use strict";

// Parse a local copy of the transcript. Only explicitly supported markup can
// create elements or attributes. In particular, rendering never fetches images.
const transcriptMarkdown = typeof markdownit === "function"
  ? new markdownit({ html: false, linkify: false, maxNesting: 20 }) : null;
const transcriptTags = new Set([
  "p", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "ul", "li", "blockquote",
  "strong", "em", "s", "table", "thead", "tbody", "tr", "th", "td"
]);

function transcriptLink(href) {
  if (href && /^https?:\/\//i.test(href)) {
    try {
      const url = new URL(href);
      const link = document.createElement("a");
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      return link;
    } catch {
      // Keep the label of an invalid destination readable.
    }
  }
  return document.createElement("span");
}

function appendTranscriptTokens(parent, tokens) {
  const parents = [parent];
  for (const token of tokens) {
    if (token.hidden) continue; // Tight list items have implicit paragraphs.
    const current = parents[parents.length - 1];
    if (token.nesting === -1) {
      parents.pop();
    } else if (token.nesting === 1) {
      const node = token.type === "link_open" ? transcriptLink(token.attrGet("href"))
        : document.createElement(transcriptTags.has(token.tag) ? token.tag : "span");
      if (token.tag === "ol" && token.attrGet("start") !== null) {
        node.start = Number(token.attrGet("start"));
      }
      if (token.tag === "table") {
        const scroll = document.createElement("div");
        scroll.className = "table-scroll";
        scroll.append(node);
        current.append(scroll);
      } else {
        current.append(node);
      }
      parents.push(node);
    } else if (token.type === "inline") {
      appendTranscriptTokens(current, token.children);
    } else if (token.type === "code_inline" || token.type === "fence" || token.type === "code_block") {
      const code = document.createElement("code");
      code.textContent = token.content;
      if (token.type === "code_inline") {
        current.append(code);
      } else {
        const pre = document.createElement("pre");
        pre.append(code);
        current.append(pre);
      }
    } else if (token.type === "hardbreak" || token.type === "hr") {
      current.append(document.createElement(token.type === "hr" ? "hr" : "br"));
    } else {
      // Soft breaks in prose follow normal paragraph wrapping. Images show
      // their alternative text. Raw HTML remains literal text.
      current.append(token.type === "softbreak" ? "\n" : token.content);
    }
  }
}

function renderLanternMarkdown(body, text) {
  if (transcriptMarkdown && typeof text === "string" && text.length <= 128 * 1024) {
    try {
      const formatted = document.createDocumentFragment();
      appendTranscriptTokens(formatted, transcriptMarkdown.parse(text, {}));
      body.append(formatted);
      return;
    } catch {
      // A formatting failure in one historical reply must not block later events.
    }
  }
  const pre = document.createElement("pre");
  pre.textContent = text;
  body.append(pre);
}
