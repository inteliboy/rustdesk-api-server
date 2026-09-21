// Shared client-side helpers for the RustDesk API Server WebUI.
// All data access goes through the JSON API under /api/v1/... ; this file
// never talks to the database directly and never trusts anything about
// permissions beyond what the API actually returns (the API is the only
// real authorization boundary - see CLAUDE.md section 66).

// i18n.js (loaded before this file) provides t(); this keeps the helpers usable without it.
if (typeof globalThis.t !== "function") {
  globalThis.t = (key, values) =>
    String(key).replace(/\{(\d+)\}/g, (whole, n) => (values && values[n - 1] !== undefined ? values[n - 1] : whole));
}

function getCookie(name) {
  const match = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
  return match ? decodeURIComponent(match[2]) : null;
}

async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  const method = (options.method || "GET").toUpperCase();
  if (method !== "GET") {
    const csrf = getCookie("rd_csrf");
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  let body = null;
  const text = await response.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    const message = body && body.error && body.error.message ? body.error.message : `Request failed (${response.status})`;
    const err = new Error(message);
    err.status = response.status;
    err.body = body;
    throw err;
  }
  return body;
}

function toast(message, kind = "info") {
  const container = document.getElementById("toast-root");
  if (!container) return;
  const el = document.createElement("div");
  const colors = {
    info: "bg-brand-600",
    error: "bg-red-600",
    success: "bg-green-600",
  };
  el.className = `toast rounded-lg px-4 py-3 text-sm text-white shadow-lg ${colors[kind] || colors.info}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// The signed-in user, remembered so shared helpers (userLink) can tell whether
// the viewer may open the administrator-only Users page.
let currentUser = null;

async function requireAuth() {
  try {
    currentUser = await api("/api/v1/auth/me");
    return currentUser;
  } catch (err) {
    window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
    return null;
  }
}

// Escapes text for safe interpolation into innerHTML. Required for anything
// a RustDesk client reports (peer names, file names, paths): those arrive
// on unauthenticated endpoints, so they are attacker-controlled.
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Only a plain #rrggbb reaches a style attribute. Tag colors are settable by
// any user and pushed by clients via /api/ab, so anything else falls back.
function safeColor(value) {
  return /^#[0-9a-fA-F]{6}$/.test(value || "") ? value : "#64748b";
}

// The client reports "<name>, <freq>, <logical>/<physical> cores" - e.g. a
// 5900X (12 cores, 24 threads) arrives as "..., 24/12 cores" - which reads as
// cores/threads but is threads first. Shown as "12 cores / 24 threads". The
// stored value stays verbatim; anything not in that shape is shown unchanged.
// physical_core_count can be unknown (reported as 0), then only threads show.
// The clock speed also gets a space before its unit ("3.61GHz" -> "3.61 GHz").
function fmtCpu(cpu) {
  const text = String(cpu ?? "").replace(/(\d)(GHz|MHz)\b/g, "$1 $2");
  const m = /^(.*?),?\s*(\d+)\/(\d+) cores\s*$/.exec(text);
  if (!m) return text;
  const a = parseInt(m[2], 10), b = parseInt(m[3], 10);
  const threads = Math.max(a, b), cores = Math.min(a, b);
  const counts = cores > 0 ? `${cores} cores / ${threads} threads` : `${threads} threads`;
  return m[1] ? `${m[1]}, ${counts}` : counts;
}

// The client reports the platform in lowercase ("windows", "linux", "macos").
// Display-only; "macOS"/"iOS" keep their own capitalization.
const PLATFORM_NAMES = { macos: "macOS", ios: "iOS" };
function fmtPlatform(platform) {
  const text = String(platform ?? "");
  return PLATFORM_NAMES[text.toLowerCase()] || text.charAt(0).toUpperCase() + text.slice(1);
}

// Just the processor's name for the device list ("AMD Ryzen 9 5900X 12-Core
// Processor"); the clock speed and core/thread counts are on the device page.
function fmtCpuName(cpu) {
  const text = String(cpu ?? "");
  const name = text
    .replace(/,?\s*\d+\/\d+ cores\s*$/, "")
    .replace(/\s*[,@]?\s*\d+(?:\.\d+)?\s?[GM]Hz\s*$/i, "")
    .trim();
  return name || text;
}

// "63.91GB" -> "63.91 GB". Anything not shaped like number+unit is unchanged.
function fmtMemory(memory) {
  return String(memory ?? "").replace(/(\d)([KMGT]i?B)\b/g, "$1 $2");
}

// Quick pre-filter so obviously private addresses never trigger a request;
// the server (services/ip_info.py) is the authority on what is public.
function isLocalIp(ip) {
  if (/^(10|127)\./.test(ip) || /^192\.168\./.test(ip) || /^169\.254\./.test(ip)) return true;
  const m = /^(?:172\.(\d+)|100\.(\d+))\./.exec(ip);
  if (m) return (m[1] !== undefined && m[1] >= 16 && m[1] <= 31) || (m[2] !== undefined && m[2] >= 64 && m[2] <= 127);
  return /^(::1?|f[cd][0-9a-f]{2}:|fe80:)/i.test(ip);
}

// An IP address. A public one gets a dotted underline and shows its registry
// (WHOIS-style) details in a tooltip on hover or keyboard focus; anything else
// (private ranges, junk) is plain text and never triggers a lookup.
function ipLabel(ip) {
  const text = escapeHtml(ip);
  if (!/^[0-9a-fA-F:.]+$/.test(ip || "") || isLocalIp(ip)) return text;
  return `<span class="whitespace-nowrap cursor-help underline decoration-dotted decoration-slate-400" tabindex="0" data-ip="${text}">${text}</span>`;
}

const ipInfoCache = new Map(); // ip -> Promise of the API answer (or null)

// Looked up on first hover only (not when a page renders), then remembered for
// the page's lifetime. A failed or rate-limited answer is forgotten so a later
// hover retries.
function lookupIp(ip) {
  if (!ipInfoCache.has(ip)) {
    ipInfoCache.set(
      ip,
      api(`/api/v1/ip-info?ip=${encodeURIComponent(ip)}`)
        .catch(() => null)
        .then((info) => {
          if (!info || info.status === "unavailable") ipInfoCache.delete(ip);
          return info;
        })
    );
  }
  return ipInfoCache.get(ip);
}

let ipTooltipEl = null;
let ipTooltipFor = null; // the [data-ip] element the tooltip currently belongs to

function ipTooltip() {
  if (!ipTooltipEl) {
    ipTooltipEl = document.createElement("div");
    ipTooltipEl.setAttribute("role", "tooltip");
    ipTooltipEl.className = "card fixed z-50 hidden max-w-xs px-3 py-2 text-xs pointer-events-none";
    document.body.appendChild(ipTooltipEl);
  }
  return ipTooltipEl;
}

// Registry data is third-party text: only ever inserted as text nodes.
function ipTooltipRow(label, value, valueClass = "") {
  const row = document.createElement("div");
  row.className = "flex gap-2";
  const v = document.createElement("span");
  v.className = valueClass;
  v.textContent = value;
  if (label) {
    const l = document.createElement("span");
    l.className = "shrink-0 text-slate-500";
    l.textContent = label;
    row.append(l);
  }
  row.append(v);
  return row;
}

function ipTooltipContent(ip, info) {
  if (info === undefined) return [ipTooltipRow("", `Looking up ${ip}...`, "text-slate-400")];
  if (info && info.status === "ok") {
    const rows = [
      ["Network", info.network],
      ["Organization", info.organization],
      ["Country", info.country],
      ["Range", info.cidr],
      ["Registry", info.registry],
    ].filter(([, value]) => value);
    return [ipTooltipRow("", ip, "font-mono font-medium"), ...rows.map(([label, value]) => ipTooltipRow(label, value))];
  }
  const why = info && info.status === "disabled" ? "IP lookup is disabled on this server." : "No registry details available right now.";
  return [ipTooltipRow("", ip, "font-mono font-medium"), ipTooltipRow("", why, "text-slate-400")];
}

function placeIpTooltip(el) {
  const tip = ipTooltip();
  const anchor = el.getBoundingClientRect();
  const size = tip.getBoundingClientRect();
  const left = Math.max(8, Math.min(anchor.left, window.innerWidth - size.width - 8));
  const below = anchor.bottom + 6;
  const top = below + size.height > window.innerHeight - 8 ? Math.max(8, anchor.top - size.height - 6) : below;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function hideIpTooltip() {
  ipTooltipFor = null;
  if (ipTooltipEl) ipTooltipEl.classList.add("hidden");
}

function showIpTooltip(el) {
  const ip = el.dataset.ip;
  ipTooltipFor = el;
  const tip = ipTooltip();
  tip.replaceChildren(...ipTooltipContent(ip, undefined));
  tip.classList.remove("hidden");
  placeIpTooltip(el);
  lookupIp(ip).then((info) => {
    if (ipTooltipFor !== el) return; // the pointer has moved on
    tip.replaceChildren(...ipTooltipContent(ip, info));
    placeIpTooltip(el);
  });
}

if (typeof document !== "undefined" && typeof window !== "undefined" && typeof window.addEventListener === "function") {
  const ipTarget = (evt) => (evt.target instanceof Element ? evt.target.closest("[data-ip]") : null);
  const enter = (evt) => {
    const el = ipTarget(evt);
    if (el && el !== ipTooltipFor) showIpTooltip(el);
  };
  const leave = (evt) => {
    const el = ipTarget(evt);
    if (el && el === ipTooltipFor && !el.contains(evt.relatedTarget)) hideIpTooltip();
  };
  document.addEventListener("mouseover", enter);
  document.addEventListener("focusin", enter);
  document.addEventListener("mouseout", leave);
  document.addEventListener("focusout", leave);
  document.addEventListener("keydown", (evt) => evt.key === "Escape" && hideIpTooltip());
  window.addEventListener("scroll", hideIpTooltip, true);
}

// A table row with data-href is clickable as a whole (the CSP forbids inline
// onclick handlers, so this one delegated listener replaces them). Clicks on
// links, buttons and form controls inside the row keep their own behaviour and
// do not also trigger the row.
document.addEventListener("click", (event) => {
  const row = event.target.closest && event.target.closest("tr[data-href]");
  if (!row || event.target.closest("a, button, input, select, textarea, label")) return;
  window.location.href = row.dataset.href;
});

// A RustDesk ID that starts a connection in the installed client via the
// rustdesk:// URL scheme (the same link the Address Book uses). A surrounding
// clickable table row ignores clicks on it (see the listener above).
function connectLink(rustdeskId) {
  return `<a href="rustdesk://connect/${encodeURIComponent(rustdeskId)}" title="Connect with RustDesk" class="font-mono text-link hover:underline whitespace-nowrap">${escapeHtml(rustdeskId)}</a>`;
}

// A username that opens that user's details on the Users page. That page is
// administrator-only, so anyone else (and entries with no resolvable user)
// gets plain text rather than a dead link.
function userLink(id, username) {
  const name = escapeHtml(username);
  if (id == null || !currentUser || !currentUser.is_admin) return `<span class="whitespace-nowrap">${name}</span>`;
  return `<a href="/users?id=${encodeURIComponent(id)}" class="text-link hover:underline whitespace-nowrap">${name}</a>`;
}

function tagBadges(tags) {
  if (!tags || tags.length === 0) return "-";
  return tags
    .map((t) => {
      const c = safeColor(t.color);
      return `<span class="badge" style="background:${c}22;color:${c}"><span class="badge-dot" style="background:${c}"></span>${escapeHtml(t.name)}</span>`;
    })
    .join(" ");
}

// How a device's last heartbeat reached the server: "https" (the current address) or
// plain "http" (e.g. a client that lost its API server setting and fell back to
// <ID server>:21114). Never trusts the value beyond those two words.
function apiSchemeBadge(scheme) {
  if (scheme !== "https" && scheme !== "http") return "-";
  const secure = scheme === "https";
  const c = secure ? "#16a34a" : "#d97706";
  const tip = secure ? "Reaches the API server over HTTPS" : "Reaches the API server over plain HTTP";
  return `<span class="badge" title="${tip}" style="background:${c}22;color:${c}"><span class="badge-dot" style="background:${c}"></span>${secure ? "HTTPS" : "HTTP"}</span>`;
}

// The post-login redirect target. Only same-origin absolute paths are
// allowed: a bare assignment of ?next= to location.href would allow
// "javascript:..." (script execution) and "//evil.example" (open redirect).
function safeNextPath(value) {
  return typeof value === "string" && /^\/(?![\/\\])/.test(value) ? value : "/dashboard";
}

// The API stores and returns UTC, but SQLite drops the zone, so timestamps
// arrive as "2026-09-20T10:36:13" with no "Z". `new Date()` reads that as
// *local* time and every displayed time ends up off by the UTC offset. A
// value with no explicit zone is therefore UTC.
function parseServerDate(iso) {
  const text = String(iso);
  return new Date(/(?:Z|[+-]\d{2}(?::?\d{2})?)$/i.test(text) ? text : `${text}Z`);
}

// Rendered in the viewer's own time zone (the system's local time).
function fmtDate(iso) {
  if (!iso) return "Never";
  const d = parseServerDate(iso);
  return Number.isNaN(d.getTime()) ? "Unknown" : d.toLocaleString(window.rdLanguage || undefined);
}

// " from 10.0.0.5 using Chrome 130 on Windows" for login entries. Older
// entries predate user-agent capture and simply show less. `safe` holds the
// already-escaped detail values; the raw header is the hover text.
function loginOrigin(item, safe) {
  // In an element of its own, so the words around it are text nodes that can be translated.
  const ip = item.ip_address ? `<span class="whitespace-nowrap">${ipLabel(item.ip_address)}</span>` : "";
  let via = "";
  if (item.detail && item.detail.via === "rustdesk_client") {
    // The version is what that device last reported in sysinfo; absent until it has.
    via = safe.client_version ? t("RustDesk client {1}", [safe.client_version]) : t("RustDesk client");
  }
  let agent = safe.browser || via;
  if (safe.os) agent = agent ? t("{1} on {2}", [agent, safe.os]) : safe.os;
  let text = ip ? ` from ${ip}` : "";
  if (item.result === "success" && item.detail && item.detail.two_factor) text += " (two-factor)";
  if (agent) {
    const tip = safe.user_agent ? ` title="${safe.user_agent}"` : "";
    text += ` using <span${tip} class="underline decoration-dotted decoration-slate-400">${agent}</span>`;
  }
  return text;
}

// Renders a human-readable summary for an audit log entry (shared by the
// dashboard's global feed and the per-user activity panel). `includeActor`
// is false in the per-user panel, where who did it is already the page
// context. `who` falls back to "Someone" for events with no resolvable
// actor (e.g. a failed login for an unknown username - AuditLog.actor_id
// is nullable for exactly that case).
function describeActivity(item, { includeActor = true } = {}) {
  // Every value here is user/client-supplied, and the result is inserted as
  // HTML by the callers, so it is escaped up front.
  const who = item.actor_username ? userLink(item.actor_id, item.actor_username) : `<span class="whitespace-nowrap">Someone</span>`;
  const prefix = includeActor ? `${who} ` : "";
  const safe = Object.fromEntries(Object.entries(item.detail || {}).map(([k, v]) => [k, escapeHtml(v)]));
  switch (item.action) {
    case "login": {
      const origin = loginOrigin(item, safe);
      return item.result === "success"
        ? `${prefix}logged in${origin}`
        : item.detail && item.detail.two_factor
          ? `Failed two-factor verification${origin}`
          : `Failed login attempt for "${safe.username || "unknown user"}"${origin}`;
    }
    case "logout":
      return `${prefix}logged out`;
    case "user_created":
      // First-run setup entries written before the username was recorded have none.
      return safe.username
        ? `${prefix}created user "${safe.username}"`
        : `${prefix}created the first administrator account`;
    case "user_updated":
      return `${prefix}updated user "${safe.username}"`;
    case "user_password_reset":
      return `${prefix}reset the password for "${safe.username}"`;
    case "user_deleted":
      return `${prefix}deleted user "${safe.username}"`;
    case "group_created":
      return `${prefix}created group "${safe.name}"`;
    case "group_updated":
      return `${prefix}renamed/updated group "${safe.name}"`;
    case "group_deleted":
      return `${prefix}deleted group "${safe.name}"`;
    case "tag_created":
      return `${prefix}created tag "${safe.name}"`;
    case "tag_updated":
      return `${prefix}updated tag "${safe.name}"`;
    case "tag_deleted":
      return `${prefix}deleted tag "${safe.name}"`;
    case "device_updated":
      return `${prefix}updated device <span class="whitespace-nowrap">${safe.alias ? `"${safe.alias}" (${safe.rustdesk_id})` : safe.rustdesk_id}</span>${item.detail && item.detail.via === "bulk" ? " (bulk change)" : ""}`;
    case "device_deleted":
      return `${prefix}deleted device <span class="whitespace-nowrap">${safe.alias ? `"${safe.alias}" (${safe.rustdesk_id})` : safe.rustdesk_id}</span>`;
    case "device_shared":
      return `${prefix}shared a device with "${safe.shared_with}" (${safe.permission})`;
    case "device_unshared":
      return `${prefix}stopped sharing a device with "${safe.shared_with}"`;
    case "connection_log_deleted":
      return `${prefix}deleted a connection log (${safe.rustdesk_id})`;
    case "file_log_deleted":
      return `${prefix}deleted a file transfer log (${safe.rustdesk_id})`;
    case "connection_logs_cleared":
      return `${prefix}cleared ${safe.deleted} connection log${item.detail && item.detail.deleted === 1 ? "" : "s"}`;
    case "file_logs_cleared":
      return `${prefix}cleared ${safe.deleted} file transfer log${item.detail && item.detail.deleted === 1 ? "" : "s"}`;
    case "alarm_log_deleted":
      return `${prefix}deleted an alarm log (${safe.rustdesk_id})`;
    case "alarm_logs_cleared":
      return `${prefix}cleared ${safe.deleted} alarm log${item.detail && item.detail.deleted === 1 ? "" : "s"}`;
    case "log_retention_purge": {
      const parts = [
        [item.detail && item.detail.audit_logs, "activity log entr", "y", "ies"],
        [item.detail && item.detail.connection_logs, "connection log", "", "s"],
        [item.detail && item.detail.file_transfer_logs, "file transfer log", "", "s"],
        [item.detail && item.detail.alarm_logs, "alarm log", "", "s"],
      ]
        .filter(([n]) => Number.isInteger(n) && n > 0)
        .map(([n, word, one, many]) => `${n} ${word}${n === 1 ? one : many}`);
      return `Retention policy removed ${parts.join(", ") || "old logs"}`;
    }
    case "address_book_created":
      return `${prefix}created the shared address book "${safe.book}"`;
    case "address_book_updated":
      return `${prefix}updated the shared address book "${safe.book}"`;
    case "address_book_deleted":
      return `${prefix}deleted the shared address book "${safe.book}"`;
    case "address_book_shared":
      return `${prefix}shared the address book "${safe.book}" with "${safe.username}" (${escapeHtml(addressBookRuleName(item.detail && item.detail.rule)).toLowerCase()})`;
    case "address_book_unshared":
      return `${prefix}stopped sharing the address book "${safe.book}" with "${safe.username}"`;
    case "address_book_entry_created":
      return `${prefix}added address book entry <span class="whitespace-nowrap">${safe.alias ? `"${safe.alias}" (${safe.rustdesk_id})` : safe.rustdesk_id}</span>`;
    case "address_book_entry_updated":
      return `${prefix}updated address book entry <span class="whitespace-nowrap">${safe.alias ? `"${safe.alias}" (${safe.rustdesk_id})` : safe.rustdesk_id}</span>`;
    case "strategy_created":
      return `${prefix}created strategy "${safe.name}"`;
    case "strategy_updated":
      return `${prefix}updated strategy "${safe.name}"`;
    case "strategy_deleted":
      return `${prefix}deleted strategy "${safe.name}"`;
    case "strategy_assigned": {
      const target = item.target_type === "group" ? `group "${safe.name}"` : `device ${safe.rustdesk_id}`;
      return item.detail && item.detail.strategy
        ? `${prefix}assigned strategy "${safe.strategy}" to ${target}`
        : `${prefix}removed the strategy from ${target}`;
    }
    case "connection_disconnect_requested": {
      const ids = Array.isArray(item.detail && item.detail.connection_ids) ? item.detail.connection_ids.length : 0;
      return `${prefix}ended ${ids} connection${ids === 1 ? "" : "s"} on device <span class="whitespace-nowrap">${safe.rustdesk_id}</span>`;
    }
    case "device_assigned":
      return `${prefix}assigned device <span class="whitespace-nowrap">${safe.rustdesk_id}</span> with <code>rustdesk --assign</code>`;
    case "device_preset_applied":
      return `Device <span class="whitespace-nowrap">${safe.rustdesk_id}</span> was placed by its preset options`;
    case "enrollment_token_created":
      return `${prefix}created the enrollment token "${safe.label}"`;
    case "enrollment_token_revoked":
      return `${prefix}revoked the enrollment token "${safe.label}"`;
    case "two_factor_enabled":
      return `${prefix}turned on two-factor authentication`;
    case "two_factor_disabled":
      return `${prefix}turned off two-factor authentication`;
    case "two_factor_reset":
      return `${prefix}reset two-factor authentication for "${safe.username}"`;
    case "recovery_codes_regenerated":
      return `${prefix}generated new recovery codes`;
    case "two_factor_check":
      return item.result === "success"
        ? `${prefix}confirmed a second factor`
        : `${prefix}failed a password or code check for a security change`;
    case "account_locked":
      return `Account "${safe.username}" was locked after ${safe.failed_logins} failed sign-ins`;
    case "user_unlocked":
      return `${prefix}unlocked the account "${safe.username}"`;
    case "user_registered":
      return item.detail && item.detail.pending_approval
        ? `"${safe.username}" registered and is waiting for approval`
        : `"${safe.username}" registered an account`;
    case "password_reset_issued":
      return `${prefix}issued a password-reset link for "${safe.username}"`;
    case "password_reset_completed":
      return item.result === "success"
        ? `${prefix}set a new password with a reset link`
        : "A password-reset link was refused (invalid, used or expired)";
    case "session_revoked":
      return `${prefix}signed out one of their sessions`;
    case "password_changed":
      return item.result === "success"
        ? `${prefix}changed their password`
        : `${prefix}entered a wrong current password while changing it`;
    case "sessions_revoked":
      return `${prefix}signed out ${safe.count} other session${item.detail && item.detail.count === 1 ? "" : "s"}`;
    case "api_key_created":
      return `${prefix}created the API key "${safe.label}" (${safe.scope})`;
    case "api_key_revoked":
      return `${prefix}revoked the API key "${safe.label}"`;
    case "device_uuid_change_requested":
      return `A different install tried to take over device <span class="whitespace-nowrap">${safe.rustdesk_id}</span> (waiting for a decision)`;
    case "device_uuid_accepted":
      return `${prefix}accepted the new install of device <span class="whitespace-nowrap">${safe.rustdesk_id}</span>`;
    case "device_uuid_rejected":
      return `${prefix}rejected the new install of device <span class="whitespace-nowrap">${safe.rustdesk_id}</span>`;
    case "data_exported":
      return `${prefix}exported ${safe.rows} ${safe.what} row${item.detail && item.detail.rows === 1 ? "" : "s"}`;
    case "devices_imported":
      return `${prefix}imported devices (${safe.created} created, ${safe.updated} updated)`;
    case "address_book_entry_deleted":
      return `${prefix}deleted address book entry <span class="whitespace-nowrap">${safe.alias ? `"${safe.alias}" (${safe.rustdesk_id})` : safe.rustdesk_id}</span>`;
    default:
      return `${prefix}${escapeHtml(item.action.replace(/_/g, " "))}${item.target_type ? ` (${escapeHtml(item.target_type)} #${escapeHtml(item.target_id)})` : ""}`;
  }
}

// The client's share rules (ShareRule in hbbs.dart): 1 read, 2 read/write, 3 full control.
const ADDRESS_BOOK_RULES = { 1: "Read only", 2: "Read / write", 3: "Full control" };
function addressBookRuleName(rule) {
  return ADDRESS_BOOK_RULES[rule] || "Unknown access";
}

// Live device-status updates (CLAUDE.md section 47). Server-side auth is
// cookie-based (see api/ws.py) - nothing extra to attach here. Reconnects
// on transient drops; a 1008 close means "not authenticated" (e.g. logged
// out in another tab), so it does not retry that case.
function connectDeviceWs(onMessage) {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${window.location.host}/api/v1/ws/devices`);
  ws.addEventListener("message", (evt) => {
    try {
      onMessage(JSON.parse(evt.data));
    } catch {
      // ignore malformed frames
    }
  });
  ws.addEventListener("close", (evt) => {
    if (evt.code === 1008) return;
    setTimeout(() => connectDeviceWs(onMessage), 3000);
  });
  return ws;
}

