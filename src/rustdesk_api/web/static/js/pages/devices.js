const urlParams = new URLSearchParams(window.location.search);
let state = {
  page: 1,
  pageSize: 25,
  search: "",
  groupId: urlParams.get("group_id") || "",
  tagId: urlParams.get("tag_id") || "",
  status: ["online", "offline", "archived", "pending", "rejected"].includes(urlParams.get("status")) ? urlParams.get("status") : "",
  sort: ["last_seen", "created", "name", "id"].includes(urlParams.get("sort")) ? urlParams.get("sort") : "last_seen",
};

// What the bulk bar can do. `needs` says which list fills the second dropdown.
let lookups = { groups: [], tags: [], users: [], strategies: [], views: [] };
const selected = new Set();

function approvalBadge(approval) {
  return approval === "rejected"
    ? `<span class="badge badge-offline" title="An administrator turned this device down; nothing it sends is used"><span class="badge-dot"></span>Rejected</span>`
    : `<span class="badge" title="Waiting for an administrator to approve it" style="background:#d9770622;color:#d97706"><span class="badge-dot" style="background:#d97706"></span>Pending</span>`;
}

function statusBadge(online) {
  return online
    ? `<span class="badge badge-online"><span class="badge-dot"></span>Online</span>`
    : `<span class="badge badge-offline"><span class="badge-dot"></span>Offline</span>`;
}

// Small extra badges after the online/offline one. Each says what it is in words,
// not just in colour.
function deviceFlags(d) {
  const flags = [];
  if (d.uuid_change_pending) {
    flags.push(`<span class="badge badge-offline" title="A different install is claiming this device"><span class="badge-dot"></span>Review</span>`);
  }
  if (d.archived) {
    flags.push(`<span class="badge badge-offline" title="Silent for a long time; it returns to the list when it reports again"><span class="badge-dot"></span>Archived</span>`);
  }
  if (d.outdated) {
    const c = "#d97706";
    flags.push(`<span class="badge" title="This RustDesk client is older than the minimum version" style="background:${c}22;color:${c}"><span class="badge-dot" style="background:${c}"></span>Outdated</span>`);
  }
  return flags.map((f) => ` ${f}`).join("");
}

function optionList(items, placeholder, selectedValue) {
  const first = placeholder === null ? "" : `<option value="">${escapeHtml(placeholder)}</option>`;
  return (
    first +
    items
      .map((i) => `<option value="${Number(i.id)}" ${String(i.id) === String(selectedValue) ? "selected" : ""}>${escapeHtml(i.name)}</option>`)
      .join("")
  );
}

async function loadFilterOptions() {
  const admin = currentUser && currentUser.is_admin;
  const [groups, tags, users, strategies] = await Promise.all([
    api("/api/v1/groups"),
    api("/api/v1/tags"),
    admin ? api("/api/v1/users") : Promise.resolve([]),
    admin ? api("/api/v1/strategies") : Promise.resolve([]),
  ]);
  lookups.groups = groups;
  lookups.tags = tags;
  lookups.users = users.map((u) => ({ id: u.id, name: u.username }));
  lookups.strategies = strategies;
  document.getElementById("group-filter").innerHTML = optionList(groups, "All groups", state.groupId);
  document.getElementById("tag-filter").innerHTML = optionList(tags, "All tags", state.tagId);
}

// ---------------------------------------------------------------- listing

function listParams() {
  const params = new URLSearchParams({ page: state.page, page_size: state.pageSize, sort: state.sort });
  if (state.search) params.set("search", state.search);
  if (state.groupId) params.set("group_id", state.groupId);
  if (state.tagId) params.set("tag_id", state.tagId);
  if (state.status) params.set("status", state.status);
  return params;
}

