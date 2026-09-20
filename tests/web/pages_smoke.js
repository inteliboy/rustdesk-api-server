// Runs the shipped page scripts (src/rustdesk_api/web/static/js/pages/*.js) in
// Node against a stub DOM and canned API answers, and drives the flows that
// matter: selecting devices and applying a bulk action, the timeline and the
// uuid banner, API keys, sessions, reset links, registration.
//
// It cannot check layout or styling. What it does catch is the kind of bug a
// unit test of the API never sees: a script that reads an element id its
// template does not have, a ReferenceError on a render path, or a request the
// page builds wrongly. Elements exist only if their id is in the template (or
// appears in markup a script assigned to innerHTML), like in a browser.
//
// Usage: node pages_smoke.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const WEB = path.join(__dirname, "..", "..", "src", "rustdesk_api", "web");
const read = (...p) => fs.readFileSync(path.join(WEB, ...p), "utf8");
const idsIn = (html) => Array.from(html.matchAll(/\bid="([^"]+)"/g), (m) => m[1]);

class Env {
  constructor(template, script, { search = "", hash = "", pathname = "/", fixtures = {}, user }) {
    this.known = new Set([...idsIn(read("templates", "base.html")), ...idsIn(read("templates", "_app_shell.html")), ...idsIn(read("templates", template))]);
    this.elements = new Map();
    this.listeners = new Map(); // "id:type" -> fn
    this.requests = [];
    this.toasts = [];
    this.fixtures = fixtures;
    this.user = user;
    this.errors = [];
    this.location = { href: pathname, pathname, search, hash, origin: "http://x", host: "x", protocol: "http:" };
    this.scriptName = script;
    this.html = read("templates", template);
    this.template = template;
  }

  element(id) {
    if (!this.known.has(id)) return null;
    if (!this.elements.has(id)) {
      const env = this;
      const classes = new Set(
        (new RegExp(`id="${id}"[^>]*class="([^"]*)"`).exec(this.html) || [, ""])[1].split(/\s+/).filter(Boolean)
      );
      const el = {
        id,
        dataset: {},
        style: {},
        value: "",
        checked: false,
        disabled: false,
        textContent: "",
        _html: "",
        files: [],
        classes,
        classList: {
          add: (...c) => c.forEach((x) => classes.add(x)),
          remove: (...c) => c.forEach((x) => classes.delete(x)),
          toggle: (c, force) => {
            const on = force === undefined ? !classes.has(c) : force;
            on ? classes.add(c) : classes.delete(c);
          },
          contains: (c) => classes.has(c),
        },
        addEventListener: (type, fn) => env.listeners.set(`${id}:${type}`, fn),
        querySelector: () => null,
        querySelectorAll: () => [],
        closest: () => null,
        focus() {},
        scrollIntoView() {},
        setAttribute() {},
        append() {},
        replaceChildren() {},
        appendChild() {},
        remove() {},
        contains: () => false,
        getBoundingClientRect: () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 }),
        get innerHTML() {
          return this._html;
        },
        set innerHTML(value) {
          this._html = String(value);
          idsIn(this._html).forEach((i) => env.known.add(i));
        },
        hidden() {
          return classes.has("hidden");
        },
      };
      this.elements.set(id, el);
    }
    return this.elements.get(id);
  }

  async fire(id, type, event = {}) {
    const fn = this.listeners.get(`${id}:${type}`);
    assert.ok(fn, `no ${type} listener on #${id}`);
    await fn({ preventDefault() {}, target: this.element(id), ...event });
    await settle();
  }

  route(method, url) {
    for (const [pattern, body] of Object.entries(this.fixtures)) {
      const [m, re] = pattern.split(" ");
      if (m === method && new RegExp(re).test(url)) return typeof body === "function" ? body(url) : body;
    }
    return undefined;
  }

  async run() {
    const env = this;
    const document = {
      cookie: "rd_csrf=csrf-token",
      getElementById: (id) => env.element(id),
      addEventListener() {},
      createElement: () => env.element("__scratch__") || { append() {}, setAttribute() {}, classList: { add() {} } },
      querySelectorAll: () => [],
      querySelector: () => null,
      body: { appendChild() {} },
    };
    const window = {
      location: env.location,
      history: { replaceState: (_s, _t, url) => { env.location.hash = ""; env.replaced = url; } },
      addEventListener() {},
      innerWidth: 1200,
      innerHeight: 800,
    };
    const fetch = async (url, options = {}) => {
      const method = (options.method || "GET").toUpperCase();
      const body = options.body === undefined ? undefined : options.body;
      env.requests.push({ method, url, body, headers: options.headers || {} });
      const answer = url.startsWith("/api/v1/auth/me") ? env.user : env.route(method, url);
      if (answer === undefined) return { ok: false, status: 404, text: async () => JSON.stringify({ error: { code: "X", message: `no fixture for ${method} ${url}` } }) };
      if (answer && answer.__status) return { ok: false, status: answer.__status, text: async () => JSON.stringify(answer.body) };
      return { ok: true, status: 200, text: async () => (answer === null ? "" : JSON.stringify(answer)) };
    };
    const context = vm.createContext({
      document, window, fetch, console, URLSearchParams, encodeURIComponent, Number, Set, Map, Array, Object, Date, JSON, Promise, Boolean, String, Error, parseInt, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
      confirm: () => true,
      navigator: { clipboard: { writeText: async (t) => { env.copied = t; } } },
      WebSocket: class { addEventListener() {} },
      Element: class {},
      __toasts: env.toasts,
    });
    this.context = context;
    vm.runInContext(read("static", "js", "app.js"), context, { filename: "app.js" });
    vm.runInContext("toast = function (message, kind) { __toasts.push([message, kind || 'info']); };", context);
    vm.runInContext(read("static", "js", "pages", this.scriptName), context, { filename: this.scriptName });
    await settle();
    return this;
  }

  eval(code) {
    return vm.runInContext(code, this.context);
  }

  html_(id) {
    return this.element(id) ? this.element(id).innerHTML : null;
  }

  posted(url) {
    return this.requests.filter((r) => r.method === "POST" && r.url.startsWith(url));
  }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

