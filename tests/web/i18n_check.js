// Runs the real static/js/i18n.js against a tiny fake DOM and the real Polish catalog.
// Usage: node i18n_check.js <path to web/static>
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = process.argv[2];
const read = (...p) => fs.readFileSync(path.join(STATIC, ...p), "utf8");

class Node_ {
  constructor(type) { this.nodeType = type; this.parentElement = null; }
}
class Text extends Node_ {
  constructor(data) { super(3); this.data = data; }
}
class El extends Node_ {
  constructor(tag, attrs = {}, children = []) {
    super(1);
    this.tagName = tag.toUpperCase();
    this.attrs = new Map(Object.entries(attrs));
    this.children = [];
    children.forEach((c) => this.append(typeof c === "string" ? new Text(c) : c));
  }
  append(child) { child.parentElement = this; this.children.push(child); }
  hasAttribute(n) { return this.attrs.has(n); }
  getAttribute(n) { return this.attrs.has(n) ? this.attrs.get(n) : null; }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  removeAttribute(n) { this.attrs.delete(n); }
  querySelectorAll() {
    const out = [];
    const walk = (el) => el.children.forEach((c) => { if (c.nodeType === 1) { if (c.attrs.has("data-i18n-html")) out.push(c); walk(c); } });
    walk(this);
    return out;
  }
  get lang() { return this.attrs.get("lang") || ""; }
  set innerHTML(html) { this.children = []; this.html = html; }
  get textContent() { return this.children.map((c) => (c.nodeType === 3 ? c.data : c.textContent)).join(""); }
}

function load(lang, tree) {
  const observers = [];
  const document = {
    documentElement: new El("html", { lang }),
    readyState: "complete",
    createTreeWalker(root) {
      const order = [];
      const walk = (n) => { n.children.forEach((c) => { order.push(c); if (c.nodeType === 1) walk(c); }); };
      walk(root);
      let i = 0;
      return { nextNode: () => order[i++] || null };
    },
    nodeType: 9,
    getElementById: () => null,
    addEventListener() {},
    querySelectorAll: () => tree.querySelectorAll(),
  };
  document.children = [tree];
  tree.parentElement = null;
  const window = { RD_I18N: undefined, location: { protocol: "http:" } };
  if (lang !== "en") vm.runInNewContext(read("i18n", `${lang}.js`), { window });
  const context = vm.createContext({ window, document, MutationObserver: class { constructor(cb) { observers.push(cb); } observe() {} }, setTimeout: () => 0, WeakMap, Map, Set, Object, Array, String, RegExp });
  vm.runInContext(read("js", "i18n.js"), context);
  return { window, document, observers };
}

let failed = 0;
const eq = (name, got, want) => {
  if (got !== want) { failed++; console.log("FAIL", name, "\n  got :", JSON.stringify(got), "\n  want:", JSON.stringify(want)); }
};

// ---- Polish
{
  const title = new El("title", {}, ["Devices - RustDesk API Server"]);
  const heading = new El("h1", {}, ["\n    Devices\n  "]);
  const pattern = new El("span", {}, ["Page 2 of 5 (120 total)"]);
  const userData = new El("td", {}, ["Some device called Reception"]);
  const skipped = new El("div", { "data-i18n-skip": "" }, ["Devices"]);
  const code = new El("code", {}, ["Devices"]);
  const button = new El("button", { title: "Copy", "aria-label": "Refresh" }, ["Save"]);
  const block = new El("p", { "data-i18n-html": "connect.intro" }, ["English fallback"]);
  const unknown = new El("p", {}, ["A sentence nobody translated"]);
  const activity = new El("div", {}, [new El("span", {}, ["admin"]), ' created user "bob"']);
  const root = new El("html", {}, [title, new El("body", {}, [heading, pattern, userData, skipped, code, button, block, unknown, activity])]);
  const { window } = load("pl", root);

  eq("title keeps the site name", title.textContent, "Urządzenia - RustDesk API Server");
  eq("whitespace around a text is kept", heading.textContent, "\n    Urządzenia\n  ");
  eq("pattern with values", pattern.textContent, "Strona 2 z 5 (łącznie: 120)");
  eq("user data that is not a phrase is untouched", userData.textContent, "Some device called Reception");
  eq("data-i18n-skip", skipped.textContent, "Devices");
  eq("code is skipped", code.textContent, "Devices");
  eq("attributes", `${button.getAttribute("title")}|${button.getAttribute("aria-label")}|${button.textContent}`, "Kopiuj|Odśwież|Zapisz");
  eq("html block replaced", block.html.startsWith("Wszystko, co klient RustDesk"), true);
  eq("unknown text stays", unknown.textContent, "A sentence nobody translated");
  eq("activity words", activity.children[1].data, ' utworzył(a) użytkownika „bob"');

  eq("t() with values", window.t("Delete {1} device(s)? This cannot be undone.", [3]), "Usunąć urządzenia (3)? Tej operacji nie można cofnąć.");
  eq("t() with a t-only template", window.t("{1} on {2}", ["Chrome", "Windows"]), "Chrome na Windows");
  eq("t() falls back to English", window.t("Not in the catalog {1}", ["x"]), "Not in the catalog x");
  eq("language exposed", window.rdLanguage, "pl");
}

// ---- a t()-only template is not a page-wide pattern
{
  const risky = new El("td", {}, ["Chrome on Windows"]);
  load("pl", new El("html", {}, [new El("body", {}, [risky])]));
  eq("generic templates do not rewrite page text", risky.textContent, "Chrome on Windows");
}

// ---- translations added later (the observer) and our own writes
{
  const host = new El("div", {}, []);
  const root = new El("html", {}, [new El("body", {}, [host])]);
  const { observers } = load("fr", root);
  const late = new El("span", {}, ["Never"]);
  host.append(late);
  observers[0]([{ type: "childList", addedNodes: [late] }]);
  eq("added later", late.textContent, "Jamais");
  const text = late.children[0];
  observers[0]([{ type: "characterData", target: text }]); // our own write is not translated again
  eq("no double translation", late.textContent, "Jamais");
}

// ---- English: only t(), nothing rewritten
{
  const el = new El("h1", {}, ["Devices"]);
  const { window, observers } = load("en", new El("html", {}, [new El("body", {}, [el])]));
  eq("English page unchanged", el.textContent, "Devices");
  eq("no observer for English", observers.length, 0);
  eq("t() interpolates English", window.t("Page {1} of {2}", [1, 2]), "Page 1 of 2");
}

// ---- every language loads and knows the navigation
for (const lang of ["pl", "fr", "de", "es"]) {
  const nav = new El("a", {}, ["Dashboard"]);
  load(lang, new El("html", {}, [new El("body", {}, [nav])]));
  eq(`${lang} translates the navigation`, nav.textContent !== "Dashboard", true);
}

console.log(failed ? `${failed} i18n check(s) failed` : "all i18n checks passed");
process.exit(failed ? 1 : 0);