async function loadDevices() {
  const data = await api("/api/v1/devices?" + listParams().toString());

  const rowsEl = document.getElementById("device-rows");
  const emptyEl = document.getElementById("empty-state");
  selected.clear();
  document.getElementById("select-all").checked = false;
  if (data.items.length === 0) {
    rowsEl.innerHTML = "";
    emptyEl.classList.remove("hidden");
  } else {
    emptyEl.classList.add("hidden");
    rowsEl.innerHTML = data.items
      .map(
        (d) => `<tr id="device-row-${d.id}" class="hover:bg-slate-50 cursor-pointer" data-href="/devices/${d.id}">
          <td class="px-4 py-3"><input type="checkbox" class="row-select" value="${Number(d.id)}" aria-label="Select this device" /></td>
          <td class="px-4 py-3" data-cell="status"><span data-status-badge>${d.approval && d.approval !== "approved" ? approvalBadge(d.approval) : statusBadge(d.online)}</span>${deviceFlags(d)}</td>
          <td class="px-4 py-3" data-cell="scheme">${apiSchemeBadge(d.api_scheme)}</td>
          <td class="px-4 py-3">${connectLink(d.rustdesk_id, d.approval === "approved" ? d.id : null)}</td>
          <td class="px-4 py-3 whitespace-nowrap">${escapeHtml(d.alias || d.hostname || "-")}</td>
          <td class="px-4 py-3">${escapeHtml(d.platform ? fmtPlatform(d.platform) : "-")}</td>
          <td class="px-4 py-3 text-slate-500 whitespace-nowrap">${escapeHtml(d.cpu ? fmtCpuName(d.cpu) : "-")}</td>
          <td class="px-4 py-3 text-slate-500 whitespace-nowrap">${escapeHtml(d.memory ? fmtMemory(d.memory) : "-")}</td>
          <td class="px-4 py-3">${d.owner_username ? userLink(d.owner_id, d.owner_username) : "-"}</td>
          <td class="px-4 py-3 whitespace-nowrap">${escapeHtml(d.group_name || "-")}</td>
          <td class="px-4 py-3 space-x-1">${tagBadges(d.tags)}</td>
          <td class="px-4 py-3 text-slate-500 whitespace-nowrap">${fmtDate(d.created_at)}</td>
          <td class="px-4 py-3 text-slate-500 whitespace-nowrap" data-cell="last-seen">${fmtDate(d.last_seen)}</td>
        </tr>`
      )
      .join("");
  }
  updateBulkBar();
  refreshPendingBanner();

  const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  document.getElementById("page-info").textContent = `Page ${data.page} of ${totalPages} (${data.total} total)`;
  document.getElementById("prev-page").disabled = data.page <= 1;
  document.getElementById("next-page").disabled = data.page >= totalPages;
}

function reload(resetPage = true) {
  if (resetPage) state.page = 1;
  return loadDevices().catch((err) => toast(err.message, "error"));
}

// ---------------------------------------------------------------- bulk bar

function bulkActions() {
  const admin = currentUser && currentUser.is_admin;
  if (state.status === "pending") {
    return [
      { key: "approve", label: "Approve", needs: null },
      { key: "reject", label: "Reject", needs: null },
      { key: "delete", label: "Delete", needs: null },
    ];
  }
  if (state.status === "rejected") {
    return [
      { key: "approve", label: "Approve", needs: null },
      { key: "delete", label: "Delete", needs: null },
    ];
  }
  const actions = [
    { key: "add_tag", label: "Add tag...", needs: "tags" },
    { key: "remove_tag", label: "Remove tag...", needs: "tags" },
    { key: "set_group", label: "Move to group...", needs: "groups", clearLabel: "(no group)" },
  ];
  if (admin) {
    actions.push(
      { key: "reject", label: "Reject", needs: null },
      { key: "set_owner", label: "Set owner...", needs: "users", clearLabel: "(no owner)" },
      { key: "set_strategy", label: "Set strategy...", needs: "strategies", clearLabel: "(no strategy)" },
      { key: "watch", label: "Notify when offline", needs: null },
      { key: "unwatch", label: "Stop offline notifications", needs: null }
    );
  }
  actions.push(
    { key: "archive", label: "Archive", needs: null },
    { key: "unarchive", label: "Restore from archive", needs: null },
    { key: "delete", label: "Delete", needs: null }
  );
  return actions;
}

function currentBulkAction() {
  return bulkActions().find((a) => a.key === document.getElementById("bulk-action").value);
}

function refreshBulkTarget() {
  const action = currentBulkAction();
  const target = document.getElementById("bulk-target");
  if (!action || !action.needs) {
    target.classList.add("hidden");
    target.innerHTML = "";
    return;
  }
  const clear = action.clearLabel ? `<option value="none">${escapeHtml(action.clearLabel)}</option>` : "";
  target.innerHTML = `<option value="" disabled selected>Choose...</option>` + clear + optionList(lookups[action.needs], null, "");
  target.classList.remove("hidden");
}

function updateBulkBar() {
  const bar = document.getElementById("bulk-bar");
  bar.classList.toggle("hidden", selected.size === 0);
  document.getElementById("bulk-count").textContent = `${selected.size} selected`;
  const all = document.querySelectorAll(".row-select");
  document.getElementById("select-all").checked = all.length > 0 && selected.size === all.length;
}

