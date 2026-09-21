// Translation of the WebUI (Polish, French, German, Spanish; English is the source).
//
// The pages are written in English. For another language the server includes
// /static/i18n/<lang>.js (window.RD_I18N: English text -> translation) before this
// file, and this file translates what is on the page, and whatever is added to it later:
//
//   * a text node, or a title / placeholder / aria-label / alt attribute, whose whole text
//     (whitespace collapsed) is a key of the catalog;
//   * a key with {1}, {2}... in it is a pattern: "Page {1} of {2}" translates "Page 3 of 9";
//   * an element with data-i18n-html="id" has its markup replaced by the catalog entry
//     "@id" (used for paragraphs with links or <code> inside, which cannot be translated
//     piece by piece);
//   * t("English text", [values]) for text that is not put on the page as such (confirm()).
//
// Anything the catalog does not know stays English. Text a person or a RustDesk client
// supplied (names, notes, paths) is translated only if it is *exactly* a UI phrase, which
// is why elements with data-i18n-skip are left alone.
(function () {
  "use strict";

  const catalog = (typeof window !== "undefined" && window.RD_I18N) || null;
  const root = typeof document !== "undefined" ? document.documentElement : null;
  const lang = (root && root.lang) || "en";

  const exact = new Map();
  const templates = new Map(); // keys with {1}... for t()
  const patterns = []; // the same, for text on the page
  const htmlBlocks = new Map();
  if (catalog) {
    for (const [rawKey, value] of Object.entries(catalog)) {
      // A leading "!" keeps a key out of the page-wide patterns: it is only for t(), because
      // it is too generic ("{1} on {2}") to be safe against user-supplied text.
      const tOnly = rawKey.startsWith("!");
      const key = tOnly ? rawKey.slice(1) : rawKey;
      if (key.startsWith("@")) {
        htmlBlocks.set(key.slice(1), value);
      } else if (/\{\d+\}/.test(key)) {
        templates.set(key, value);
        if (tOnly) continue;
        const source = key
          .split(/(\{\d+\})/)
          .map((part) => (/^\{\d+\}$/.test(part) ? "(.+?)" : part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))
          .join("");
        patterns.push({ re: new RegExp(`^${source}$`), value });
      } else {
        exact.set(key, value);
      }
    }
  }

  function fill(template, values) {
    if (!values) return template;
    return template.replace(/\{(\d+)\}/g, (whole, n) => {
      const v = Array.isArray(values) ? values[Number(n) - 1] : values[n];
      return v === undefined ? whole : String(v);
    });
  }

  const misses = new Set();
  // The translation of a whole (whitespace-collapsed) text, or null.
  function lookup(text) {
    if (!catalog) return null;
    const hit = exact.get(text);
    if (hit !== undefined) return hit;
    if (misses.has(text)) return null;
    for (const { re, value } of patterns) {
      const m = re.exec(text);
      if (m) return fill(value, m.slice(1));
    }
    if (misses.size > 5000) misses.clear();
    misses.add(text);
    return null;
  }

  // For text that is not put on the page directly (a confirm() question, a message
  // assembled from parts). Falls back to the English text.
  function t(key, values) {
    const collapsed = String(key).replace(/\s+/g, " ").trim();
    const hit = catalog ? (exact.get(collapsed) ?? templates.get(collapsed)) : undefined;
    return fill(hit !== undefined ? hit : key, values);
  }
  window.t = t;
  window.rdLanguage = lang;

  if (!catalog || !root || typeof document.createTreeWalker !== "function") return;

  // ---------------------------------------------------------------- translating
  const SKIP = new Set(["SCRIPT", "STYLE", "TEXTAREA", "CODE", "PRE"]);
  const ATTRS = ["title", "placeholder", "aria-label", "alt"];
  const written = new WeakMap(); // node -> the text we wrote, so our own change is not read as new text

  function skipped(node) {
    for (let el = node.nodeType === 1 ? node : node.parentElement; el; el = el.parentElement) {
      if (SKIP.has(el.tagName)) return true;
      if (el.hasAttribute && (el.hasAttribute("data-i18n-skip") || el.hasAttribute("data-i18n-html"))) return true;
    }
    return false;
  }

  function translateText(node) {
    const raw = node.data;
    if (written.get(node) === raw) return;
    const collapsed = raw.replace(/\s+/g, " ").trim();
    if (!collapsed || skipped(node)) return;
    let out;
    if (node.parentElement && node.parentElement.tagName === "TITLE") {
      // "Devices - RustDesk API Server": only the page name is translated.
      const cut = collapsed.lastIndexOf(" - ");
      const head = cut < 0 ? collapsed : collapsed.slice(0, cut);
      const done = lookup(head);
      out = done === null ? null : cut < 0 ? done : `${done}${collapsed.slice(cut)}`;
    } else {
      out = lookup(collapsed);
    }
    if (out === null) return;
    const lead = /^\s*/.exec(raw)[0];
    const trail = /\s*$/.exec(raw)[0];
    const next = lead + out + trail;
    written.set(node, next);
    node.data = next;
  }

  function translateAttrs(el) {
    if (!el.getAttribute || skipped(el)) return;
    for (const name of ATTRS) {
      const value = el.getAttribute(name);
      if (!value) continue;
      const out = lookup(value.replace(/\s+/g, " ").trim());
      if (out !== null && out !== value) el.setAttribute(name, out);
    }
  }

  function translateBlocks(rootNode) {
    const found = rootNode.nodeType === 1 && rootNode.hasAttribute("data-i18n-html") ? [rootNode] : [];
    if (rootNode.querySelectorAll) found.push(...rootNode.querySelectorAll("[data-i18n-html]"));
    for (const el of found) {
      const html = htmlBlocks.get(el.getAttribute("data-i18n-html"));
      // The catalog is our own shipped file (never user data), so its markup is trusted.
      if (html !== undefined && el.getAttribute("data-i18n-done") !== "1") {
        el.setAttribute("data-i18n-done", "1");
        el.innerHTML = html;
      }
    }
  }

  function translateTree(node) {
    if (node.nodeType === 3) return translateText(node);
    if (node.nodeType !== 1 && node.nodeType !== 9 && node.nodeType !== 11) return;
    if (node.nodeType === 1 && SKIP.has(node.tagName)) return;
    translateBlocks(node);
    if (node.nodeType === 1) translateAttrs(node);
    const walker = document.createTreeWalker(node, 1 | 4); // elements and text
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (n.nodeType === 3) translateText(n);
      else translateAttrs(n);
    }
  }

  function start() {
    translateTree(document);
    root.removeAttribute("data-i18n-pending");
    if (typeof MutationObserver === "function") {
      new MutationObserver((records) => {
        for (const r of records) {
          if (r.type === "childList") r.addedNodes.forEach(translateTree);
          else if (r.type === "characterData") translateText(r.target);
          else if (r.type === "attributes") translateAttrs(r.target);
        }
      }).observe(document, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: ATTRS,
      });
    }
  }

  // The page is hidden by a stylesheet rule until the first pass (see base.html), with a
  // timeout so a script error can never leave it blank.
  root.setAttribute("data-i18n-pending", "");
  setTimeout(() => root.removeAttribute("data-i18n-pending"), 1500);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();

// The language picker: in the Appearance menu (app.js), and in the corner of the pages that have no
// header (sign in, register, first run). A cookie (not the server's database)
// remembers the choice per browser, like the theme.
(function () {
  "use strict";
  if (typeof document === "undefined" || typeof document.getElementById !== "function") return;
  const LANGUAGES = { en: "English", pl: "Polski", fr: "Français", de: "Deutsch", es: "Español" };
  // Used by the Appearance menu (app.js) and by the corner picker below.
  window.rdLanguages = LANGUAGES;
  window.rdSetLanguage = (code) => {
    if (!LANGUAGES[code]) return;
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `rd_lang=${encodeURIComponent(code)}; path=/; max-age=31536000; SameSite=Lax${secure}`;
    window.location.reload();
  };
  function attach() {
    const select = document.getElementById("lang-select");
    if (!select || select.dataset.ready) return;
    select.dataset.ready = "1";
    select.innerHTML = Object.entries(LANGUAGES)
      .map(([code, name]) => `<option value="${code}" lang="${code}">${name}</option>`)
      .join("");
    select.value = window.rdLanguage || "en";
    select.addEventListener("change", () => window.rdSetLanguage(select.value));
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", attach);
  else attach();
})();