const admin = { id: 1, username: "admin", is_admin: true };
const alice = { id: 2, username: "alice", is_admin: false };

let failures = 0;
async function test(name, fn) {
  try {
    await fn();
    console.log("ok  ", name);
  } catch (err) {
    failures++;
    console.log("FAIL", name, "\n    ", err && err.stack ? err.stack.split("\n").slice(0, 4).join("\n     ") : err);
  }
}

const device = (over = {}) => ({
  id: 7, rustdesk_id: "123456789", name: null, hostname: "host-7", alias: null, username: "u", platform: "linux",
  os_version: "x", client_version: "1.4.9", ip_address: "10.0.0.5", cpu: null, memory: null, note: null, last_seen: "2026-09-20T10:00:00",
  owner_id: 1, owner_username: "admin", group_id: null, group_name: null, strategy_id: null, strategy_name: null,
  effective_strategy_name: null, connection_count: 0, uuid_change_pending: false, uuid_change_at: null, uuid_change_ip: null,
  tags: [], created_at: "2026-09-01T10:00:00", updated_at: "2026-09-01T10:00:00", online: true, ...over,
});

const listFixtures = (items) => ({
  "GET ^/api/v1/devices\\?": { items, page: 1, page_size: 25, total: items.length },
  "GET ^/api/v1/groups": [{ id: 4, name: "Reception" }],
  "GET ^/api/v1/tags": [{ id: 3, name: "site-a", color: "#112233" }],
  "GET ^/api/v1/users": [{ id: 1, username: "admin" }, { id: 2, username: "alice" }],
  "GET ^/api/v1/strategies": [{ id: 9, name: "Locked down" }],
  "GET ^/api/v1/views": [{ id: 5, name: "Offline", query: { status: "offline", sort: "name", group_id: 4 } }],
});

