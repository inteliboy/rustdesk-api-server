const fs = require("fs");
// Usage: node helpers_check.js <path to web/static/js/app.js>
const src = fs.readFileSync(process.argv[2], "utf8");

// Evaluate the real app.js with a minimal DOM stub so we test the shipped code.
const document = { getElementById: () => null, cookie: "", addEventListener() {} };
const window = { location: { protocol: "https:", host: "x", pathname: "/", search: "" } };
const fn = new Function("document", "window", "fetch", src + "\nreturn { escapeHtml, safeColor, safeNextPath, tagBadges, apiSchemeBadge, describeActivity, fmtCpu, fmtCpuName, fmtMemory, fmtPlatform, connectLink, ipLabel, isLocalIp, addressBookRuleName, parseServerDate, fmtDate, userLink, setUser: (u) => { currentUser = u; } };");
const h = fn(document, window, () => {});

let failed = 0;
const eq = (name, got, want) => {
  if (got !== want) { failed++; console.log("FAIL", name, "\n  got :", JSON.stringify(got), "\n  want:", JSON.stringify(want)); }
};
const has = (name, got, needle) => {
  if (!got.includes(needle)) { failed++; console.log("FAIL", name, "missing", needle, "in", got); }
};
const lacks = (name, got, needle) => {
  if (got.includes(needle)) { failed++; console.log("FAIL", name, "contains raw", needle, "in", got); }
};

const XSS = '<img src=x onerror=alert(1)>';

eq("next ok", h.safeNextPath("/devices?tag_id=3"), "/devices?tag_id=3");
for (const bad of ["javascript:alert(1)", "//evil.example", "/\\evil.example", "https://evil.example", "", null, undefined, "devices"]) {
  eq("next bad " + bad, h.safeNextPath(bad), "/dashboard");
}

eq("color ok", h.safeColor("#aBc123"), "#aBc123");
eq("color css injection", h.safeColor("red;background:url(x)"), "#64748b");
eq("color attr breakout", h.safeColor('#fff" onmouseover="alert(1)'), "#64748b");
eq("color 3-digit rejected", h.safeColor("#fff"), "#64748b");
eq("color int from client", h.safeColor(4278190080), "#64748b");

eq("escape", h.escapeHtml(XSS + `"'&`), "&lt;img src=x onerror=alert(1)&gt;&quot;&#39;&amp;");
eq("escape null", h.escapeHtml(null), "");

const badges = h.tagBadges([{ name: XSS, color: 'red" onmouseover="alert(1)' }]);
lacks("tag name", badges, "<img");
lacks("tag color", badges, "onmouseover=\"");
has("tag color fallback", badges, "#64748b");

// Only the two known words become a badge; anything else a server sent is not echoed.
has("https badge", h.apiSchemeBadge("https"), "HTTPS");
has("http badge", h.apiSchemeBadge("http"), ">HTTP<");
eq("no scheme yet", h.apiSchemeBadge(null), "-");
eq("scheme junk", h.apiSchemeBadge(XSS), "-");