// Fixed swatch colors for the accent picker (must match ACCENTS in tailwind.config.js).
// Only static values are interpolated below, never user data.
const ACCENT_SWATCHES = {
  blue: "#2563eb",
  indigo: "#4f46e5",
  violet: "#7c3aed",
  emerald: "#059669",
  rose: "#e11d48",
  amber: "#d97706",
};

// "Appearance" dropdown: light / dark / follow-system, plus the accent color.
// The state itself lives in theme.js (loaded in <head> to avoid a flash).
function renderThemeMenu() {
  const host = document.getElementById("theme-menu");
  if (!host || !window.appTheme) return;

  host.innerHTML = `<button id="theme-toggle" type="button" aria-haspopup="true" aria-expanded="false"
      class="rounded-md px-2 py-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-900">Appearance</button>
    <div id="theme-panel" class="hidden absolute right-0 mt-2 w-60 card p-3 shadow-lg z-40"></div>`;
  const toggle = document.getElementById("theme-toggle");
  const panel = document.getElementById("theme-panel");

  function paint() {
    const current = window.appTheme.get();
    const themeButtons = window.appTheme.THEMES.map(
      (t) => `<button type="button" data-theme-choice="${t}" aria-pressed="${t === current.theme}"
        class="flex-1 rounded-md px-2 py-1 text-xs font-medium capitalize ${
          t === current.theme ? "bg-brand-600 text-white" : "text-slate-600 hover:bg-slate-100"
        }">${t}</button>`
    ).join("");
    const swatches = window.appTheme.ACCENTS.map(
      (a) => `<button type="button" data-accent-choice="${a}" aria-label="${a} accent" aria-pressed="${a === current.accent}"
        title="${a}" style="background:${ACCENT_SWATCHES[a]}"
        class="h-6 w-6 rounded-full ring-offset-2 ring-offset-surface ${a === current.accent ? "ring-2 ring-slate-900" : ""}"></button>`
    ).join("");
    panel.innerHTML = `<p class="text-xs font-semibold uppercase tracking-wide text-slate-500 mb-2">Theme</p>
      <div class="flex gap-1 mb-4">${themeButtons}</div>
      <p class="text-xs font-semibold uppercase tracking-wide text-slate-500 mb-2">Accent color</p>
      <div class="flex gap-2 px-1 py-1">${swatches}</div>`;
  }

  function setOpen(open) {
    panel.classList.toggle("hidden", !open);
    toggle.setAttribute("aria-expanded", String(open));
    if (open) paint();
  }

  toggle.addEventListener("click", () => setOpen(panel.classList.contains("hidden")));
  panel.addEventListener("click", (evt) => {
    const target = evt.target.closest("button");
    if (!target) return;
    if (target.dataset.themeChoice) window.appTheme.setTheme(target.dataset.themeChoice);
    if (target.dataset.accentChoice) window.appTheme.setAccent(target.dataset.accentChoice);
    paint();
  });
  document.addEventListener("click", (evt) => {
    if (!host.contains(evt.target)) setOpen(false);
  });
  document.addEventListener("keydown", (evt) => {
    if (evt.key === "Escape") setOpen(false);
  });
}

