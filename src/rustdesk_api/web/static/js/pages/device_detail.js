const deviceId = Number(document.getElementById("device-page").dataset.deviceId);

function row(label, value) {
  return `<div class="flex justify-between items-center py-2 border-b border-slate-100 last:border-0 text-sm">
    <span class="text-slate-500">${label}</span>
    <span class="font-medium">${value ?? "-"}</span>
  </div>`;
}

async function renderSharing() {
  const container = document.getElementById("sharing-section");
  let shares;
  try {
    shares = await api(`/api/v1/devices/${deviceId}/shares`);
  } catch (err) {
    // 404 here means the caller isn't the owner/admin, not that
    // something is broken - just hide the section.
    container.innerHTML = "";
    return;
  }

  container.innerHTML = `
    <h2 class="text-sm font-semibold text-slate-500 uppercase tracking-wide mb-3">Sharing</h2>
    <div class="card p-4 mb-4">
      <table class="w-full text-sm" id="share-table">
        <tbody></tbody>
      </table>
      <p id="no-shares" class="text-sm text-slate-500 hidden">Not shared with anyone yet.</p>
    </div>
    <div class="card p-4 flex flex-wrap items-end gap-3">
      <div>
        <label class="block text-xs text-slate-500 mb-1">Username</label>
        <input id="share-username" class="border border-slate-300 rounded px-2 py-1.5 text-sm" />
      </div>
      <div>
        <label class="block text-xs text-slate-500 mb-1">Permission</label>
        <select id="share-permission" class="border border-slate-300 rounded px-2 py-1.5 text-sm">
          <option value="view">View</option>
          <option value="control">Control</option>
        </select>
      </div>
      <button id="share-add" class="px-4 py-2 rounded-md bg-brand-600 text-white text-sm font-medium hover:bg-brand-700">Share</button>
    </div>
  `;

  const tbody = document.querySelector("#share-table tbody");
  const noShares = document.getElementById("no-shares");
  if (shares.length === 0) {
    noShares.classList.remove("hidden");
  } else {
    noShares.classList.add("hidden");
    tbody.innerHTML = shares
      .map(
        (s) => `<tr class="border-b border-slate-100 last:border-0">
          <td class="py-2">${userLink(s.shared_with_user_id, s.shared_with_username)}</td>
          <td class="py-2 text-slate-500">${escapeHtml(s.permission)}</td>
          <td class="py-2 text-slate-500 whitespace-nowrap">${s.expires_at ? "expires " + fmtDate(s.expires_at) : "no expiry"}</td>
          <td class="py-2 text-right"><button data-id="${s.id}" class="revoke-share text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Revoke</button></td>
        </tr>`
      )
      .join("");
    document.querySelectorAll(".revoke-share").forEach((btn) =>
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/v1/devices/${deviceId}/shares/${btn.dataset.id}`, { method: "DELETE" });
          toast("Share revoked.", "success");
          renderSharing();
        } catch (err) {
          toast(err.message, "error");
        }
      })
    );
  }

  document.getElementById("share-add").addEventListener("click", async () => {
    const username = document.getElementById("share-username").value;
    const permission = document.getElementById("share-permission").value;
    if (!username) {
      toast("Username is required.", "error");
      return;
    }
    try {
      await api(`/api/v1/devices/${deviceId}/shares`, {
        method: "POST",
        body: JSON.stringify({ username, permission }),
      });
      toast("Device shared.", "success");
      renderSharing();
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

// The incoming connections the client reported at its last heartbeat. Ending
// one is asked of the server, which passes it on with the client's next
// heartbeat (a few seconds while a connection is open), so it is not instant.
let connectionTimer = null;

async function renderConnections(canManage) {
  const container = document.getElementById("connections-section");
  if (!container) return;
  let connections;
  try {
    connections = await api(`/api/v1/devices/${deviceId}/connections`);
  } catch (err) {
    container.innerHTML = "";
    return;
  }
  if (connections.length === 0) {
    container.innerHTML = `<h2 class="text-sm font-semibold text-slate-500 uppercase tracking-wide mb-3">Active connections</h2>
      <div class="card p-4 text-sm text-slate-500">Nobody is connected right now.</div>`;
    return;
  }
  const rows = connections
    .map((c) => {
      const who = c.peer_id ? `${escapeHtml(c.peer_name || "")} (${escapeHtml(c.peer_id)})` : "unknown";
      const action = c.disconnect_requested
        ? `<span class="text-slate-500">ending&hellip;</span>`
        : canManage
          ? `<button data-conn="${Number(c.id)}" class="end-connection text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Disconnect</button>`
          : "";
      return `<tr class="border-b border-slate-100 last:border-0">
        <td class="py-2">${who}</td>
        <td class="py-2 text-slate-500">${c.from_ip ? ipLabel(c.from_ip) : "-"}</td>
        <td class="py-2 text-slate-500 whitespace-nowrap">${c.started_at ? fmtDate(c.started_at) : "-"}</td>
        <td class="py-2 text-right">${action}</td>
      </tr>`;
    })
    .join("");
  const all = canManage && connections.length > 1
    ? `<button id="end-all-connections" class="mt-3 px-3 py-1.5 rounded-md border border-red-300 dark:border-red-800 text-red-600 dark:text-red-400 text-sm hover:bg-red-50 dark:hover:bg-red-950/40">Disconnect all</button>`
    : "";
  container.innerHTML = `<h2 class="text-sm font-semibold text-slate-500 uppercase tracking-wide mb-3">Active connections</h2>
    <div class="card p-4">
      <table class="w-full text-sm">
        <thead class="text-left text-slate-500"><tr>
          <th class="py-1 font-medium">Connected from</th><th class="py-1 font-medium">Address</th>
          <th class="py-1 font-medium">Since</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      ${all}
    </div>`;

  const end = async (ids) => {
    try {
      await api(`/api/v1/devices/${deviceId}/disconnect`, {
        method: "POST",
        body: JSON.stringify({ connection_ids: ids }),
      });
      toast("The client will end it at its next check-in.", "success");
      renderConnections(canManage);
    } catch (err) {
      toast(err.message, "error");
    }
  };
  container.querySelectorAll(".end-connection").forEach((btn) =>
    btn.addEventListener("click", () => {
      if (confirm(t("Disconnect this connection?"))) end([parseInt(btn.dataset.conn, 10)]);
    })
  );
  const allBtn = document.getElementById("end-all-connections");
  if (allBtn) allBtn.addEventListener("click", () => confirm(t("Disconnect everyone?")) && end(null));
}