function fillBulkActions() {
  document.getElementById("bulk-action").innerHTML = bulkActions()
    .map((a) => `<option value="${escapeHtml(a.key)}">${escapeHtml(a.label)}</option>`)
    .join("");
  refreshBulkTarget();
}

async function applyBulk() {
  const action = currentBulkAction();
  if (!action || selected.size === 0) return;
  const body = { ids: Array.from(selected), action: action.key };
  if (action.needs) {
    const value = document.getElementById("bulk-target").value;
    if (!value) {
      toast("Choose what to apply.", "error");
      return;
    }
    const field = { tags: "tag_id", groups: "group_id", users: "owner_id", strategies: "strategy_id" }[action.needs];
    body[field] = value === "none" ? null : parseInt(value, 10);
  }
  if (action.key === "delete" && !confirm(t("Delete {1} device(s)? This cannot be undone.", [selected.size]))) return;
  try {
    const result = await api("/api/v1/devices/bulk", { method: "POST", body: JSON.stringify(body) });
    const skipped = result.skipped.length;
    toast(
      `Updated ${result.updated.length} device(s)` + (skipped ? `, skipped ${skipped} you cannot change.` : "."),
      skipped && result.updated.length === 0 ? "error" : "success"
    );
    await reload(false);
  } catch (err) {
    toast(err.message, "error");
  }
}

document.getElementById("bulk-action").addEventListener("change", refreshBulkTarget);
document.getElementById("bulk-apply").addEventListener("click", applyBulk);
document.getElementById("bulk-clear").addEventListener("click", () => {
  selected.clear();
  document.querySelectorAll(".row-select").forEach((box) => (box.checked = false));
  updateBulkBar();
});
document.getElementById("select-all").addEventListener("change", (evt) => {
  document.querySelectorAll(".row-select").forEach((box) => {
    box.checked = evt.target.checked;
    const id = parseInt(box.value, 10);
    if (evt.target.checked) selected.add(id);
    else selected.delete(id);
  });
  updateBulkBar();
});
document.getElementById("device-rows").addEventListener("change", (evt) => {
  const box = evt.target.closest(".row-select");
  if (!box) return;
  const id = parseInt(box.value, 10);
  if (box.checked) selected.add(id);
  else selected.delete(id);
  updateBulkBar();
});

// ------------------------------------------------------------- saved views

function currentQuery() {
  const query = { sort: state.sort };
  if (state.search) query.search = state.search;
  if (state.groupId) query.group_id = parseInt(state.groupId, 10);
  if (state.tagId) query.tag_id = parseInt(state.tagId, 10);
  if (state.status) query.status = state.status;
  return query;
}

function showFilters() {
  document.getElementById("status-filter").value = state.status;
  document.getElementById("sort-select").value = state.sort;
  document.getElementById("search").value = state.search;
  document.getElementById("group-filter").value = state.groupId;
  document.getElementById("tag-filter").value = state.tagId;
}

async function loadViews(selectId) {
  lookups.views = await api("/api/v1/views");
  const select = document.getElementById("view-select");
  select.innerHTML = optionList(lookups.views, "Saved views", selectId || "");
  document.getElementById("view-delete").classList.toggle("hidden", !selectId);
}

document.getElementById("view-select").addEventListener("change", async (evt) => {
  const view = lookups.views.find((v) => String(v.id) === evt.target.value);
  document.getElementById("view-delete").classList.toggle("hidden", !view);
  if (!view) return;
  const q = view.query;
  state.search = q.search || "";
  state.groupId = q.group_id ? String(q.group_id) : "";
  state.tagId = q.tag_id ? String(q.tag_id) : "";
  state.status = q.status || "";
  state.sort = q.sort || "last_seen";
  showFilters();
  await reload();
});

document.getElementById("view-save-toggle").addEventListener("click", () => {
  document.getElementById("view-save-form").classList.toggle("hidden");
  document.getElementById("view-name").focus();
});

document.getElementById("view-save").addEventListener("click", async () => {
  const name = document.getElementById("view-name").value.trim();
  if (!name) {
    toast("Give the view a name.", "error");
    return;
  }
  try {
    const saved = await api("/api/v1/views", { method: "POST", body: JSON.stringify({ name, query: currentQuery() }) });
    document.getElementById("view-name").value = "";
    document.getElementById("view-save-form").classList.add("hidden");
    toast("View saved.", "success");
    await loadViews(saved.id);
  } catch (err) {
    toast(err.message, "error");
  }
});

