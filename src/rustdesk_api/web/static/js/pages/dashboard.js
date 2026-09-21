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

// "RustDesk API Server 0.1.0 · commit f81a8e8 · protocol checked against RustDesk client 1.4.9".
// Built with DOM calls rather than HTML strings: the pieces come from the server.
async function showBuildInfo() {
  const el = document.getElementById("build-info");
  try {
    const info = await api("/api/version");
    el.textContent = `RustDesk API Server ${info.version}`;
    const sep = () => el.append(" · ");
    sep();
    if (info.commit_short) {
      el.append("commit ");
      const link = document.createElement("a");
      link.textContent = info.commit_short;
      link.href = info.commit_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.className = "font-mono text-link hover:underline";
      el.append(link);
      if (info.dirty) el.append(" (uncommitted changes)");
    } else {
      el.append("commit unknown");
    }
    sep();
    el.append(`written against the RustDesk ${info.rustdesk_client_source} client source`);
  } catch (err) {
    el.textContent = ""; // the version line is a nicety; the dashboard works without it
  }
}

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("dashboard", user);
  showBuildInfo();

  try {
    const stats = await api("/api/v1/admin/dashboard");
    document.getElementById("stats").innerHTML =
      statCard("Total devices", stats.total_devices, "brand") +
      statCard("Online", stats.online_devices, "green") +
      statCard("Offline", stats.offline_devices, "slate") +
      statCard("Users", stats.total_users, "violet") +
      statCard("Groups", stats.total_groups, "amber") +
      statCard("Tags", stats.total_tags, "rose");
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
