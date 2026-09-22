// Usage: node menu_check.js <path to web/static/js>
// The menu's per-viewer settings (theme.js), its icons (icons.js) and the links renderNav builds (app.js),
// run as shipped against a stub DOM.
const fs = require("fs");
const path = require("path");
const dir = process.argv[2];
const read = (name) => fs.readFileSync(path.join(dir, name), "utf8");

let failed = 0;
const eq = (name, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    failed++;
    console.log("FAIL", name, "\n  got :", JSON.stringify(got), "\n  want:", JSON.stringify(want));
  }
};
const ok = (name, condition, detail) => {
  if (!condition) { failed++; console.log("FAIL", name, detail === undefined ? "" : detail); }
};

function browser(storage, systemDark) {
  const attributes = {};
  const window = {
    localStorage: storage,
    matchMedia: () => ({ matches: !!systemDark, addEventListener() {} }),
  };
  const document = {
    documentElement: { setAttribute: (k, v) => { attributes[k] = v; } },
  };
  return { window, document, attributes };
}
function runTheme(storage, systemDark) {
  const env = browser(storage, systemDark);
  new Function("window", "document", read("theme.js"))(env.window, env.document);
  return env;
}
function memory(initial) {
  const data = Object.assign({}, initial);
  return { data, getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); } };
}

// ---- defaults, saved choices, and values that are not choices
let env = runTheme(memory({}));
eq("default menu", [env.attributes["data-layout"], env.attributes["data-nav"]], ["top", "full"]);
env = runTheme(memory({ rd_layout: "left", rd_nav: "compact" }));
eq("saved menu", [env.attributes["data-layout"], env.attributes["data-nav"]], ["left", "compact"]);
env = runTheme(memory({ rd_layout: "bottom", rd_nav: "huge" }));
eq("unknown saved values fall back", [env.attributes["data-layout"], env.attributes["data-nav"]], ["top", "full"]);

// ---- choosing, and that it is remembered
const storage = memory({});
env = runTheme(storage);
env.window.appTheme.setLayout("left");
env.window.appTheme.setNav("compact");
eq("chosen menu applied", [env.attributes["data-layout"], env.attributes["data-nav"]], ["left", "compact"]);
eq("chosen menu remembered", [storage.data.rd_layout, storage.data.rd_nav], ["left", "compact"]);
eq("get reports it", [env.window.appTheme.get().layout, env.window.appTheme.get().nav], ["left", "compact"]);
env.window.appTheme.setLayout("diagonal");
env.window.appTheme.setNav("");
eq("unknown choices ignored", [env.attributes["data-layout"], env.attributes["data-nav"], storage.data.rd_layout], ["left", "compact", "left"]);
eq("layouts offered", env.window.appTheme.LAYOUTS, ["top", "left"]);

// ---- storage that throws (private mode, blocked): still works, just does not persist
const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
env = runTheme(blocked);
eq("blocked storage: defaults", env.attributes["data-layout"], "top");
env.window.appTheme.setLayout("left");
eq("blocked storage: choice still applies", env.attributes["data-layout"], "left");

// ---- icons
const iconEnv = { window: {} };
new Function("window", read("icons.js"))(iconEnv.window);
const NAV_KEYS = ["dashboard", "devices", "groups", "tags", "address-book", "logs", "strategies", "users", "roles", "connect", "settings", "security", "layout-left", "layout-top", "collapse", "expand", "appearance", "logout", "online", "offline", "user"];
for (const key of NAV_KEYS) {
  const svg = iconEnv.window.rdIcon(key);
  ok("icon " + key, /^<svg [^>]*aria-hidden="true"[^>]*><path d="M[^"]+"\/><\/svg>$/.test(svg), svg.slice(0, 80));
}
eq("unknown icon", iconEnv.window.rdIcon("nope"), "");
ok("icon size", iconEnv.window.rdIcon("devices", 16).includes('width="16"'));

// ---- the links renderNav builds: an icon and a label each, the current page marked
function navFor(user, active) {
  const holder = { innerHTML: "" };
  const els = { "app-nav": holder };
  const document = { getElementById: (id) => els[id] || null, cookie: "", addEventListener() {} };
  const window = { location: { protocol: "https:", host: "x", pathname: "/", search: "" }, rdIcon: iconEnv.window.rdIcon };
  const fn = new Function("document", "window", "fetch", read("app.js") + "\nreturn { renderNav };");
  fn(document, window, () => {}).renderNav(active, user);
  return holder.innerHTML;
}
const html = navFor({ username: "u", is_admin: true, id: 1 }, "devices");
const links = html.match(/<a [^>]*>.*?<\/a>/g) || [];
eq("admin sees every item", links.length, 12);
ok("each link has an icon and a label", links.every((l) => l.includes("<svg") && l.includes('class="rd-nav-label"')));
eq("one current page", (html.match(/aria-current="page"/g) || []).length, 1);
ok("the current page is Devices", /href="\/devices"[^>]*aria-current="page"/.test(html));
eq("a plain user sees fewer", (navFor({ username: "u", is_admin: false, id: 2 }, "logs").match(/<a /g) || []).length, 8);
ok("every link has a tooltip for the icons-only menu", links.every((l) => /title="[^"]+"/.test(l)));

// ---- the account controls: an avatar with the first letter, the name, a badge for an administrator
function accountFor(user) {
  const label = { innerHTML: "", href: "", title: "" };
  const logout = { innerHTML: "", addEventListener() {} };
  const els = { "app-nav": { innerHTML: "" }, "current-user-label": label, "logout-btn": logout };
  const document = { getElementById: (id) => els[id] || null, cookie: "", addEventListener() {} };
  const window = { location: { protocol: "https:", host: "x", pathname: "/", search: "" }, rdIcon: iconEnv.window.rdIcon };
  const fn = new Function("document", "window", "fetch", read("app.js") + "\nreturn { renderNav };");
  fn(document, window, () => {}).renderNav("dashboard", user);
  return { label, logout };
}
let account = accountFor({ username: "alice", is_admin: true, id: 3 });
ok("avatar shows the first letter", account.label.innerHTML.includes('class="rd-avatar" aria-hidden="true">A</span>'), account.label.innerHTML);
ok("the name is shown", account.label.innerHTML.includes('<span class="rd-user-name">alice</span>'));
ok("an administrator gets a badge", account.label.innerHTML.includes('class="rd-role">admin</span>'));
eq("an administrator's chip opens their record", account.label.href, "/users?id=3");
account = accountFor({ username: "bob", is_admin: false, id: 4 });
ok("a plain user gets no badge", !account.label.innerHTML.includes("rd-role"));
account = accountFor({ username: "Łukasz", is_admin: false, id: 5 });
ok("the initial is a whole character", account.label.innerHTML.includes(">Ł</span>"), account.label.innerHTML);
account = accountFor({ username: "<img src=x onerror=alert(1)>", is_admin: false, id: 6 });
ok("a hostile name is escaped", !account.label.innerHTML.includes("<img") && account.label.innerHTML.includes("&lt;img"), account.label.innerHTML);
ok("Log out has an icon and a label", account.logout.innerHTML.includes("<svg") && account.logout.innerHTML.includes("<span>Log out</span>"));

if (failed) { console.log(failed + " check(s) failed"); process.exit(1); }
console.log("menu checks passed");