// Audit-log feed text is inserted as HTML by its callers.
const mk = (action, detail, actor = XSS) => ({ action, detail, actor_username: actor, result: "success", target_type: "x", target_id: 1 });
for (const [action, detail] of [
  ["user_created", { username: XSS }],
  ["group_deleted", { name: XSS }],
  ["tag_deleted", { name: XSS }],
  ["device_deleted", { rustdesk_id: XSS, alias: XSS }],
  ["device_shared", { shared_with: XSS, permission: XSS }],
  ["address_book_entry_created", { rustdesk_id: XSS, alias: XSS }],
  ["address_book_created", { book: XSS }],
  ["address_book_shared", { book: XSS, username: XSS, rule: XSS }],
  ["address_book_unshared", { book: XSS, username: XSS }],
  ["account_locked", { username: XSS, failed_logins: XSS }],
  ["user_unlocked", { username: XSS }],
  ["user_registered", { username: XSS, pending_approval: true }],
  ["password_reset_issued", { username: XSS }],
  ["api_key_created", { label: XSS, scope: XSS }],
  ["api_key_revoked", { label: XSS }],
  ["device_uuid_change_requested", { rustdesk_id: XSS }],
  ["device_uuid_accepted", { rustdesk_id: XSS }],
  ["data_exported", { what: XSS, rows: XSS }],
  ["devices_imported", { created: XSS, updated: XSS }],
  ["sessions_revoked", { count: XSS }],
  ["device_updated", { rustdesk_id: XSS, alias: XSS, via: "bulk" }],
  ["some_future_action", {}],
]) {
  const text = h.describeActivity(mk(action, detail));
  lacks("activity " + action, text, "<img");
}
lacks("activity failed login", h.describeActivity({ action: "login", result: "failure", detail: { username: XSS }, actor_username: null }), "<img");
has("activity still readable", h.describeActivity(mk("tag_deleted", { name: "prod" }, "admin")), '<span class="whitespace-nowrap">admin</span> deleted tag "prod"');

// Login origin: IP + browser/OS, hostile values (including the raw header used
// as a title="" attribute) must stay inert.
const loginOk = (detail, ip = "10.0.0.5") => ({ action: "login", result: "success", actor_username: "admin", ip_address: ip, detail });
eq("login origin", h.describeActivity(loginOk({ via: "webui", browser: "Chrome 130", os: "Windows", user_agent: "Mozilla/5.0" })),
  '<span class="whitespace-nowrap">admin</span> logged in from 10.0.0.5 using <span title="Mozilla/5.0" class="underline decoration-dotted decoration-slate-400">Chrome 130 on Windows</span>');
eq("login old entry", h.describeActivity(loginOk({ via: "webui" })), "<span class=\"whitespace-nowrap\">admin</span> logged in from 10.0.0.5");
eq("login no detail at all", h.describeActivity({ action: "login", result: "success", actor_username: "admin", ip_address: null, detail: null }), "<span class=\"whitespace-nowrap\">admin</span> logged in");
has("login rustdesk client", h.describeActivity(loginOk({ via: "rustdesk_client" })), ">RustDesk client</span>");
has("login failure keeps origin", h.describeActivity({ action: "login", result: "failure", actor_username: null, ip_address: "1.2.3.4", detail: { username: "bob", browser: "curl" } }), 'for "bob" from <span class="whitespace-nowrap cursor-help underline decoration-dotted decoration-slate-400" tabindex="0" data-ip="1.2.3.4">1.2.3.4</span> using');
const hostile = h.describeActivity(loginOk({ via: "webui", browser: XSS, os: XSS, user_agent: '"><img src=x onerror=alert(1)>' }, XSS));
lacks("login hostile tag", hostile, "<img");
lacks("login hostile title breakout", hostile, 'title=""');

eq("activity address book shared", h.describeActivity(mk("address_book_shared", { book: "Team", username: "alice", rule: 2 }, "admin")),
  '<span class="whitespace-nowrap">admin</span> shared the address book "Team" with "alice" (read / write)');
eq("rule names", [1, 2, 3, 9].map(h.addressBookRuleName).join("|"), "Read only|Read / write|Full control|Unknown access");

eq("activity setup entry without username", h.describeActivity({ action: "user_created", result: "success", actor_id: null, actor_username: "admin", detail: { via: "setup" } }),
  '<span class="whitespace-nowrap">admin</span> created the first administrator account');

// CPU: the client's "<logical>/<physical> cores" becomes cores / threads.
eq("cpu real 5900X", h.fmtCpu("AMD Ryzen 9 5900X 12-Core Processor, 3.61GHz, 24/12 cores"),
  "AMD Ryzen 9 5900X 12-Core Processor, 3.61 GHz, 12 cores / 24 threads");
