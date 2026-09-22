// Admin-only page: the role permission matrix and user groups. The backend
// enforces every check itself (CLAUDE.md section 66); this page is only shown
// to an administrator (see the guard below) so it never needs to degrade for
// a non-admin viewer the way users.js does.

const AREAS = [
  ["devices", "Devices"],
  ["users", "Users"],
  ["groups", "Groups"],
  ["address_books", "Address books"],
  ["logs", "Logs"],
  ["policies", "Policies"],
  ["settings", "Settings"],
  ["tokens", "Tokens"],
];

let roles = [];
let users = [];
let editingRoleId = null;
let editingGroupId = null;

function permissionsSummary(role) {
  const parts = AREAS.filter(([key]) => role.permissions[key] && role.permissions[key] !== "none").map(
    ([key, label]) => `${label}: ${role.permissions[key]}`
  );
  return parts.length ? parts.join(", ") : "None";
}

function renderMatrix(permissions) {
  const p = permissions || {};
  document.getElementById("rf-matrix").innerHTML = AREAS.map(([key, label]) => {
    const level = p[key] || "none";
    return `<tr>
      <td class="py-1">${escapeHtml(label)}</td>
      ${["none", "view", "manage"]
        .map(
          (lvl) =>
            `<td class="py-1"><input type="radio" name="rf-area-${key}" value="${lvl}" ${lvl === level ? "checked" : ""} /></td>`
        )
        .join("")}
    </tr>`;
  }).join("");
}

function readMatrix() {
  const permissions = {};
  for (const [key] of AREAS) {
    const checked = document.querySelector(`input[name="rf-area-${key}"]:checked`);
    if (checked && checked.value !== "none") permissions[key] = checked.value;
  }
  return permissions;
}

function openRoleForm(role) {
  editingRoleId = role ? role.id : null;
  document.getElementById("role-form-title").textContent = role ? "Edit " + role.name : "New role";
  document.getElementById("rf-name").value = role ? role.name : "";
  document.getElementById("rf-description").value = role ? role.description || "" : "";
  document.getElementById("rf-2fa").checked = role ? role.requires_2fa : false;
  renderMatrix(role ? role.permissions : {});
  document.getElementById("rf-save").textContent = role ? "Save" : "Create";
  document.getElementById("role-form").classList.remove("hidden");
}

function closeRoleForm() {
  editingRoleId = null;
  document.getElementById("role-form").classList.add("hidden");
}

async function loadRoles() {
  roles = await api("/api/v1/roles");
  const rowsEl = document.getElementById("role-rows");
  const emptyEl = document.getElementById("role-empty");
  if (roles.length === 0) {
    rowsEl.innerHTML = "";
    emptyEl.classList.remove("hidden");
  } else {
    emptyEl.classList.add("hidden");
    rowsEl.innerHTML = roles
      .map(
        (r) => `<tr>
          <td class="px-4 py-3 font-medium">${escapeHtml(r.name)}</td>
          <td class="px-4 py-3 text-slate-500">${escapeHtml(r.description || "-")}</td>
          <td class="px-4 py-3 text-slate-500">${escapeHtml(permissionsSummary(r))}</td>
          <td class="px-4 py-3">${r.requires_2fa ? "Required" : "-"}</td>
          <td class="px-4 py-3 text-right space-x-2 whitespace-nowrap">
            <button data-id="${r.id}" class="edit-role text-slate-500 hover:text-slate-900">Edit</button>
            <button data-id="${r.id}" class="delete-role text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Delete</button>
          </td>
        </tr>`
      )
      .join("");
    document.querySelectorAll(".edit-role").forEach((btn) =>
      btn.addEventListener("click", () => openRoleForm(roles.find((r) => r.id === parseInt(btn.dataset.id, 10))))
    );
    document.querySelectorAll(".delete-role").forEach((btn) =>
      btn.addEventListener("click", async () => {
        if (!confirm(t("Delete this role? Anyone holding it (directly or through a user group) loses its access."))) return;
        try {
          await api(`/api/v1/roles/${btn.dataset.id}`, { method: "DELETE" });
          loadRoles();
          loadGroups();
        } catch (err) {
          toast(err.message, "error");
        }
      })
    );
  }
  // The user-group form's role picker follows the current role list.
  const roleSelect = document.getElementById("gf-role");
  const current = roleSelect.value;
  roleSelect.innerHTML =
    `<option value="">No role</option>` +
    roles.map((r) => `<option value="${r.id}">${escapeHtml(r.name)}</option>`).join("");
  roleSelect.value = current;
}

