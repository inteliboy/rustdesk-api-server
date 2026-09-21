const urlParams = new URLSearchParams(window.location.search);
const TABS = ["activity", "conn", "file", "alarm"];
const state = {
  // Chosen once the person is known: Activity is for administrators.
  tab: TABS.includes(urlParams.get("tab")) ? urlParams.get("tab") : null,
  page: 1,
  pageSize: 25,
  deviceId: urlParams.get("device_id") || "",
  total: 0,
};
let isAdmin = false;

// Enum labels below come from the RustDesk client source (connection.rs).
const CONN_TYPES = { 0: "Remote control", 1: "File transfer", 2: "Port forward", 3: "View camera", 4: "Terminal" };
const PRIMARY_AUTH = { 0: "-", 1: "Click (accepted)", 2: "Temporary password", 3: "Permanent password", 4: "Switch sides" };
const TWO_FACTOR = { 0: "", 1: " + 2FA (TOTP)", 2: " + trusted device" };
// FileAuditType in connection.rs: RemoteSend = 0, RemoteReceive = 1, named from
// the controlled device's side. Receive = the controller uploaded to it (the
// path is the destination on the device); Send = the controller downloaded
// from it (the path is the source on the device).
const FILE_DIRECTIONS = { 0: "Sent to peer", 1: "Received from peer" };
// AlarmAuditType in connection.rs (values 3-5 are commented out there, and 10
// exists only in newer clients). Named from the enum, not from client UI text.
const ALARM_TYPES = {
  0: "IP not on whitelist",
  1: "Too many failed attempts (over 30)",
  2: "Too many failed attempts (over 6 in a minute)",
  6: "Too many failed attempts from an IPv6 prefix",
  7: "Terminal OS login backoff",
  8: "Terminal OS login concurrency limit",
  9: "Session scope violation",
  10: "ID not on whitelist",
};

const RESOURCES = { activity: "admin/audit-logs", conn: "connection-logs", file: "file-logs", alarm: "alarm-logs" };
const resource = () => RESOURCES[state.tab];
const th = (label) => `<th class="px-4 py-2 font-medium">${label}</th>`;
const td = (html, cls = "") => `<td class="px-4 py-3 ${cls}">${html}</td>`;
const deleteCell = (item) =>
  isAdmin
    ? td(`<button data-id="${item.id}" class="delete-log text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Delete</button>`, "text-right")
    : "";

// The device name opens it in Devices; its RustDesk ID starts a connection.
function deviceCell(item) {
  const name = item.device_label || (item.device_id ? "Device" : "");
  const label = !name
    ? ""
    : item.device_id
      ? `<a href="/devices/${encodeURIComponent(item.device_id)}" class="text-link hover:underline whitespace-nowrap">${escapeHtml(name)}</a> `
      : `<span class="whitespace-nowrap">${escapeHtml(name)}</span> `;
  return `${label}${connectLink(item.rustdesk_id)}`;
}

function peerCell(item) {
  if (!item.peer_id && !item.peer_name) return "-";
  const name = item.peer_name ? `<span class="whitespace-nowrap">${escapeHtml(item.peer_name)}</span> ` : "";
  return `${name}${item.peer_id ? connectLink(item.peer_id) : ""}`;
}

// What the controlling user typed when the client asked for a note at the end
// of the session.
function noteLine(item) {
  return item.note
    ? `<div class="text-xs text-slate-500 break-words max-w-xs">Note: ${escapeHtml(item.note)}</div>`
    : "";
}