// What happened to this device: it came online, connections, edits and other
// changes, and attempts by another install to take its id. Newest first; "Show
// older" pages backwards.
const TIMELINE_PAGE = 25;
let timelineEntries = [];
let timelineDone = false;

function timelineText(e) {
  const detail = e.detail || {};
  if (e.source === "audit") {
    return describeActivity(
      { action: e.kind, actor_username: e.actor, actor_id: null, result: e.result, detail, target_type: "device" },
      { includeActor: true }
    );
  }
  if (e.source === "connection") {
    const who = detail.peer_id ? `${escapeHtml(detail.peer_name || "")} (${escapeHtml(detail.peer_id)})` : "Someone";
    const from = detail.from_ip ? ` from ${ipLabel(detail.from_ip)}` : "";
    const ended = detail.ended_at ? ` &middot; ended ${escapeHtml(fmtDate(detail.ended_at))}` : " &middot; still open or not reported closed";
    const note = detail.note
      ? `<div class="text-xs text-slate-500 break-words">Note: ${escapeHtml(detail.note)}</div>`
      : "";
    return `${who} connected${from}${ended}${note}`;
  }
  const ip = detail.ip ? ` from ${ipLabel(detail.ip)}` : "";
  switch (e.kind) {
    case "registered":
      return detail.pending ? `Device registered${ip} - waiting for approval` : `Device registered${ip}`;
    case "approved":
      return "Approved by an administrator";
    case "rejected":
      return "Rejected by an administrator";
    case "online":
      return detail.offline_since
        ? `Came back online (silent since ${escapeHtml(fmtDate(detail.offline_since))})`
        : "Came back online";
    case "imported":
      return "Added by an import";
    case "uuid_change_requested":
      return `A different install uploaded this device's details${ip} - waiting for a decision`;
    case "uuid_accepted":
      return `The new install was accepted${ip}`;
    case "uuid_rejected":
      return `The new install was rejected${ip}`;
    case "uuid_changed":
      return "The device was re-bound to a new install (signed in as its owner or an administrator)";
    default:
      return escapeHtml(String(e.kind).replace(/_/g, " "));
  }
}

