// Literal class names (not built from parts) so the Tailwind build sees them.
const TONES = {
  brand: "border-l-brand-600",
  green: "border-l-green-500",
  slate: "border-l-slate-400",
  violet: "border-l-violet-500",
  amber: "border-l-amber-500",
  rose: "border-l-rose-500",
};

// A round icon in the card's own color sits at the right (the same Remix icons as the menu).
const ICON_TONES = {
  brand: "stat-icon-brand",
  green: "stat-icon-green",
  slate: "stat-icon-slate",
  violet: "stat-icon-violet",
  amber: "stat-icon-amber",
  rose: "stat-icon-rose",
};

function statCard(label, value, tone, icon) {
  const chip = icon && window.rdIcon ? `<span class="stat-icon ${ICON_TONES[tone]}">${window.rdIcon(icon, 20)}</span>` : "";
  return `<div class="card p-4 border-l-4 ${TONES[tone]} flex items-start justify-between gap-2">
    <div class="min-w-0">
      <p class="text-xs text-slate-500">${label}</p>
      <p class="text-2xl font-semibold mt-1">${value}</p>
    </div>
    ${chip}
  </div>`;
}

// The update check's answer (administrators), fetched after the stats; null until it arrives.
let update = null;
let lastStats = null;

// A line for the list when a newer build exists. The count and text are ours; the link is GitHub's,
// and only an https://github.com address is used.
function updateItem(u) {
  if (!u || (u.state !== "behind" && u.state !== "diverged")) return null;
  const n = u.behind_by;
  let text;
  if (u.in_image) {
    if (u.image === "published") {
      text = t("A newer version is available ({1} commit(s) ahead). Pull the new image and recreate the container.", [n]);
    } else if (u.image === "pending") {
      text = t("A newer version exists ({1} commit(s) ahead), but its image is not published yet: it is probably still being built.", [n]);
    } else {
      text = t("A newer version exists ({1} commit(s) ahead). Pull the new image once it is published.", [n]);
    }
  } else {
    text = t("A newer version is available ({1} commit(s) ahead of this build). Pull it and restart the server.", [n]);
  }
  const url = u.compare_url || (u.latest && u.latest.url) || "";
  const github = url.startsWith("https://github.com/");
  return { href: github ? url : "/dashboard", text: escapeHtml(text), external: github };
}

async function checkForUpdates(refresh) {
  try {
    update = await api("/api/v1/admin/update-status" + (refresh ? "?refresh=true" : ""));
  } catch (err) {
    update = null; // the check is a courtesy: the dashboard works without it
    return;
  }
  if (lastStats) renderAttention(lastStats);
  if (window.setUpdateStatus) window.setUpdateStatus(update);
}
window.checkForUpdates = checkForUpdates;

// Things worth a look, from the same stats: each is a sentence with a link to where to act on it.
function attentionItems(stats) {
  const items = [];
  const newer = updateItem(update);
  if (newer) items.push(newer);
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
    .map((i) => `<a href="${i.href}"${i.external ? ' target="_blank" rel="noopener noreferrer"' : ""} class="flex items-center justify-between px-4 py-3 text-sm hover:bg-slate-50"><span>${i.text}</span><span class="text-slate-400" aria-hidden="true">&rarr;</span></a>`)
    .join("");
}

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("dashboard", user);

  try {
    const stats = await api("/api/v1/admin/dashboard");
    document.getElementById("stats").innerHTML =
      statCard("Total devices", stats.total_devices, "brand", "devices") +
      statCard("Online", stats.online_devices, "green", "online") +
      statCard("Offline", stats.offline_devices, "slate", "offline") +
      statCard("Users", stats.total_users, "violet", "user") +
      statCard("Groups", stats.total_groups, "amber", "groups") +
      statCard("Tags", stats.total_tags, "rose", "tags");
    lastStats = stats;
    renderAttention(stats);
    initServerPanel(); // the stats call above succeeded, so this is an administrator
    await checkForUpdates(false); // last: the page is already drawn, and this never throws
  } catch (err) {
    document.getElementById("stats").innerHTML =
      err.status === 403
        ? `<p class="text-sm text-slate-500 col-span-4">Dashboard stats require administrator access.</p>`
        : `<p class="text-sm text-red-600 dark:text-red-400 col-span-4">Failed to load dashboard stats: ${escapeHtml(err.message)}</p>`;
  }
})();