function durationText(start, end) {
  if (!end) return "-";
  const secs = Math.max(0, Math.round((parseServerDate(end) - parseServerDate(start)) / 1000));
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

// The activity log (the audit log): who did what in the WebUI and the management API.
function activityRow(a) {
  return `<tr>
    ${td(fmtDate(a.created_at), "text-slate-500 whitespace-nowrap")}
    ${td(describeActivity(a), a.result !== "success" ? "text-red-600 dark:text-red-400" : "")}
    ${td(a.ip_address ? ipLabel(a.ip_address) : "-", "font-mono whitespace-nowrap")}
  </tr>`;
}

function connRow(c) {
  const type = c.conn_type == null ? "-" : CONN_TYPES[c.conn_type] || `Type ${c.conn_type}`;
  const auth = c.primary_auth == null ? "-" : (PRIMARY_AUTH[c.primary_auth] || `Auth ${c.primary_auth}`) + (TWO_FACTOR[c.two_factor] || "");
  return `<tr>
    ${td(fmtDate(c.started_at), "text-slate-500 whitespace-nowrap")}
    ${td(deviceCell(c))}
    ${td(peerCell(c) + noteLine(c))}
    ${td(escapeHtml(type))}
    ${td(escapeHtml(auth))}
    ${td(c.from_ip ? ipLabel(c.from_ip) : "-", "font-mono whitespace-nowrap")}
    ${td(c.ended_at ? escapeHtml(durationText(c.started_at, c.ended_at)) : '<span class="text-slate-400">no close recorded</span>', "whitespace-nowrap")}
    ${deleteCell(c)}
  </tr>`;
}

// The one path the client reports is on the controlled device: the source
// when it sent files, the destination when it received them. The other end
// is the controlling peer.
function fileRow(f) {
  const count = f.num ?? f.files.length;
  const names = f.files.slice(0, 3).map((x) => escapeHtml(x[0] || "(folder)")).join(", ");
  const more = f.files.length > 3 ? ` +${f.files.length - 3} more` : "";
  const what = f.is_file
    ? "(single file)"
    : `${count} item${count === 1 ? "" : "s"}${names ? ": " + names + more : ""}`;
  const path = f.path
    ? `<div class="font-mono text-xs text-slate-500 break-all">${escapeHtml(f.path)}</div>`
    : `<div class="text-xs text-slate-400">no path reported (clipboard copy/paste)</div>`;
  const onDevice = `<div>${deviceCell(f)}</div>${path}`;
  const peer = peerCell(f);
  const direction = f.audit_type == null ? "-" : FILE_DIRECTIONS[f.audit_type] || `Type ${escapeHtml(f.audit_type)}`;
  let from, to;
  if (f.audit_type === 0) [from, to] = [onDevice, peer];
  else if (f.audit_type === 1) [from, to] = [peer, onDevice];
  else [from, to] = ['<span class="text-slate-400">unknown direction</span>', `${onDevice}<div>${peer}</div>`];
  return `<tr>
    ${td(fmtDate(f.logged_at), "text-slate-500 whitespace-nowrap")}
    ${td(direction, "whitespace-nowrap")}
    ${td(from)}
    ${td(to)}
    ${td(what)}
    ${td(f.from_ip ? ipLabel(f.from_ip) : "-", "font-mono whitespace-nowrap")}
    ${deleteCell(f)}
  </tr>`;
}

function alarmRow(a) {
  const label = a.alarm_type == null ? "-" : ALARM_TYPES[a.alarm_type] || `Type ${a.alarm_type}`;
  const details = [a.conn_type, a.message].filter(Boolean).map((x) => escapeHtml(x)).join(" - ");
  return `<tr>
    ${td(fmtDate(a.logged_at), "text-slate-500 whitespace-nowrap")}
    ${td(escapeHtml(label))}
    ${td(deviceCell(a))}
    ${td(peerCell(a))}
    ${td(a.from_ip ? ipLabel(a.from_ip) : "-", "font-mono whitespace-nowrap")}
    ${td(details || "-")}
    ${deleteCell(a)}
  </tr>`;
}

function setTabStyles() {
  for (const [key, id] of [["activity", "tab-activity"], ["conn", "tab-conn"], ["file", "tab-file"], ["alarm", "tab-alarm"]]) {
    document.getElementById(id).className =
      "px-3 py-1.5 rounded-md text-sm font-medium " +
      (state.tab === key ? "bg-brand-600 text-white" : "text-slate-600 hover:bg-slate-100");
  }
  // The activity log is the audit log: administrators only (the server enforces it too).
  document.getElementById("tab-activity").classList.toggle("hidden", !isAdmin);
  document.getElementById("activity-note").classList.toggle("hidden", state.tab !== "activity");
  // The client-reported logs have their own introduction; the activity log is not reported by clients.
  document.getElementById("intro").classList.toggle("hidden", state.tab === "activity");
  document.getElementById("file-note").classList.toggle("hidden", state.tab !== "file");
  document.getElementById("alarm-note").classList.toggle("hidden", state.tab !== "alarm");
}

const EMPTY_TEXT = {
  activity: "No activity recorded yet.",
  conn: "No connections recorded yet.",
  file: "No file transfers recorded yet.",
  alarm: "No alarms recorded yet.",
};
const ROW_RENDERERS = { activity: activityRow, conn: connRow, file: fileRow, alarm: alarmRow };

async function load() {
  setTabStyles();
  const COLUMNS = {
    activity: [th("Time"), th("Activity"), th("From IP")],
    conn: [th("Started"), th("Device (controlled)"), th("Controlled by"), th("Type"), th("Authorization"), th("From IP"), th("Duration")],
    file: [th("Time"), th("Direction"), th("From"), th("To"), th("Files"), th("IP")],
    alarm: [th("Time"), th("Alarm"), th("Device (controlled)"), th("Refused peer"), th("From IP"), th("Details")],
  };
  const columns = COLUMNS[state.tab];
  // Client-reported logs can be deleted by an administrator; the activity log cannot.
  const canDelete = isAdmin && state.tab !== "activity";
  document.getElementById("log-head").innerHTML = "<tr>" + columns.join("") + (canDelete ? th("") : "") + "</tr>";

  const params = new URLSearchParams({ page: state.page, page_size: state.pageSize });
  if (state.deviceId && state.tab !== "activity") params.set("device_id", state.deviceId);
  const data = await api(`/api/v1/${resource()}?` + params.toString());
  state.total = data.total;

  // Deleting the last row of the last page leaves that page empty.
  const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  if (data.items.length === 0 && state.page > 1) {
    state.page = totalPages;
    return load();
  }

  const rowsEl = document.getElementById("log-rows");
  const emptyEl = document.getElementById("empty-state");
  if (data.items.length === 0) {
    rowsEl.innerHTML = "";
    emptyEl.textContent = EMPTY_TEXT[state.tab];
    emptyEl.classList.remove("hidden");
  } else {
    emptyEl.classList.add("hidden");
    rowsEl.innerHTML = data.items.map(ROW_RENDERERS[state.tab]).join("");
  }
  document.getElementById("page-info").textContent = `Page ${data.page} of ${totalPages} (${data.total} total)`;
  document.getElementById("prev-page").disabled = data.page <= 1;
  document.getElementById("next-page").disabled = data.page >= totalPages;

  const clearBtn = document.getElementById("clear-btn");
  clearBtn.classList.toggle("hidden", !canDelete);
  clearBtn.textContent = state.deviceId ? "Clear this device's logs" : "Clear all";
  clearBtn.disabled = data.total === 0;
  clearBtn.classList.toggle("opacity-50", data.total === 0);
}

function switchTab(tab) {
  state.tab = tab;
  state.page = 1;
  load().catch((err) => toast(err.message, "error"));
}

async function deleteOne(id) {
  if (!confirm(t("Delete this log entry? This cannot be undone."))) return;
  try {
    await api(`/api/v1/${resource()}/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("Log entry deleted.", "success");
    await load();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function clearAll() {
  const what = { conn: "connection", file: "file transfer", alarm: "alarm" }[state.tab];
  const scope = state.deviceId ? " for this device" : "";
  if (!confirm(`Permanently delete ${state.total} ${what} log${state.total === 1 ? "" : "s"}${scope}? This cannot be undone.`)) return;
  try {
    const query = state.deviceId ? `?device_id=${encodeURIComponent(state.deviceId)}` : "";
    const result = await api(`/api/v1/${resource()}${query}`, { method: "DELETE" });
    toast(`Deleted ${result.deleted} log${result.deleted === 1 ? "" : "s"}.`, "success");
    state.page = 1;
    await load();
  } catch (err) {
    toast(err.message, "error");
  }
}

(async () => {
  const user = await requireAuth();
  if (!user) return;
  isAdmin = !!user.is_admin;
  // Activity leads for administrators; a link to one device's logs (device_id) opens on Connections.
  if (!isAdmin && state.tab === "activity") state.tab = null;
  if (!state.tab) state.tab = isAdmin && !state.deviceId ? "activity" : "conn";
  renderNav("logs", user);
  if (state.deviceId) {
    document.getElementById("device-filter-note").innerHTML =
      `Filtered to one device. <a href="/logs?tab=${state.tab}" class="text-link underline">Show all</a>`;
  }
  try {
    await load();
  } catch (err) {
    document.getElementById("empty-state").textContent = err.message;
    document.getElementById("empty-state").classList.remove("hidden");
  }
})();

document.getElementById("tab-activity").addEventListener("click", () => switchTab("activity"));
document.getElementById("tab-conn").addEventListener("click", () => switchTab("conn"));
document.getElementById("tab-file").addEventListener("click", () => switchTab("file"));
document.getElementById("tab-alarm").addEventListener("click", () => switchTab("alarm"));
document.getElementById("refresh-btn").addEventListener("click", () => load().catch((e) => toast(e.message, "error")));
document.getElementById("clear-btn").addEventListener("click", clearAll);
document.getElementById("log-rows").addEventListener("click", (evt) => {
  const btn = evt.target.closest(".delete-log");
  if (btn) deleteOne(btn.dataset.id);
});
document.getElementById("prev-page").addEventListener("click", () => {
  if (state.page > 1) { state.page -= 1; load(); }
});
document.getElementById("next-page").addEventListener("click", () => { state.page += 1; load(); });
