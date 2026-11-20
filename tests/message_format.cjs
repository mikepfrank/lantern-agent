// Run the shipped tokenizer and DOM renderer without network or browser dependencies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assets = path.join(__dirname, "../lantern/web_assets");
const MarkdownIt = require(path.join(assets, "markdown-it-15.0.3.min.js"));
const renderer = fs.readFileSync(path.join(assets, "message-format.js"), "utf8");

class Node {
  constructor(tag) { this.tag = tag; this.children = []; }
  append(...items) {
    for (const item of items) {
      if (item.tag === "#fragment") this.children.push(...item.children);
      else this.children.push(item);
    }
  }
  set textContent(value) { this.children = [value]; }
  get textContent() {
    return this.children.map(item => typeof item === "string" ? item : item.textContent).join("");
  }
  find(tag) {
    return [ ...(this.tag === tag ? [this] : []),
      ...this.children.filter(item => item instanceof Node).flatMap(item => item.find(tag)) ];
  }
}

const document = { createElement: tag => new Node(tag), createDocumentFragment: () => new Node("#fragment") };
function render(text, parser = MarkdownIt) {
  const scope = { document, URL, markdownit: parser };
  vm.runInNewContext(renderer, scope);
  const body = new Node("div");
  scope.renderLanternMarkdown(body, text);
  return body;
}

const example = render([
  "", "", "### Notes", "", "", "**Bold with _emphasis_** and `literal **code**`.",
  "", "", "", "3. Third", "4. Fourth", "", "> An observation", "", "~~~python",
  "    print('*literal*')", "", "", "    # keep these blank lines", "~~~", "", ""
].join("\n"));
assert.deepEqual(example.children.map(node => node.tag), ["h3", "p", "ol", "blockquote", "pre"]);
assert.equal(example.find("strong")[0].find("em")[0].textContent, "emphasis");
assert.equal(example.find("ol")[0].start, 3);
assert.equal(example.find("li").length, 2);
assert.equal(example.find("code")[0].textContent, "literal **code**");
assert.equal(example.find("pre")[0].textContent, "    print('*literal*')\n\n\n    # keep these blank lines\n");

const links = render('<script>alert(1)</script> [safe](https://example.com/a) [relative](/api/room) ' +
  '[local](file:///tmp/test) [active](javascript:alert(1)) ![an image](https://example.com/image.png)');
assert.equal(links.find("script").length + links.find("img").length, 0);
assert.ok(links.textContent.includes("<script>alert(1)</script>"));
assert.ok(links.textContent.includes("an image"));
assert.deepEqual(links.find("a").map(link => [link.href, link.rel]),
                 [["https://example.com/a", "noopener noreferrer"]]);

const literal = "x".repeat(128 * 1024 + 1);
assert.equal(render(literal).find("pre")[0].textContent, literal);
assert.equal(render("**Missing library**", null).textContent, "**Missing library**");
class BrokenParser { parse() { throw new Error("formatting failure"); } }
assert.equal(render("unformatted", BrokenParser).textContent, "unformatted");
