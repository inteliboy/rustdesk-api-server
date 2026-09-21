// Literal class names (not built from parts) so the Tailwind build sees them.
const TONES = {
  brand: "border-l-brand-600",
  green: "border-l-green-500",
  slate: "border-l-slate-400",
  violet: "border-l-violet-500",
  amber: "border-l-amber-500",
  rose: "border-l-rose-500",
};

function statCard(label, value, tone) {
  return `<div class="card p-4 border-l-4 ${TONES[tone]}">
    <p class="text-xs text-slate-500">${label}</p>
    <p class="text-2xl font-semibold mt-1">${value}</p>
  </div>`;
}

// describeActivity() is defined in app.js, shared with the per-user
// activity panel on the Users page.

// Things worth a look, from the same stats: each is a sentence with a link to where to act on it.
function attentionItems(stats) {
  const items = [];
  if (stats.new_devices_24h > 0) {
    items.push({
      href: "/devices?sort=created",
      text: stats.new_devices_24h === 1 ? "1 new device registered in the last 24 hours" : `${stats.new_devices_24h} new devices registered in the last 24 hours`,
    });
  }
  if (stats.outdated_devices > 0) {
    items.push({
      href: "/devices",
      text: stats.outdated_devices === 1 ? "1 device runs an outdated RustDesk client" : `${stats.outdated_devices} devices run an outdated RustDesk client`,
    });
  }
  if (stats.devices_without_strategy > 0 && stats.total_devices > 0) {
    items.push({
      href: "/strategies",
      text: stats.devices_without_strategy === 1 ? "1 device receives no strategy" : `${stats.devices_without_strategy} devices receive no strategy`,
    });
  }
  if (stats.archived_devices > 0) {
    items.push({
      href: "/devices?status=archived",
      text: stats.archived_devices === 1 ? "1 archived device" : `${stats.archived_devices} archived devices`,
    });
  }
  return items;
}

function renderAttention(stats) {
  const host = document.getElementById("attention");
  if (!host) return;
  const items = attentionItems(stats);
  host.classList.toggle("hidden", items.length === 0);
  host.innerHTML = items
    .map((i) => `<a href="${i.href}" class="flex items-center justify-between px-4 py-3 text-sm hover:bg-slate-50"><span>${i.text}</span><span class="text-slate-400" aria-hidden="true">&rarr;</span></a>`)
    .join("");
}

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("dashboard", user);

  try {
    const stats = await api("/api/v1/admin/dashboard");
    document.getElementById("stats").innerHTML =
      statCard("Total devices", stats.total_devices, "brand") +
      statCard("Online", stats.online_devices, "green") +
      statCard("Offline", stats.offline_devices, "slate") +
      statCard("Users", stats.total_users, "violet") +
      statCard("Groups", stats.total_groups, "amber") +
      statCard("Tags", stats.total_tags, "rose");
    renderAttention(stats);
    initServerPanel(); // the stats call above succeeded, so this is an administrator
  } catch (err) {
    document.getElementById("stats").innerHTML =
      err.status === 403
        ? `<p class="text-sm text-slate-500 col-span-4">Dashboard stats require administrator access.</p>`
        : `<p class="text-sm text-red-600 dark:text-red-400 col-span-4">Failed to load dashboard stats: ${escapeHtml(err.message)}</p>`;
  }

  try {
    const logs = await api("/api/v1/admin/audit-logs?page_size=10");
    const activityEl = document.getElementById("activity");
    document.getElementById("audit-export").classList.remove("hidden");
    if (logs.items.length === 0) {
      activityEl.innerHTML = `<p class="p-4 text-sm text-slate-500">No activity yet.</p>`;
    } else {
      activityEl.innerHTML = logs.items
        .map(
          (item) => `<div class="p-4 flex items-center justify-between text-sm gap-3">
            <span class="${item.result !== "success" ? "text-red-600 dark:text-red-400" : ""}">${describeActivity(item)}</span>
            <span class="text-slate-400 whitespace-nowrap">${fmtDate(item.created_at)}</span>
          </div>`
        )
        .join("");
    }
  } catch (err) {
    document.getElementById("activity").innerHTML =
      err.status === 403
        ? `<p class="p-4 text-sm text-slate-500">Activity log requires administrator access.</p>`
        : `<p class="p-4 text-sm text-red-600 dark:text-red-400">Failed to load activity: ${escapeHtml(err.message)}</p>`;
  }
})();
