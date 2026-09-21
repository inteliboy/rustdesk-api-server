// Only an administrator can set a group's strategy (it changes how the devices
// in it behave); everyone else just sees which one applies.
function strategyCell(group, strategies, isAdmin) {
  if (!isAdmin) return escapeHtml(group.strategy_name || "-");
  const options =
    `<option value="">None</option>` +
    strategies
      .map((s) => `<option value="${s.id}" ${s.id === group.strategy_id ? "selected" : ""}>${escapeHtml(s.name)}</option>`)
      .join("");
  return `<select data-id="${group.id}" class="group-strategy border border-slate-300 rounded px-2 py-1 text-sm">${options}</select>`;
}

async function loadGroups() {
  const isAdmin = currentUser && currentUser.is_admin;
  const [groups, strategies] = await Promise.all([
    api("/api/v1/groups"),
    isAdmin ? api("/api/v1/strategies") : Promise.resolve([]),
  ]);
  const rowsEl = document.getElementById("group-rows");
  const emptyEl = document.getElementById("empty-state");
  if (groups.length === 0) {
    rowsEl.innerHTML = "";
    emptyEl.classList.remove("hidden");
    return;
  }
  emptyEl.classList.add("hidden");
  rowsEl.innerHTML = groups
    .map(
      (g) => `<tr>
        <td class="px-4 py-3 font-medium">${escapeHtml(g.name)}</td>
        <td class="px-4 py-3 text-slate-500">${escapeHtml(g.description || "-")}</td>
        <td class="px-4 py-3">
          <a href="/devices?group_id=${g.id}" class="text-slate-600 hover:text-slate-900">${g.device_count}</a>
        </td>
        <td class="px-4 py-3">${strategyCell(g, strategies, isAdmin)}</td>
        <td class="px-4 py-3 text-right">
          <button data-id="${g.id}" class="delete-group text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Delete</button>
        </td>
      </tr>`
    )
    .join("");

  document.querySelectorAll(".group-strategy").forEach((select) =>
    select.addEventListener("change", async () => {
      try {
        await api(`/api/v1/groups/${select.dataset.id}/strategy`, {
          method: "PUT",
          body: JSON.stringify({ strategy_id: select.value ? parseInt(select.value, 10) : null }),
        });
        toast("Strategy updated.", "success");
      } catch (err) {
        toast(err.message, "error");
        loadGroups();
      }
    })
  );

  document.querySelectorAll(".delete-group").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm(t("Delete this group? Devices in it will become ungrouped."))) return;
      try {
        await api(`/api/v1/groups/${btn.dataset.id}`, { method: "DELETE" });
        loadGroups();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
}

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("groups", user);
  await loadGroups();
})();

document.getElementById("new-group-btn").addEventListener("click", () => {
  document.getElementById("new-group-form").classList.toggle("hidden");
});

document.getElementById("ng-create").addEventListener("click", async () => {
  const name = document.getElementById("ng-name").value;
  const description = document.getElementById("ng-description").value || null;
  if (!name) {
    toast("Name is required.", "error");
    return;
  }
  try {
    await api("/api/v1/groups", { method: "POST", body: JSON.stringify({ name, description }) });
    document.getElementById("ng-name").value = "";
    document.getElementById("ng-description").value = "";
    document.getElementById("new-group-form").classList.add("hidden");
    toast("Group created.", "success");
    loadGroups();
  } catch (err) {
    toast(err.message, "error");
  }
});
