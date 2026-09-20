let me = null;

function isLocked(user) {
  return Boolean(user.locked_until) && parseServerDate(user.locked_until) > new Date();
}

// The link is shown once and only a hash is stored, so this is the only chance
// to copy it. The token is after the "#": a browser never sends that part to
// the server.
function showResetLink(username, issued) {
  const box = document.getElementById("reset-link-box");
  box.classList.remove("hidden");
  document.getElementById("reset-link-user").textContent = username;
  document.getElementById("reset-link-url").textContent = issued.url;
  document.getElementById("reset-link-expiry").textContent = fmtDate(issued.expires_at);
  box.scrollIntoView({ block: "nearest" });
}

document.getElementById("reset-link-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(document.getElementById("reset-link-url").textContent);
    toast("Link copied.", "success");
  } catch (err) {
    toast("Could not copy - select the link and copy it by hand.", "error");
  }
});
document.getElementById("reset-link-close").addEventListener("click", () => {
  document.getElementById("reset-link-box").classList.add("hidden");
  document.getElementById("reset-link-url").textContent = "";
});

async function loadUsers() {
  const items = await api("/api/v1/users");
  document.getElementById("user-rows").innerHTML = items
    .map(
      (u) => `<tr>
        <td class="px-4 py-3 font-medium">
          <button data-id="${u.id}" class="view-user text-link hover:underline whitespace-nowrap">${escapeHtml(u.username)}</button>
        </td>
        <td class="px-4 py-3 text-slate-500">${escapeHtml(u.email || "-")}</td>
        <td class="px-4 py-3">${u.is_admin ? "Administrator" : "User"}</td>
        <td class="px-4 py-3">
          <span class="badge ${u.is_active ? "badge-online" : "badge-offline"}">
            <span class="badge-dot"></span>${u.is_active ? "Active" : "Disabled"}
          </span>
          ${u.two_factor_enabled ? `<span class="badge badge-online ml-1" title="Signs in with a second factor"><span class="badge-dot"></span>2FA</span>` : ""}
          ${isLocked(u) ? `<span class="badge badge-offline ml-1" title="Locked after too many wrong passwords until ${escapeHtml(fmtDate(u.locked_until))}"><span class="badge-dot"></span>Locked</span>` : ""}
        </td>
        <td class="px-4 py-3 text-slate-500 whitespace-nowrap">${fmtDate(u.last_login_at)}</td>
        <td class="px-4 py-3 text-right space-x-2 whitespace-nowrap">
          ${
            u.id !== me.id
              ? `${isLocked(u) ? `<button data-id="${u.id}" class="unlock-user text-slate-500 hover:text-slate-900">Unlock</button>` : ""}
                 ${u.is_active ? `<button data-id="${u.id}" data-name="${escapeHtml(u.username)}" class="reset-link text-slate-500 hover:text-slate-900">Reset link</button>` : ""}
                 ${u.two_factor_enabled ? `<button data-id="${u.id}" class="reset-2fa text-slate-500 hover:text-slate-900">Reset 2FA</button>` : ""}
                 <button data-id="${u.id}" data-active="${u.is_active}" class="toggle-active text-slate-500 hover:text-slate-900">${u.is_active ? "Disable" : "Enable"}</button>
                 <button data-id="${u.id}" class="delete-user text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Delete</button>`
              : `<span class="text-slate-300">You</span>`
          }
        </td>
      </tr>`
    )
    .join("");

  document.querySelectorAll(".view-user").forEach((btn) =>
    btn.addEventListener("click", () => openUserDetail(parseInt(btn.dataset.id, 10)))
  );
  document.querySelectorAll(".unlock-user").forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/v1/users/${btn.dataset.id}/unlock`, { method: "POST" });
        toast("The account is unlocked.", "success");
        loadUsers();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
  document.querySelectorAll(".reset-link").forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        const issued = await api(`/api/v1/users/${btn.dataset.id}/reset-link`, { method: "POST" });
        showResetLink(btn.dataset.name, issued);
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
  document.querySelectorAll(".reset-2fa").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Turn off two-factor authentication for this user and sign them out everywhere? Use this when they have lost their authenticator and recovery codes.")) return;
      try {
        await api(`/api/v1/users/${btn.dataset.id}/two-factor`, { method: "DELETE" });
        toast("Two-factor authentication was reset.", "success");
        loadUsers();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
  document.querySelectorAll(".toggle-active").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const isActive = btn.dataset.active === "true";
      await api(`/api/v1/users/${btn.dataset.id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_active: !isActive }),
      });
      loadUsers();
    })
  );
  document.querySelectorAll(".delete-user").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this user?")) return;
      await api(`/api/v1/users/${btn.dataset.id}`, { method: "DELETE" });
      loadUsers();
    })
  );
}

function closeUserDetail() {
  document.getElementById("user-detail-modal").classList.add("hidden");
  document.getElementById("user-detail-modal").classList.remove("flex");
  // Drop ?id= so a refresh does not reopen it.
  if (window.location.search) window.history.replaceState(null, "", "/users");
}

async function openUserDetail(userId) {
  const modal = document.getElementById("user-detail-modal");
  modal.classList.remove("hidden");
  modal.classList.add("flex");
  document.getElementById("ud-username").textContent = "Loading...";
  document.getElementById("ud-profile").innerHTML = "";
  document.getElementById("ud-devices").innerHTML = "";
  document.getElementById("ud-activity").innerHTML = "";

  const [detail, devices, activity] = await Promise.all([
    api(`/api/v1/users/${userId}`),
    api(`/api/v1/devices?owner_id=${userId}&page_size=10`),
    api(`/api/v1/admin/audit-logs?actor_id=${userId}&page_size=10`),
  ]);

  document.getElementById("ud-username").textContent = detail.username;
  document.getElementById("ud-profile").innerHTML = `
    <div class="grid grid-cols-2 gap-y-1.5">
      <span class="text-slate-500">Email</span><span>${escapeHtml(detail.email || "-")}</span>
      <span class="text-slate-500">Role</span><span>${detail.is_admin ? "Administrator" : "User"}</span>
      <span class="text-slate-500">Status</span><span>${detail.is_active ? "Active" : "Disabled"}</span>
      <span class="text-slate-500">Two-factor</span><span>${detail.two_factor_enabled ? "On" : "Off"}</span>
      <span class="text-slate-500">Created</span><span class="whitespace-nowrap">${fmtDate(detail.created_at)}</span>
      <span class="text-slate-500">Last login</span><span class="whitespace-nowrap">${fmtDate(detail.last_login_at)}</span>
    </div>`;

  const devicesEl = document.getElementById("ud-devices");
  if (devices.items.length === 0) {
    devicesEl.innerHTML = `<p class="text-slate-400">No devices owned.</p>`;
  } else {
    devicesEl.innerHTML = devices.items
      .map(
        (d) => `<div class="flex items-center justify-between py-1.5 border-b border-slate-100 last:border-0">
          <a href="/devices/${d.id}" class="text-link hover:underline">${escapeHtml(d.alias || d.hostname || d.rustdesk_id)}</a>
          <span class="badge ${d.online ? "badge-online" : "badge-offline"}"><span class="badge-dot"></span>${d.online ? "Online" : "Offline"}</span>
        </div>`
      )
      .join("");
    if (devices.total > devices.items.length) {
      devicesEl.innerHTML += `<p class="text-slate-400 mt-1">+${devices.total - devices.items.length} more</p>`;
    }
  }

  const activityEl = document.getElementById("ud-activity");
  if (activity.items.length === 0) {
    activityEl.innerHTML = `<p class="text-slate-400">No recorded activity.</p>`;
  } else {
    activityEl.innerHTML = activity.items
      .map(
        (a) => `<div class="flex items-center justify-between py-1.5 border-b border-slate-100 last:border-0 gap-3">
          <span class="${a.result !== "success" ? "text-red-600 dark:text-red-400" : ""}">${describeActivity(a, { includeActor: false })}</span>
          <span class="text-slate-500 whitespace-nowrap">${fmtDate(a.created_at)}</span>
        </div>`
      )
      .join("");
  }
}

document.getElementById("ud-close").addEventListener("click", closeUserDetail);
document.getElementById("user-detail-modal").addEventListener("click", (evt) => {
  if (evt.target.id === "user-detail-modal") closeUserDetail();
});

(async () => {
  const user = await requireAuth();
  if (!user) return;
  me = user;
  renderNav("users", user);
  if (!user.is_admin) {
    document.querySelector("main").innerHTML = `<p class="text-sm text-slate-500">Administrator access is required to view this page.</p>`;
    return;
  }
  await loadUsers();
  // /users?id=N (used by username links elsewhere) opens that user's details.
  const linkedId = new URLSearchParams(window.location.search).get("id") || "";
  if (/^\d+$/.test(linkedId)) {
    try {
      await openUserDetail(parseInt(linkedId, 10));
    } catch (err) {
      closeUserDetail();
      toast(err.message || "User not found", "error");
    }
  }
})();

document.getElementById("new-user-btn").addEventListener("click", () => {
  document.getElementById("new-user-form").classList.toggle("hidden");
});

document.getElementById("nu-create").addEventListener("click", async () => {
  try {
    await api("/api/v1/users", {
      method: "POST",
      body: JSON.stringify({
        username: document.getElementById("nu-username").value,
        email: document.getElementById("nu-email").value || null,
        password: document.getElementById("nu-password").value,
        is_admin: document.getElementById("nu-admin").checked,
      }),
    });
    document.getElementById("new-user-form").classList.add("hidden");
    toast("User created.", "success");
    loadUsers();
  } catch (err) {
    toast(err.message, "error");
  }
});