function drawTimeline() {
  const container = document.getElementById("timeline-section");
  if (!container) return;
  const rows = timelineEntries
    .map(
      (e) => `<li class="py-2 border-b border-slate-100 last:border-0 flex justify-between gap-4 text-sm">
        <span class="${e.result === "failure" ? "text-red-600 dark:text-red-400" : ""}">${timelineText(e)}</span>
        <span class="text-slate-500 whitespace-nowrap">${escapeHtml(fmtDate(e.at))}</span>
      </li>`
    )
    .join("");
  container.innerHTML = `<h2 class="text-sm font-semibold text-slate-500 uppercase tracking-wide mb-3">Timeline</h2>
    <div class="card p-4">
      ${rows ? `<ul>${rows}</ul>` : `<p class="text-sm text-slate-500">Nothing recorded yet.</p>`}
      ${timelineDone ? "" : `<button id="timeline-more" class="mt-3 px-3 py-1.5 rounded-md border border-slate-300 text-sm hover:bg-slate-100">Show older</button>`}
    </div>`;
  const more = document.getElementById("timeline-more");
  if (more) more.addEventListener("click", () => loadTimeline(false));
}

async function loadTimeline(reset) {
  if (reset) {
    timelineEntries = [];
    timelineDone = false;
  }
  const params = new URLSearchParams({ limit: TIMELINE_PAGE });
  if (timelineEntries.length) params.set("before", timelineEntries[timelineEntries.length - 1].at);
  try {
    const page = await api(`/api/v1/devices/${deviceId}/timeline?` + params.toString());
    timelineEntries = timelineEntries.concat(page);
    timelineDone = page.length < TIMELINE_PAGE;
  } catch (err) {
    document.getElementById("timeline-section").innerHTML = "";
    return;
  }
  drawTimeline();
}

// A different install than the one on record uploaded this device's details. It
// is parked, not trusted, until the owner or an administrator decides.
function uuidBanner(d, canManage) {
  if (!d.uuid_change_pending) return "";
  const from = d.uuid_change_ip ? ` from ${ipLabel(d.uuid_change_ip)}` : "";
  const when = d.uuid_change_at ? ` on ${escapeHtml(fmtDate(d.uuid_change_at))}` : "";
  const buttons = canManage
    ? `<div class="flex gap-3 mt-3">
         <button id="uuid-accept" class="px-3 py-1.5 rounded-md bg-brand-600 text-white text-sm font-medium hover:bg-brand-700">Accept the new install</button>
         <button id="uuid-reject" class="px-3 py-1.5 rounded-md border border-slate-300 text-sm font-medium hover:bg-slate-100">Reject</button>
       </div>`
    : "";
  return `<div class="card p-4 border-amber-300 dark:border-amber-700">
    <p class="text-sm font-medium">A different install is claiming this device.</p>
    <p class="text-sm text-slate-500 mt-1">Something${from}${when} sent this device's ID with a different identity than the one on record.
      Until it is accepted, its heartbeats are ignored and none of its details are used. Accept it if you reinstalled
      or replaced this machine; reject it if you did not.</p>
    ${buttons}
  </div>`;
}