function renderNav(active, user) {
  const nav = document.getElementById("app-nav");
  if (!nav) return;
  const items = [
    { key: "dashboard", href: "/dashboard", label: "Dashboard" },
    { key: "devices", href: "/devices", label: "Devices" },
    { key: "groups", href: "/groups", label: "Groups" },
    { key: "tags", href: "/tags", label: "Tags" },
    { key: "address-book", href: "/address-book", label: "Address Book" },
    { key: "logs", href: "/logs", label: "Logs" },
  ];
  if (user && user.is_admin) {
    items.push({ key: "strategies", href: "/strategies", label: "Strategies" });
    items.push({ key: "users", href: "/users", label: "Users" });
  }
  items.push({ key: "connect", href: "/connect", label: "Connect" });
  if (user && user.is_admin) items.push({ key: "settings", href: "/settings", label: "Settings" });
  items.push({ key: "security", href: "/security", label: "Security" });
  nav.innerHTML = items
    .map(
      (item) =>
        `<a href="${item.href}" class="px-2.5 py-2 rounded-md text-sm font-medium whitespace-nowrap ${
          item.key === active ? "bg-brand-600 text-white" : "text-slate-600 hover:bg-slate-100"
        }">${item.label}</a>`
    )
    .join("");

  renderThemeMenu();

  const userLabel = document.getElementById("current-user-label");
  if (userLabel && user) {
    userLabel.textContent = user.username + (user.is_admin ? " (admin)" : "");
    // Administrators land on their own entry in Users; everyone else on their account page.
    if (user.is_admin && user.id != null) {
      userLabel.href = `/users?id=${encodeURIComponent(user.id)}`;
      userLabel.title = "Open your user record";
    } else {
      userLabel.title = "Your account and password";
    }
  }

  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", async () => {
      await api("/api/v1/auth/logout", { method: "POST" });
      window.location.href = "/login";
    });
  }
}