document.getElementById("new-role-btn").addEventListener("click", () => openRoleForm(null));
document.getElementById("rf-cancel").addEventListener("click", closeRoleForm);
document.getElementById("rf-save").addEventListener("click", async () => {
  const name = document.getElementById("rf-name").value;
  if (!name) {
    toast("Name is required.", "error");
    return;
  }
  const payload = {
    name,
    description: document.getElementById("rf-description").value || null,
    permissions: readMatrix(),
    requires_2fa: document.getElementById("rf-2fa").checked,
  };
  try {
    if (editingRoleId) {
      await api(`/api/v1/roles/${editingRoleId}`, { method: "PATCH", body: JSON.stringify(payload) });
      toast("Role updated.", "success");
    } else {
      await api("/api/v1/roles", { method: "POST", body: JSON.stringify(payload) });
      toast("Role created.", "success");
    }
    closeRoleForm();
    loadRoles();
  } catch (err) {
    toast(err.message, "error");
  }
});

// --- User groups ---------------------------------------------------------

function openGroupForm(group) {
  editingGroupId = group ? group.id : null;
  document.getElementById("group-form-title").textContent = group ? "Edit " + group.name : "New user group";
  document.getElementById("gf-name").value = group ? group.name : "";
  document.getElementById("gf-description").value = group ? group.description || "" : "";
  document.getElementById("gf-role").value = group && group.role_id ? String(group.role_id) : "";
  const memberIds = new Set(group ? group.member_ids : []);
  document.getElementById("gf-members").innerHTML = users
    .map(
      (u) =>
        `<label class="flex items-center gap-2 py-0.5"><input type="checkbox" value="${u.id}" ${memberIds.has(u.id) ? "checked" : ""} /> ${escapeHtml(u.username)}</label>`
    )
    .join("");
  document.getElementById("gf-save").textContent = group ? "Save" : "Create";
  document.getElementById("group-form").classList.remove("hidden");
}

function closeGroupForm() {
  editingGroupId = null;
  document.getElementById("group-form").classList.add("hidden");
}

async function loadGroups() {
  const groups = await api("/api/v1/user-groups");
  const rowsEl = document.getElementById("group-rows");
  const emptyEl = document.getElementById("group-empty");
  if (groups.length === 0) {
    rowsEl.innerHTML = "";
    emptyEl.classList.remove("hidden");
    return;
  }
  emptyEl.classList.add("hidden");
  rowsEl.innerHTML = groups
    .map((g) => {
      const role = roles.find((r) => r.id === g.role_id);
      return `<tr>
        <td class="px-4 py-3 font-medium">${escapeHtml(g.name)}</td>
        <td class="px-4 py-3 text-slate-500">${escapeHtml(g.description || "-")}</td>
        <td class="px-4 py-3 text-slate-500">${role ? escapeHtml(role.name) : "-"}</td>
        <td class="px-4 py-3 text-slate-500">${g.member_ids.length}</td>
        <td class="px-4 py-3 text-right space-x-2 whitespace-nowrap">
          <button data-id="${g.id}" class="edit-group text-slate-500 hover:text-slate-900">Edit</button>
          <button data-id="${g.id}" class="delete-group text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Delete</button>
        </td>
      </tr>`;
    })
    .join("");
  document.querySelectorAll(".edit-group").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const g = (await api("/api/v1/user-groups")).find((x) => x.id === parseInt(btn.dataset.id, 10));
      if (g) openGroupForm(g);
    })
  );
  document.querySelectorAll(".delete-group").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm(t("Delete this user group?"))) return;
      try {
        await api(`/api/v1/user-groups/${btn.dataset.id}`, { method: "DELETE" });
        loadGroups();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
}

document.getElementById("new-group-btn").addEventListener("click", () => openGroupForm(null));
document.getElementById("gf-cancel").addEventListener("click", closeGroupForm);
document.getElementById("gf-save").addEventListener("click", async () => {
  const name = document.getElementById("gf-name").value;
  if (!name) {
    toast("Name is required.", "error");
    return;
  }
  const roleValue = document.getElementById("gf-role").value;
  const memberIds = Array.from(document.querySelectorAll("#gf-members input:checked")).map((el) =>
    parseInt(el.value, 10)
  );
  try {
    if (editingGroupId) {
      const payload = {
        name,
        description: document.getElementById("gf-description").value || null,
        member_ids: memberIds,
      };
      if (roleValue) payload.role_id = parseInt(roleValue, 10);
      else payload.clear_role = true;
      await api(`/api/v1/user-groups/${editingGroupId}`, { method: "PATCH", body: JSON.stringify(payload) });
      toast("User group updated.", "success");
    } else {
      await api("/api/v1/user-groups", {
        method: "POST",
        body: JSON.stringify({
          name,
          description: document.getElementById("gf-description").value || null,
          role_id: roleValue ? parseInt(roleValue, 10) : null,
          member_ids: memberIds,
        }),
      });
      toast("User group created.", "success");
    }
    closeGroupForm();
    loadGroups();
  } catch (err) {
    toast(err.message, "error");
  }
});

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("roles", user);
  if (!user.is_admin) {
    document.querySelector("main").innerHTML = `<p class="text-sm text-slate-500">Administrator access is required to view this page.</p>`;
    return;
  }
  users = await api("/api/v1/users");
  await loadRoles();
  await loadGroups();
})();