eq("cpu no SMT", h.fmtCpu("Intel(R) N100, 0.80GHz, 4/4 cores"), "Intel(R) N100, 0.80 GHz, 4 cores / 4 threads");
eq("cpu already physical/logical order", h.fmtCpu("X, 8/16 cores"), "X, 8 cores / 16 threads");
eq("cpu unknown physical", h.fmtCpu("X, 8/0 cores"), "X, 8 threads");
eq("cpu unrecognised shape untouched", h.fmtCpu("Intel i7 (8 cores)"), "Intel i7 (8 cores)");
eq("cpu empty", h.fmtCpu(null), "");
eq("cpu hostile stays text", h.fmtCpu(XSS + ", 4/2 cores"), XSS + ", 2 cores / 4 threads");

eq("cpu freq without core count", h.fmtCpu("Xeon E5-2680 v4 @ 2.40GHz"), "Xeon E5-2680 v4 @ 2.40 GHz");
eq("cpu model letters left alone", h.fmtCpu("Core i5-1135G7, 4/8 cores"), "Core i5-1135G7, 4 cores / 8 threads");
eq("memory unit spaced", h.fmtMemory("63.91GB"), "63.91 GB");
eq("memory MB/TB", h.fmtMemory("512MB") + "|" + h.fmtMemory("2TB"), "512 MB|2 TB");
eq("memory already spaced", h.fmtMemory("15.5 GB"), "15.5 GB");
eq("memory empty", h.fmtMemory(null), "");

eq("platform capitalised", ["windows", "linux", "android"].map(h.fmtPlatform).join(","), "Windows,Linux,Android");
eq("platform macOS/iOS", ["macos", "MacOS", "ios"].map(h.fmtPlatform).join(","), "macOS,macOS,iOS");
eq("platform empty", h.fmtPlatform(null), "");
eq("platform already fine", h.fmtPlatform("Windows"), "Windows");

eq("connect link", h.connectLink("123456789"),
  '<a href="rustdesk://connect/123456789" title="Connect with RustDesk" class="font-mono text-link hover:underline whitespace-nowrap">123456789</a>');
const hostileConnect = h.connectLink('1"><img src=x onerror=alert(1)>/../x');
lacks("connect link hostile", hostileConnect, "<img");
has("connect link keeps scheme and encodes id", hostileConnect, 'href="rustdesk://connect/1%22%3E%3Cimg');

eq("ip public is marked for lookup", h.ipLabel("8.8.8.8"), '<span class="whitespace-nowrap cursor-help underline decoration-dotted decoration-slate-400" tabindex="0" data-ip="8.8.8.8">8.8.8.8</span>');
eq("ip ipv6 public is marked", h.ipLabel("2606:4700:4700::1111").includes('data-ip="2606:4700:4700::1111"'), true);
for (const local of ["10.1.2.3", "127.0.0.1", "192.168.0.9", "172.16.0.1", "172.31.255.1", "169.254.1.1", "100.64.0.1", "::1", "fe80::1", "fd12::1"]) {
  eq("ip local not looked up " + local, h.ipLabel(local), local);
}
for (const pub of ["172.32.0.1", "172.15.0.1", "100.63.0.1", "100.128.0.1", "11.0.0.1"]) has("ip near-private is public " + pub, h.ipLabel(pub), "data-ip");
const hostileIp = h.ipLabel('1.2.3.4"><img src=x onerror=alert(1)>');
lacks("ip hostile", hostileIp, "<img");
lacks("ip hostile not marked", hostileIp, "data-ip");
lacks("ip hostile stays inert", hostileIp, "tabindex");
eq("ip empty", h.ipLabel(""), "");