// A new device is recorded but not used until an administrator says so (NEW_DEVICE_POLICY).
function approvalBanner(d) {
  if (d.approval === "approved" || !(currentUser && currentUser.is_admin)) return "";
  const rejected = d.approval === "rejected";
  const text = rejected
    ? "You rejected this device. Nothing it sends is used; approve it to manage it after all, or delete it."
    : "This device is new and waiting for your approval. It is given no policy and cannot be opened in the browser until you approve it. Check that the ID, host name and address are ones you recognise.";
  return `<div class="card p-4 border-amber-300 dark:border-amber-700">
    <p class="text-sm font-medium">${rejected ? "This device was rejected." : "This device is waiting for approval."}</p>
    <p class="text-sm text-slate-500 mt-1">${text}</p>
    <div class="flex gap-3 mt-3">
      <button id="approve-btn" class="px-3 py-1.5 rounded-md bg-brand-600 text-white text-sm font-medium hover:bg-brand-700">Approve</button>
      ${rejected ? "" : `<button id="reject-btn" class="px-3 py-1.5 rounded-md border border-slate-300 text-sm font-medium hover:bg-slate-100">Reject</button>`}
    </div>
  </div>`;
}

async function render() {
  const [d, groups, tags, strategies] = await Promise.all([
    api(`/api/v1/devices/${deviceId}`),
    api("/api/v1/groups"),
    api("/api/v1/tags"),
    currentUser && currentUser.is_admin ? api("/api/v1/strategies") : Promise.resolve([]),
  ]);
  const canManage = currentUser && (currentUser.is_admin || d.owner_id === currentUser.id);
  const statusBadge = d.approval === "pending"
    ? `<span class="badge" style="background:#d9770622;color:#d97706"><span class="badge-dot" style="background:#d97706"></span>Pending</span>`
    : d.approval === "rejected"
    ? `<span class="badge badge-offline"><span class="badge-dot"></span>Rejected</span>`
    : d.online
    ? `<span class="badge badge-online"><span class="badge-dot"></span>Online</span>`
    : `<span class="badge badge-offline"><span class="badge-dot"></span>Offline</span>`;

  const groupOptions =
    `<option value="">No group</option>` +
    groups.map((g) => `<option value="${g.id}" ${g.id === d.group_id ? "selected" : ""}>${escapeHtml(g.name)}</option>`).join("");

  // Only an administrator can set a strategy; everyone else just sees which
  // one applies (its own, else the group's).
  const strategyOptions =
    `<option value="">None (use the group's, else the default)</option>` +
    strategies.map((st) => `<option value="${st.id}" ${st.id === d.strategy_id ? "selected" : ""}>${escapeHtml(st.name)}</option>`).join("");
  const strategyCell =
    currentUser && currentUser.is_admin
      ? `<span class="inline-flex flex-col items-end gap-1">
           <select id="strategy-select" class="border border-slate-300 rounded px-2 py-1 text-sm">${strategyOptions}</select>
           <span class="text-xs text-slate-500">Applies: ${escapeHtml(d.effective_strategy_name || "none")}</span>
         </span>`
      : escapeHtml(d.effective_strategy_name || "-");

  const deviceTagIds = new Set((d.tags || []).map((t) => t.id));
  const tagCheckboxes = tags
    .map(
      (t) => `<label class="inline-flex items-center gap-1.5 mr-3 mb-1 text-sm">
        <input type="checkbox" class="tag-checkbox" value="${t.id}" ${deviceTagIds.has(t.id) ? "checked" : ""} />
        ${escapeHtml(t.name)}
      </label>`
    )
    .join("");

  document.getElementById("content").innerHTML = `
    <div class="flex items-center justify-between">
      <h1 class="text-lg font-semibold">${escapeHtml(d.alias || d.hostname || d.rustdesk_id)}</h1>
      <span class="inline-flex items-center gap-3">
        ${statusBadge}
      </span>
    </div>
    ${approvalBanner(d)}
    ${uuidBanner(d, canManage)}
    <div class="card p-4">
      ${row("RustDesk ID", connectLink(d.rustdesk_id, d.approval === "approved" ? d.id : null))}
      ${row("Alias", `<input id="alias-input" value="${escapeHtml(d.alias)}" class="border border-slate-300 rounded px-2 py-1 text-sm" />`)}
      ${row("Hostname", `<span class="whitespace-nowrap">${escapeHtml(d.hostname ?? "-")}</span>`)}
      ${row("Platform", escapeHtml(d.platform ? fmtPlatform(d.platform) : "-"))}
      ${row("OS version", escapeHtml(d.os_version ?? "-"))}
      ${row("Client version", escapeHtml(d.client_version ?? "-") + (d.outdated ? ` <span class="badge" title="Older than the minimum client version" style="background:#d9770622;color:#d97706"><span class="badge-dot" style="background:#d97706"></span>Outdated</span>` : ""))}
      ${row("IP address", d.ip_address ? ipLabel(d.ip_address) : "-")}
      ${row("API connection", apiSchemeBadge(d.api_scheme))}
      ${row("CPU", `<span class="whitespace-nowrap">${escapeHtml(d.cpu ? fmtCpu(d.cpu) : "-")}</span>`)}
      ${row("Memory", `<span class="whitespace-nowrap">${escapeHtml(d.memory ? fmtMemory(d.memory) : "-")}</span>`)}
      ${row("Owner", d.owner_username ? userLink(d.owner_id, d.owner_username) : "-")}
      ${row("Group", `<select id="group-select" class="border border-slate-300 rounded px-2 py-1 text-sm">${groupOptions}</select>`)}
      ${row("Strategy", strategyCell)}
      ${
        currentUser && currentUser.is_admin
          ? row(
              "Offline alerts",
              `<label class="inline-flex items-center gap-2 text-sm" title="Sends a notification (see Settings) when this device stops reporting">
                 <input type="checkbox" id="watch-toggle" ${d.watch_offline ? "checked" : ""} /> Notify me when this device goes offline
               </label>`
            )
          : ""
      }
      ${row("Note", `<input id="note-input" value="${escapeHtml(d.note)}" maxlength="500" class="border border-slate-300 rounded px-2 py-1 text-sm w-64 max-w-full" />`)}
      ${row("Last seen", `<span class="whitespace-nowrap">${fmtDate(d.last_seen)}</span>`)}
      ${row("Registered", `<span class="whitespace-nowrap">${fmtDate(d.created_at)}</span>`)}
      ${
        canManage
          ? row(
              "Archive",
              d.archived
                ? `<span class="inline-flex items-center gap-2"><span class="badge badge-offline"><span class="badge-dot"></span>Archived</span>
                     <button id="archive-toggle" type="button" class="px-2 py-1 rounded-md border border-slate-300 text-sm hover:bg-slate-100">Restore to the list</button></span>`
                : `<button id="archive-toggle" type="button" class="px-2 py-1 rounded-md border border-slate-300 text-sm hover:bg-slate-100" title="Hide it from the default device list; it returns when it reports again">Archive</button>`
            )
          : ""
      }
      ${row("Logs", `<a href="/logs?device_id=${d.id}" class="text-link underline">Connections</a> &middot; <a href="/logs?tab=file&device_id=${d.id}" class="text-link underline">File transfers</a>`)}
    </div>
    <div class="card p-4">
      <p class="text-xs text-slate-500 mb-2">Tags</p>
      <div id="tag-checkboxes">${tagCheckboxes || '<span class="text-sm text-slate-400">No tags exist yet - create one on the Tags page.</span>'}</div>
      <p class="mt-2">${tagBadges(d.tags)}</p>
    </div>
    <div class="flex gap-3">
      <button id="save-btn" class="px-4 py-2 rounded-md bg-brand-600 text-white text-sm font-medium hover:bg-brand-700">Save</button>
      <button id="delete-btn" class="px-4 py-2 rounded-md border border-red-300 dark:border-red-800 text-red-600 dark:text-red-400 text-sm font-medium hover:bg-red-50 dark:hover:bg-red-950/40">Delete device</button>
    </div>
    <div id="connections-section"></div>
    <div id="sharing-section"></div>
    <div id="timeline-section"></div>
  `;

  for (const [id, verb] of [["uuid-accept", "accept"], ["uuid-reject", "reject"]]) {
    const button = document.getElementById(id);
    if (!button) continue;
    button.addEventListener("click", async () => {
      if (verb === "accept" && !confirm(t("Accept the new install? Only do this if you reinstalled or replaced this machine."))) return;
      try {
        await api(`/api/v1/devices/${deviceId}/uuid/${verb}`, { method: "POST" });
        toast(verb === "accept" ? "The new install was accepted." : "The new install was rejected.", "success");
        render();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  for (const [id, verb] of [["approve-btn", "approve"], ["reject-btn", "reject"]]) {
    const button = document.getElementById(id);
    if (!button) continue;
    button.addEventListener("click", async () => {
      try {
        await api(`/api/v1/devices/${deviceId}/${verb}`, { method: "POST" });
        toast(verb === "approve" ? "The device was approved." : "The device was rejected.", "success");
        render();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  const watchToggle = document.getElementById("watch-toggle");
  if (watchToggle) {
    watchToggle.addEventListener("change", async () => {
      try {
        await api(`/api/v1/devices/${deviceId}/watch`, { method: "PUT", body: JSON.stringify({ watch: watchToggle.checked }) });
        toast(watchToggle.checked ? "You will be notified when this device goes offline." : "Offline notifications are off for this device.", "success");
      } catch (err) {
        watchToggle.checked = !watchToggle.checked;
        toast(err.message, "error");
      }
    });
  }

  const archiveToggle = document.getElementById("archive-toggle");
  if (archiveToggle) {
    archiveToggle.addEventListener("click", async () => {
      try {
        await api(`/api/v1/devices/${deviceId}/archive`, { method: "PUT", body: JSON.stringify({ archived: !d.archived }) });
        toast(d.archived ? "The device is back in the list." : "The device was archived.", "success");
        render();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  document.getElementById("save-btn").addEventListener("click", async () => {
    const alias = document.getElementById("alias-input").value;
    const groupValue = document.getElementById("group-select").value;
    const tag_ids = Array.from(document.querySelectorAll(".tag-checkbox:checked")).map((el) =>
      parseInt(el.value, 10)
    );
    const payload = { alias, tag_ids, note: document.getElementById("note-input").value };
    if (groupValue) payload.group_id = parseInt(groupValue, 10);
    try {
      await api(`/api/v1/devices/${deviceId}`, { method: "PATCH", body: JSON.stringify(payload) });
      const strategySelect = document.getElementById("strategy-select");
      if (strategySelect) {
        const value = strategySelect.value;
        await api(`/api/v1/devices/${deviceId}/strategy`, {
          method: "PUT",
          body: JSON.stringify({ strategy_id: value ? parseInt(value, 10) : null }),
        });
      }
      toast("Saved.", "success");
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  });

  document.getElementById("delete-btn").addEventListener("click", async () => {
    if (!confirm(t("Delete this device? This cannot be undone."))) return;
    try {
      await api(`/api/v1/devices/${deviceId}`, { method: "DELETE" });
      window.location.href = "/devices";
    } catch (err) {
      toast(err.message, "error");
    }
  });

  await renderSharing();
  await renderConnections(canManage);
  await loadTimeline(true);
  if (connectionTimer) clearInterval(connectionTimer);
  connectionTimer = setInterval(() => {
    if (!document.hidden) renderConnections(canManage);
  }, 5000);
}

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("devices", user);
  try {
    await render();
  } catch (err) {
    document.getElementById("content").innerHTML =
      `<p class="text-sm text-slate-500">${escapeHtml(err.message)}</p>`;
  }
})();