document.getElementById("view-delete").addEventListener("click", async () => {
  const id = document.getElementById("view-select").value;
  if (!id || !confirm(t("Delete this saved view?"))) return;
  try {
    await api(`/api/v1/views/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("View deleted.", "success");
    await loadViews();
  } catch (err) {
    toast(err.message, "error");
  }
});

// ------------------------------------------------- devices waiting for approval

// Administrators are told, above the list, that new devices are waiting.
async function refreshPendingBanner() {
  const banner = document.getElementById("pending-banner");
  if (!banner || !(currentUser && currentUser.is_admin)) return;
  try {
    const data = await api("/api/v1/devices?status=pending&page_size=1");
    banner.classList.toggle("hidden", data.total === 0 || state.status === "pending");
    document.getElementById("pending-count").textContent = t("{1} new device(s) are waiting for your approval.", [data.total]);
  } catch (err) {
    banner.classList.add("hidden"); // a banner that fails is not worth an error of its own
  }
}

document.getElementById("pending-review").addEventListener("click", () => {
  state.status = "pending";
  showFilters();
  fillBulkActions();
  reload();
});

// ------------------------------------------------------------------ import

function importSummary(result) {
  const parts = [`${result.created} to create`, `${result.updated} to update`];
  if (result.tags_created) parts.push(`${result.tags_created} new tag(s)`);
  const errors = result.errors
    .map((e) => `<li>Row ${Number(e.row)}: ${escapeHtml(e.message)}</li>`)
    .join("");
  if (errors) {
    return `<p class="text-red-600 dark:text-red-400 mb-1">Nothing was imported. Fix these rows first:</p><ul class="list-disc ml-5">${errors}</ul>`;
  }
  return `<p>${result.applied ? "Imported" : "This would be"}: ${escapeHtml(parts.join(", "))}.</p>`;
}

async function runImport(dryRun) {
  const file = document.getElementById("import-file").files[0];
  if (!file) {
    toast("Choose a file first.", "error");
    return;
  }
  const type = file.name.toLowerCase().endsWith(".json") ? "application/json" : "text/csv";
  try {
    const result = await api("/api/v1/import/devices" + (dryRun ? "?dry_run=true" : ""), {
      method: "POST",
      body: await file.text(),
      headers: { "Content-Type": type },
    });
    document.getElementById("import-result").innerHTML = importSummary(result);
    if (result.applied) {
      toast("Import finished.", "success");
      await loadFilterOptions();
      await reload();
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

document.getElementById("import-toggle").addEventListener("click", () =>
  document.getElementById("import-panel").classList.toggle("hidden")
);
document.getElementById("import-check").addEventListener("click", () => runImport(true));
document.getElementById("import-run").addEventListener("click", () => runImport(false));

// ---------------------------------------------------------- filters, paging

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("devices", user);
  if (user.is_admin) {
    document.getElementById("import-toggle").classList.remove("hidden");
    for (const opt of document.querySelectorAll("#status-filter [data-admin-only]")) opt.hidden = false;
  }
  await loadFilterOptions();
  showFilters(); // status and sort may come from the address (the dashboard links here)
  fillBulkActions();
  await Promise.all([loadViews(), loadDevices()]);

  // Live status/last-seen updates for whatever is on the current page -
  // a device outside the current page/filter is ignored here and simply
  // picked up by the next manual refresh or pagination/filter change.
  connectDeviceWs((msg) => {
    if (msg.type !== "device_updated") return;
    const row = document.getElementById(`device-row-${msg.device.id}`);
    if (!row) return;
    const statusCell = row.querySelector('[data-cell="status"]');
    const badge = statusCell && statusCell.querySelector("[data-status-badge]");
    if (badge && !badge.querySelector(".badge[style]")) badge.innerHTML = statusBadge(msg.device.online);
    const schemeCell = row.querySelector('[data-cell="scheme"]');
    if (schemeCell) schemeCell.innerHTML = apiSchemeBadge(msg.device.api_scheme);
    const lastSeenCell = row.querySelector('[data-cell="last-seen"]');
    if (lastSeenCell) lastSeenCell.textContent = fmtDate(msg.device.last_seen);
  });
})();

let searchTimer;
document.getElementById("search").addEventListener("input", (evt) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = evt.target.value;
    reload();
  }, 300);
});

for (const [id, key] of [
  ["group-filter", "groupId"],
  ["tag-filter", "tagId"],
  ["status-filter", "status"],
  ["sort-select", "sort"],
]) {
  document.getElementById(id).addEventListener("change", (evt) => {
    state[key] = evt.target.value;
    if (key === "status") fillBulkActions();
    reload();
  });
}

document.getElementById("prev-page").addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    reload(false);
  }
});
document.getElementById("next-page").addEventListener("click", () => {
  state.page += 1;
  reload(false);
});