// Device list shows only the processor name.
eq("cpu name real", h.fmtCpuName("AMD Ryzen 9 5900X 12-Core Processor, 3.61GHz, 24/12 cores"), "AMD Ryzen 9 5900X 12-Core Processor");
eq("cpu name intel", h.fmtCpuName("Intel(R) N100, 0.80GHz, 4/4 cores"), "Intel(R) N100");
eq("cpu name at-freq", h.fmtCpuName("Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz"), "Intel(R) Xeon(R) CPU E5-2680 v4");
eq("cpu name at-freq plus cores", h.fmtCpuName("Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz, 12/6 cores"), "Intel(R) Core(TM) i7-8700 CPU");
eq("cpu name unrecognised shape", h.fmtCpuName("Apple M2"), "Apple M2");
eq("cpu name only freq falls back", h.fmtCpuName("3.61GHz"), "3.61GHz");
eq("cpu name empty", h.fmtCpuName(null), "");

// Username links: only administrators can open the Users page.
h.setUser({ id: 1, is_admin: true });
eq("userLink admin", h.userLink(7, "alice"),
  '<a href="/users?id=7" class="text-link hover:underline whitespace-nowrap">alice</a>');
eq("userLink no id", h.userLink(null, "alice"), '<span class="whitespace-nowrap">alice</span>');
const linked = h.userLink('7"><img src=x onerror=alert(1)>', XSS);
lacks("userLink hostile id", linked, "<img");
lacks("userLink hostile name", linked, "<img");
has("activity actor is a link", h.describeActivity({ action: "logout", result: "success", actor_id: 3, actor_username: "bob", detail: null }), 'href="/users?id=3"');
eq("activity unknown actor", h.describeActivity({ action: "logout", result: "success", actor_id: null, actor_username: null, detail: null }), "Someone logged out");
h.setUser({ id: 2, is_admin: false });
eq("userLink non-admin is plain", h.userLink(7, "alice"), '<span class="whitespace-nowrap">alice</span>');
h.setUser(null);

// Zoneless API timestamps are UTC, whatever zone this machine is in.
const utcMs = Date.UTC(2026, 8, 20, 10, 36, 13);
eq("date zoneless is UTC", h.parseServerDate("2026-09-20T10:36:13").getTime(), utcMs);
eq("date zoneless with micros", h.parseServerDate("2026-09-20T10:36:13.298167").getTime(), utcMs + 298);
eq("date Z unchanged", h.parseServerDate("2026-09-20T10:36:13Z").getTime(), utcMs);
eq("date offset respected", h.parseServerDate("2026-09-20T12:36:13+02:00").getTime(), utcMs);
eq("date negative offset respected", h.parseServerDate("2026-09-20T05:36:13-05:00").getTime(), utcMs);
eq("fmtDate local render", h.fmtDate("2026-09-20T10:36:13"), new Date(utcMs).toLocaleString());
eq("fmtDate empty", h.fmtDate(null), "Never");
eq("fmtDate garbage", h.fmtDate("not a date"), "Unknown");

const purge = (detail) => h.describeActivity({ action: "log_retention_purge", result: "success", detail, actor_username: null, actor_id: null });
eq("retention purge text", purge({ audit_logs: 1, connection_logs: 2, file_transfer_logs: 0 }),
  "Retention policy removed 1 activity log entry, 2 connection logs");
eq("retention purge many", purge({ audit_logs: 3, connection_logs: 0, file_transfer_logs: 1 }),
  "Retention policy removed 3 activity log entries, 1 file transfer log");
lacks("retention purge hostile detail", purge({ audit_logs: XSS, connection_logs: 1 }), "<img");

has("bulk change is labelled", h.describeActivity(mk("device_updated", { rustdesk_id: "1", via: "bulk" }, "admin")), "(bulk change)");
has("lock is described", h.describeActivity(mk("account_locked", { username: "alice", failed_logins: 10 })), "was locked after 10 failed sign-ins");
has("registration waiting", h.describeActivity(mk("user_registered", { username: "n", pending_approval: true })), "waiting for approval");

console.log(failed ? `${failed} FAILED` : "all helper checks passed");
process.exit(failed ? 1 : 0);
