const CHANNEL_NAMES = { webhook: "Webhook", ntfy: "ntfy", email: "E-mail" };
const CHANNEL_HELP = {
  webhook: "NOTIFY_WEBHOOK_URL: a JSON POST that Slack, Mattermost, Discord and most automation tools accept.",
  ntfy: "NOTIFY_NTFY_URL: a topic on ntfy.sh or your own ntfy server, for phone notifications.",
  email: "NOTIFY_SMTP_HOST, NOTIFY_EMAIL_FROM and NOTIFY_EMAIL_TO: mail through your own SMTP server.",
};
const KIND_NAMES = { manual: "Manual", auto: "Scheduled", premigrate: "Before an upgrade" };

function on(flag) {
  const label = flag ? "On" : "Off";
  const cls = flag ? "badge-online" : "badge-offline";
  return `<span class="badge ${cls}"><span class="badge-dot"></span>${label}</span>`;
}

function fmtSize(bytes) {
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function renderNotifications(n) {
  document.getElementById("channels").innerHTML = n.channels
    .map(
      (c) => `<div class="card p-4">
        <div class="flex items-center justify-between mb-1">
          <p class="font-medium">${escapeHtml(CHANNEL_NAMES[c.name] || c.name)}</p>
          ${c.configured ? `<span class="badge badge-online"><span class="badge-dot"></span>Configured</span>` : `<span class="badge badge-offline"><span class="badge-dot"></span>Not configured</span>`}
        </div>
        <p class="text-xs text-slate-500">${c.configured ? `Sends to ${escapeHtml(c.target || "")}` : escapeHtml(CHANNEL_HELP[c.name] || "")}</p>
      </div>`
    )
    .join("");
  const any = n.channels.some((c) => c.configured);
  document.getElementById("test-notification").classList.toggle("hidden", !any);
  document.getElementById("events").innerHTML = n.events
    .map(
      (e) => `<tr>
        <td class="px-4 py-3 font-mono text-xs whitespace-nowrap">${escapeHtml(e.name)}</td>
        <td class="px-4 py-3">${escapeHtml(e.description)}</td>
        <td class="px-4 py-3">${on(e.enabled && any)}</td>
      </tr>`
    )
    .join("");
  document.getElementById("notification-notes").textContent = t(
    'Choose the events with NOTIFY_EVENTS. A device is reported offline after {1} minute(s) without a heartbeat (NOTIFY_OFFLINE_AFTER_MINUTES), and only if you turned on "Notify me when this device goes offline" on its page ({2} device(s) now). At most {3} notifications are sent per minute.',
    [n.offline_after_minutes, n.watched_devices, n.max_per_minute]
  );
}

function renderBackups(b) {
  const summary = document.getElementById("backup-summary");
  document.getElementById("backup-now").classList.toggle("hidden", !b.supported);
  if (!b.supported) {
    summary.textContent = t("Backups need SQLite, the default database; this server uses another one.");
  } else {
    const schedule =
      b.schedule_hours > 0 ? t("every {1} hour(s), keeping the newest {2}", [b.schedule_hours, b.keep]) : t("off (BACKUP_INTERVAL_HOURS=0)");
    summary.textContent = t("Scheduled backups: {1}. Backup before an upgrade: {2}. Folder: {3}", [
      schedule,
      b.before_migration ? t("on") : t("off"),
      b.directory,
    ]);
  }
  const rows = document.getElementById("backups");
  document.getElementById("backups-empty").classList.toggle("hidden", b.items.length > 0);
  rows.innerHTML = b.items
    .map(
      (i) => `<tr>
        <td class="px-4 py-3 font-mono text-xs">${escapeHtml(i.name)}</td>
        <td class="px-4 py-3">${escapeHtml(KIND_NAMES[i.kind] || i.kind)}</td>
        <td class="px-4 py-3 text-slate-500 whitespace-nowrap">${escapeHtml(fmtSize(i.size))}</td>
        <td class="px-4 py-3 text-slate-500 whitespace-nowrap">${fmtDate(i.created_at)}</td>
      </tr>`
    )
    .join("");
}

function renderFleet(f) {
  const row = (label, value) =>
    `<div class="flex items-center justify-between gap-4 px-4 py-3"><span>${label}</span><span class="text-slate-500">${value}</span></div>`;
  document.getElementById("fleet").innerHTML =
    row(
      "Archive devices that have not reported for",
      f.stale_days > 0 ? t("{1} day(s) (DEVICE_STALE_DAYS)", [f.stale_days]) : t("never (DEVICE_STALE_DAYS=0)")
    ) +
    row(
      "Flag clients older than",
      f.min_client_version ? `${escapeHtml(f.min_client_version)} (MIN_CLIENT_VERSION)` : t("not set (MIN_CLIENT_VERSION)")
    );
}

let optionGroups = [];

// The sentence of an option's detail ("every {1} hour(s), keeping {2}") with its values filled in.
function optionDetail(o) {
  return o.detail ? escapeHtml(t(o.detail, o.args)) : "";
}

function renderOptions() {
  const needle = document.getElementById("option-filter").value.trim().toLowerCase();
  const state = document.getElementById("option-state").value;
  let shown = 0;
  let total = 0;
  let enabled = 0;
  const html = optionGroups
    .map((g) => {
      total += g.items.length;
      enabled += g.items.filter((o) => o.enabled).length;
      const items = g.items.filter((o) => {
        if (state === "on" && !o.enabled) return false;
        if (state === "off" && o.enabled) return false;
        // Match the label as the visitor sees it (translated), the English one, and the variable names.
        const text = `${t(o.label)} ${o.label} ${t(o.help)} ${o.env.join(" ")}`.toLowerCase();
        return !needle || text.includes(needle);
      });
      shown += items.length;
      if (!items.length) return "";
      return `<div>
        <h3 class="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">${escapeHtml(g.name)}</h3>
        <div class="card divide-y divide-slate-100 text-sm">
          ${items
            .map(
              (o) => `<div class="flex items-start justify-between gap-4 px-4 py-3">
                <div class="min-w-0">
                  <p class="font-medium">${escapeHtml(o.label)}</p>
                  <p class="text-xs text-slate-500 mt-0.5">${escapeHtml(o.help)}</p>
                  <p class="font-mono text-xs text-slate-500 mt-1 break-all">${o.env.map(escapeHtml).join(" · ")}</p>
                </div>
                <div class="flex flex-col items-end gap-1 shrink-0 text-right">
                  ${on(o.enabled)}
                  <span class="text-xs text-slate-500">${optionDetail(o)}</span>
                </div>
              </div>`
            )
            .join("")}
        </div>
      </div>`;
    })
    .join("");
  document.getElementById("options").innerHTML = html;
  document.getElementById("options-empty").classList.toggle("hidden", shown > 0);
  document.getElementById("option-summary").textContent = t("{1} of {2} options are on.", [enabled, total]);
}

async function load() {
  const [system, options] = await Promise.all([api("/api/v1/admin/system"), api("/api/v1/admin/options")]);
  renderNotifications(system.notifications);
  renderBackups(system.backups);
  renderFleet(system.fleet);
  optionGroups = options;
  renderOptions();
}

document.getElementById("option-filter").addEventListener("input", renderOptions);
document.getElementById("option-state").addEventListener("change", renderOptions);

document.getElementById("test-notification").addEventListener("click", async (evt) => {
  const button = evt.currentTarget;
  const out = document.getElementById("test-result");
  button.disabled = true;
  out.textContent = "Sending...";
  try {
    const results = await api("/api/v1/admin/notifications/test", { method: "POST" });
    out.innerHTML = results
      .map((r) =>
        r.ok
          ? `<p class="text-green-700 dark:text-green-400">${escapeHtml(CHANNEL_NAMES[r.channel] || r.channel)}: sent.</p>`
          : `<p class="text-red-600 dark:text-red-400">${escapeHtml(CHANNEL_NAMES[r.channel] || r.channel)}: failed (${escapeHtml(r.error || "unknown error")}).</p>`
      )
      .join("");
  } catch (err) {
    out.textContent = "";
    toast(err.message, "error");
  } finally {
    button.disabled = false;
  }
});

document.getElementById("backup-now").addEventListener("click", async (evt) => {
  const button = evt.currentTarget;
  button.disabled = true;
  try {
    await api("/api/v1/admin/backups", { method: "POST" });
    toast("Backup written.", "success");
    await load();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    button.disabled = false;
  }
});

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("settings", user);
  if (!user.is_admin) {
    document.querySelector("main").innerHTML =
      `<p class="text-sm text-slate-500">Administrator access is required to view this page.</p>`;
    return;
  }
  try {
    await load();
  } catch (err) {
    const box = document.getElementById("error");
    box.textContent = err.message;
    box.classList.remove("hidden");
  }
})();