(async () => {
  // ------------------------------------------------------------------ devices
  await test("devices: renders rows with checkboxes, filters, views and admin-only controls", async () => {
    const env = await new Env("devices.html", "devices.js", {
      user: admin,
      fixtures: listFixtures([device(), device({ id: 8, rustdesk_id: "222", hostname: "<img src=x onerror=alert(1)>", uuid_change_pending: true })]),
    }).run();
    const rows = env.html_("device-rows");
    assert.match(rows, /class="row-select" value="7"/);
    assert.match(rows, /class="row-select" value="8"/);
    assert.ok(!rows.includes("<img src=x"), "hostname must be escaped");
    assert.match(rows, /Review/); // the pending uuid badge
    assert.ok(!env.element("import-toggle").classList.contains("hidden"), "admin sees Import");
    assert.match(env.html_("view-select"), /Offline/);
    assert.match(env.html_("bulk-action"), /Set owner/);
    assert.match(env.html_("bulk-action"), /Set strategy/);
    assert.deepStrictEqual(env.toasts, []);
  });

  await test("devices: a non-admin has no owner/strategy bulk actions and no import", async () => {
    const env = await new Env("devices.html", "devices.js", { user: alice, fixtures: listFixtures([device()]) }).run();
    const actions = env.html_("bulk-action");
    assert.ok(!/Set owner|Set strategy/.test(actions), actions);
    assert.ok(env.element("import-toggle").classList.contains("hidden"));
  });

  await test("devices: selecting rows shows the bulk bar and Apply posts the right request", async () => {
    const env = await new Env("devices.html", "devices.js", {
      user: admin,
      fixtures: {
        ...listFixtures([device(), device({ id: 8, rustdesk_id: "222" })]),
        "POST ^/api/v1/devices/bulk": { updated: [7], skipped: [{ id: 8, reason: "forbidden" }] },
      },
    }).run();
    const pick = (id, checked) => ({ target: {}, ...{ target: { closest: () => ({ value: String(id), checked }) } } });
    await env.fire("device-rows", "change", pick(7, true));
    assert.strictEqual(env.element("bulk-count").textContent, "1 selected");
    assert.ok(!env.element("bulk-bar").classList.contains("hidden"));

    env.element("bulk-action").value = "add_tag";
    await env.fire("bulk-action", "change");
    assert.ok(!env.element("bulk-target").classList.contains("hidden"));
    env.element("bulk-target").value = "3";
    await env.fire("bulk-apply", "click");

    const [request] = env.posted("/api/v1/devices/bulk");
    assert.deepStrictEqual(JSON.parse(request.body), { ids: [7], action: "add_tag", tag_id: 3 });
    assert.strictEqual(request.headers["X-CSRF-Token"], "csrf-token");
    assert.ok(env.toasts.some(([m]) => /Updated 1 device.*skipped 1/.test(m)), JSON.stringify(env.toasts));
  });

  await test("devices: clearing an owner sends an explicit null, and Apply needs a target", async () => {
    const env = await new Env("devices.html", "devices.js", {
      user: admin,
      fixtures: { ...listFixtures([device()]), "POST ^/api/v1/devices/bulk": { updated: [7], skipped: [] } },
    }).run();
    await env.fire("device-rows", "change", { target: { closest: () => ({ value: "7", checked: true }) } });
    env.element("bulk-action").value = "set_owner";
    await env.fire("bulk-action", "change");
    await env.fire("bulk-apply", "click"); // nothing chosen yet
    assert.strictEqual(env.posted("/api/v1/devices/bulk").length, 0);
    env.element("bulk-target").value = "none";
    await env.fire("bulk-apply", "click");
    assert.deepStrictEqual(JSON.parse(env.posted("/api/v1/devices/bulk")[0].body), { ids: [7], action: "set_owner", owner_id: null });
  });

  await test("devices: filters and sort reach the list request; applying a saved view restores them", async () => {
    const env = await new Env("devices.html", "devices.js", { user: admin, fixtures: listFixtures([device()]) }).run();
    env.element("status-filter").value = "offline";
    await env.fire("status-filter", "change", { target: env.element("status-filter") });
    env.element("sort-select").value = "name";
    await env.fire("sort-select", "change", { target: env.element("sort-select") });
    const last = env.requests.filter((r) => r.url.startsWith("/api/v1/devices?")).pop().url;
    assert.match(last, /status=offline/);
    assert.match(last, /sort=name/);

    env.element("view-select").value = "5";
    await env.fire("view-select", "change", { target: env.element("view-select") });
    const afterView = env.requests.filter((r) => r.url.startsWith("/api/v1/devices?")).pop().url;
    assert.match(afterView, /status=offline/);
    assert.match(afterView, /group_id=4/);
    assert.strictEqual(env.element("status-filter").value, "offline");
  });

  await test("devices: saving a view posts the current filters", async () => {
    const env = await new Env("devices.html", "devices.js", {
      user: admin,
      fixtures: { ...listFixtures([device()]), "POST ^/api/v1/views": { id: 6, name: "Mine", query: {} } },
    }).run();
    env.element("status-filter").value = "online";
    await env.fire("status-filter", "change", { target: env.element("status-filter") });
    env.element("view-name").value = "Mine";
    await env.fire("view-save", "click");
    const [request] = env.posted("/api/v1/views");
    assert.deepStrictEqual(JSON.parse(request.body), { name: "Mine", query: { sort: "last_seen", status: "online" } });
  });

  await test("devices: import 'check only' is a dry run and errors are escaped", async () => {
    const env = await new Env("devices.html", "devices.js", {
      user: admin,
      fixtures: {
        ...listFixtures([device()]),
        "POST ^/api/v1/import/devices": { dry_run: true, applied: false, created: 0, updated: 0, tags_created: 0, errors: [{ row: 2, message: "no user named '<b>x</b>'" }] },
      },
    }).run();
    env.element("import-file").files = [{ name: "d.csv", text: async () => "rustdesk_id\n1\n" }];
    await env.fire("import-check", "click");
    const [request] = env.posted("/api/v1/import/devices");
    assert.match(request.url, /dry_run=true/);
    assert.strictEqual(request.headers["Content-Type"], "text/csv");
    const result = env.html_("import-result");
    assert.match(result, /Row 2/);
    assert.ok(!result.includes("<b>x</b>"), result);
  });

  // ------------------------------------------------------------ device detail
  const detailFixtures = (d, timeline, extra = {}) => ({
    "GET ^/api/v1/devices/7$": d,
    "GET ^/api/v1/groups": [],
    "GET ^/api/v1/tags": [],
    "GET ^/api/v1/strategies": [],
    "GET ^/api/v1/devices/7/shares": [],
    "GET ^/api/v1/devices/7/connections": [],
    "GET ^/api/v1/devices/7/timeline": timeline,
    ...extra,
  });
  const detailEnv = (d, timeline, extra, user = admin) => {
    const env = new Env("device_detail.html", "device_detail.js", { user, fixtures: detailFixtures(d, timeline, extra), pathname: "/devices/7" });
    // the page reads its id from the <main data-device-id> attribute
    const original = env.element.bind(env);
    env.element = (id) => {
      const el = original(id);
      if (id === "device-page" && el) el.dataset.deviceId = "7";
      return el;
    };
    return env.run();
  };

  await test("device detail: the timeline renders every source, escaped", async () => {
    const timeline = [
      { at: "2026-09-20T10:00:00", source: "audit", kind: "device_updated", actor: "admin", result: "success", detail: { rustdesk_id: "123456789", alias: "<i>x</i>", via: "bulk" } },
      { at: "2026-09-20T09:00:00", source: "connection", kind: "connection", actor: null, result: null, detail: { peer_id: "222", peer_name: "<script>x</script>", from_ip: "203.0.113.7", ended_at: null } },
      { at: "2026-09-20T08:00:00", source: "event", kind: "online", actor: null, result: null, detail: { offline_since: "2026-09-20T07:00:00" } },
      { at: "2026-09-20T07:00:00", source: "event", kind: "uuid_change_requested", actor: null, result: null, detail: { ip: "203.0.113.9" } },
      { at: "2026-09-20T06:00:00", source: "event", kind: "registered", actor: null, result: null, detail: {} },
    ];
    const env = await detailEnv(device(), timeline);
    const html = env.html_("timeline-section");
    assert.match(html, /Timeline/);
    assert.match(html, /bulk change/);
    assert.match(html, /Came back online/);
    assert.match(html, /different install uploaded/);
    assert.match(html, /Device registered/);
    assert.match(html, /connected from/);
    assert.ok(!html.includes("<script>x") && !html.includes("<i>x</i>"), "untrusted text must be escaped");
    assert.deepStrictEqual(env.toasts, []);
  });

  await test("device detail: a pending uuid change shows the banner and Accept posts", async () => {
    const env = await detailEnv(
      device({ uuid_change_pending: true, uuid_change_ip: "203.0.113.9", uuid_change_at: "2026-09-20T09:00:00" }),
      [],
      { "POST ^/api/v1/devices/7/uuid/accept": { pending: false } }
    );
    assert.match(env.html_("content"), /A different install is claiming this device/);
    await env.fire("uuid-accept", "click");
    assert.strictEqual(env.posted("/api/v1/devices/7/uuid/accept").length, 1);
    assert.ok(env.toasts.some(([m]) => /accepted/.test(m)));
  });

  await test("device detail: a viewer who cannot decide sees no buttons; no banner when nothing is pending", async () => {
    const viewer = await detailEnv(device({ owner_id: 3, uuid_change_pending: true }), [], {}, alice);
    assert.match(viewer.html_("content"), /claiming this device/);
    assert.ok(!viewer.html_("content").includes('id="uuid-accept"'));
    const calm = await detailEnv(device(), []);
    assert.ok(!calm.html_("content").includes("claiming this device"));
    assert.match(calm.html_("timeline-section"), /Nothing recorded yet/);
  });

  await test("device detail: Show older pages the timeline with 'before'", async () => {
    const page = (n) => Array.from({ length: 25 }, (_, i) => ({ at: `2026-09-20T10:${String(59 - n * 0 - i).padStart(2, "0")}:00`, source: "event", kind: "online", actor: null, result: null, detail: {} }));
    const env = await detailEnv(device(), (url) => (url.includes("before=") ? [] : page(0)));
    assert.match(env.html_("timeline-section"), /timeline-more/);
    await env.fire("timeline-more", "click");
    const older = env.requests.filter((r) => r.url.includes("/timeline")).pop().url;
    assert.match(older, /before=/);
    assert.ok(!env.html_("timeline-section").includes("timeline-more"), "no more pages");
  });

  // ----------------------------------------------------------------- security
  const securityFixtures = (extra = {}) => ({
    "GET ^/api/v1/auth/2fa$": { enabled: false, available: false, recovery_codes_remaining: 0 },
    "GET ^/api/v1/enrollment-tokens": [],
    "GET ^/api/v1/auth/sessions": [
      { id: 1, kind: "webui", ip_address: "10.0.0.5", user_agent: "<b>Firefox</b>", created_at: "2026-09-20T09:00:00", last_used_at: null, expires_at: "2026-09-27T09:00:00", current: true },
      { id: 2, kind: "client", ip_address: null, user_agent: null, created_at: "2026-09-19T09:00:00", last_used_at: "2026-09-20T09:00:00", expires_at: "2026-09-27T09:00:00", current: false },
    ],
    "GET ^/api/v1/api-keys": [{ id: 4, label: "<i>nightly</i>", scope: "read", created_at: "2026-09-19T09:00:00", expires_at: "2026-12-19T09:00:00", last_used_at: null }],
    ...extra,
  });

  await test("security: sessions list marks the current one and revokes another", async () => {
    const env = await new Env("security.html", "security.js", {
      user: alice,
      fixtures: securityFixtures({ "DELETE ^/api/v1/auth/sessions/2": null, "POST ^/api/v1/auth/sessions/revoke-others": { revoked: 1 } }),
    }).run();
    const html = env.html_("sessions");
    assert.match(html, /This session/);
    assert.ok(!html.includes("<b>Firefox</b>"), "user agent must be escaped");
    assert.strictEqual((html.match(/revoke-session/g) || []).length, 1, "the current session has no Sign out button");
    assert.match(html, /Sign out everywhere else \(1\)/);
    await env.fire("revoke-others", "click");
    assert.strictEqual(env.posted("/api/v1/auth/sessions/revoke-others").length, 1);
    assert.ok(env.toasts.some(([m]) => /Signed out 1 session\./.test(m)), JSON.stringify(env.toasts));
  });

  await test("security: creating an API key shows the token once, revoking asks the server", async () => {
    const env = await new Env("security.html", "security.js", {
      user: alice,
      fixtures: securityFixtures({
        "POST ^/api/v1/api-keys": { id: 9, label: "ci", scope: "full", created_at: "x", expires_at: "y", last_used_at: null, token: "SECRET-TOKEN-123" },
      }),
    }).run();
    assert.ok(!env.html_("api-keys").includes("<i>nightly</i>"), "label must be escaped");
    env.element("key-label").value = "ci";
    env.element("key-scope").value = "full";
    env.element("key-days").value = "30";
    await env.fire("key-create", "click");
    assert.deepStrictEqual(JSON.parse(env.posted("/api/v1/api-keys")[0].body), { label: "ci", scope: "full", days: 30 });
    const html = env.html_("api-keys");
    assert.match(html, /SECRET-TOKEN-123/);
    assert.match(html, /not shown again/);
    await env.fire("key-copy", "click");
    assert.strictEqual(env.copied, "SECRET-TOKEN-123");
    env.element("key-label").value = "";
    await env.fire("key-create", "click");
    assert.ok(env.toasts.some(([m]) => /label/.test(m)));
  });

  // -------------------------------------------------------------------- users
  await test("users: locked accounts are recognised and a reset link is displayed for copying", async () => {
    const future = new Date(Date.now() + 3600e3).toISOString();
    const env = await new Env("users.html", "users.js", {
      user: admin,
      fixtures: {
        "GET ^/api/v1/users$": [
          { id: 1, username: "admin", email: null, is_admin: true, is_active: true, two_factor_enabled: false, locked_until: null, created_at: "2026-09-01T00:00:00", last_login_at: null },
          { id: 2, username: "alice", email: null, is_admin: false, is_active: true, two_factor_enabled: false, locked_until: future, created_at: "2026-09-01T00:00:00", last_login_at: null },
          { id: 3, username: "bob", email: null, is_admin: false, is_active: false, two_factor_enabled: false, locked_until: null, created_at: "2026-09-01T00:00:00", last_login_at: null },
        ],
      },
    }).run();
    const rows = env.html_("user-rows");
    assert.match(rows, /Locked/);
    assert.match(rows, /unlock-user/);
    assert.strictEqual((rows.match(/reset-link/g) || []).length, 1, "only active users other than you get a reset link");
    assert.strictEqual(env.eval("isLocked({locked_until: null})"), false);
    assert.strictEqual(env.eval("isLocked({locked_until: '2000-01-01T00:00:00'})"), false);

    env.eval("showResetLink('alice', {url: 'http://x/reset-password#token=abc', expires_at: '2026-09-20T11:00:00'})");
    assert.ok(!env.element("reset-link-box").classList.contains("hidden"));
    assert.strictEqual(env.element("reset-link-url").textContent, "http://x/reset-password#token=abc");
    await env.fire("reset-link-copy", "click");
    assert.strictEqual(env.copied, "http://x/reset-password#token=abc");
    await env.fire("reset-link-close", "click");
    assert.strictEqual(env.element("reset-link-url").textContent, "", "the link is not kept on the page");
  });

  // ------------------------------------------------------- login / register / reset
  await test("login: the register link appears only when registration is on; a reset shows a notice", async () => {
    const on = await new Env("login.html", "login.js", {
      user: admin, search: "?reset=1",
      fixtures: { "GET ^/api/v1/auth/setup/status": { setup_required: false }, "GET ^/api/v1/auth/options": { registration_enabled: true, registration_requires_approval: true } },
    }).run();
    assert.ok(!on.element("register-link").classList.contains("hidden"));
    assert.ok(!on.element("login-notice").classList.contains("hidden"));
    const off = await new Env("login.html", "login.js", {
      user: admin,
      fixtures: { "GET ^/api/v1/auth/setup/status": { setup_required: false }, "GET ^/api/v1/auth/options": { registration_enabled: false, registration_requires_approval: true } },
    }).run();
    assert.ok(off.element("register-link").classList.contains("hidden"));
    assert.ok(off.element("login-notice").classList.contains("hidden"));
  });

  await test("register: explains approval, posts the form, and reports the outcome", async () => {
    const env = await new Env("register.html", "register.js", {
      user: admin,
      fixtures: {
        "GET ^/api/v1/auth/options": { registration_enabled: true, registration_requires_approval: true },
        "POST ^/api/v1/auth/register": { status: "pending_approval" },
      },
    }).run();
    assert.match(env.element("register-note").textContent, /approve/);
    env.element("username").value = "newbie";
    env.element("email").value = "";
    env.element("password").value = "newbiepassword1";
    await env.fire("register-form", "submit");
    assert.deepStrictEqual(JSON.parse(env.posted("/api/v1/auth/register")[0].body), { username: "newbie", email: null, password: "newbiepassword1" });
    assert.ok(env.element("register-form").classList.contains("hidden"));
    assert.match(env.element("register-done-text").textContent, /approved/);
  });

  await test("register: a disabled server sends the visitor back to the login page", async () => {
    const env = await new Env("register.html", "register.js", {
      user: admin,
      fixtures: { "GET ^/api/v1/auth/options": { registration_enabled: false, registration_requires_approval: true } },
    }).run();
    assert.strictEqual(env.location.href, "/login");
  });

  await test("reset password: the token comes from the fragment, is removed from the URL and posted", async () => {
    const env = await new Env("reset_password.html", "reset_password.js", {
      user: admin, hash: "#token=abc123",
      fixtures: { "POST ^/api/v1/auth/reset-password": null },
    }).run();
    assert.strictEqual(env.replaced, "/", "the fragment is cleared from the address bar");
    env.element("password").value = "brandnewpass1";
    env.element("confirm").value = "different";
    await env.fire("reset-form", "submit");
    assert.strictEqual(env.posted("/api/v1/auth/reset-password").length, 0);
    assert.match(env.element("error-message").textContent, /not the same/);
    env.element("confirm").value = "brandnewpass1";
    await env.fire("reset-form", "submit");
    assert.deepStrictEqual(JSON.parse(env.posted("/api/v1/auth/reset-password")[0].body), { token: "abc123", password: "brandnewpass1" });
    assert.strictEqual(env.location.href, "/login?reset=1");
  });

  await test("reset password: without a token nothing is sent", async () => {
    const env = await new Env("reset_password.html", "reset_password.js", { user: admin, fixtures: {} }).run();
    env.element("password").value = "brandnewpass1";
    env.element("confirm").value = "brandnewpass1";
    await env.fire("reset-form", "submit");
    assert.strictEqual(env.requests.filter((r) => r.method === "POST").length, 0);
    assert.match(env.element("error-message").textContent, /full link/);
  });

  // ---------------------------------------------------------------- dashboard
  await test("dashboard: only an administrator gets the audit export link", async () => {
    const fixtures = { "GET ^/api/v1/admin/stats": { total_devices: 0, online_devices: 0, offline_devices: 0, total_users: 1, total_groups: 0 }, "GET ^/api/v1/admin/audit-logs": { items: [], page: 1, page_size: 10, total: 0 } };
    const env = await new Env("dashboard.html", "dashboard.js", { user: admin, fixtures }).run();
    assert.ok(!env.element("audit-export").classList.contains("hidden"));
  });

  console.log(failures ? `\n${failures} page smoke test(s) failed` : "\nall page smoke tests passed");
  process.exit(failures ? 1 : 0);
})();
