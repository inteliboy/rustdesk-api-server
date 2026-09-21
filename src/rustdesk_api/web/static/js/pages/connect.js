// The commands are built here from the config string the server made. They are
// source-derived (the client's core_main.rs and custom_server.rs), so the page tells
// people to try one machine first.

const WARNINGS = {
  https_21114:
    "The client silently drops :21114 from an https:// API server address, so it would talk to port 443 instead. Use a host name on 443 (or another port), or plain http://host:21114.",
  api_without_scheme: "The API server needs http:// or https:// in front, or the client cannot use it.",
  no_key: "No key is set: clients will connect without checking the ID server's key.",
};

let current = null; // the last answer of /api/v1/connect/config
let os = "windows";

function shellQuote(text) {
  return `'${String(text).replace(/'/g, `'\\''`)}'`;
}

function scriptFor(kind, config) {
  if (kind === "windows") {
    return [
      "# In PowerShell, as administrator:",
      "winget install --id RustDesk.RustDesk -e",
      `& "$env:ProgramFiles\\RustDesk\\rustdesk.exe" --config "${config}"`,
      "",
      "# Optional: put the device in an address book (create the token on the Security page)",
      `& "$env:ProgramFiles\\RustDesk\\rustdesk.exe" --assign --token <TOKEN> --user_name <USERNAME> --address_book_name "<ADDRESS BOOK>"`,
    ].join("\n");
  }
  if (kind === "linux") {
    return [
      "# After installing the RustDesk package, as root:",
      `sudo rustdesk --config ${shellQuote(config)}`,
      "",
      "# Optional: put the device in an address book (create the token on the Security page)",
      `sudo rustdesk --assign --token <TOKEN> --user_name <USERNAME> --address_book_name "<ADDRESS BOOK>"`,
    ].join("\n");
  }
  return [
    "# After installing RustDesk.app, as root (untested on macOS - try one machine first):",
    `sudo /Applications/RustDesk.app/Contents/MacOS/RustDesk --config ${shellQuote(config)}`,
    "",
    "# Optional: put the device in an address book (create the token on the Security page)",
    `sudo /Applications/RustDesk.app/Contents/MacOS/RustDesk --assign --token <TOKEN> --user_name <USERNAME> --address_book_name "<ADDRESS BOOK>"`,
  ].join("\n");
}

function paintTabs() {
  document.querySelectorAll(".os-tab").forEach((tab) => {
    const active = tab.dataset.os === os;
    tab.setAttribute("aria-selected", String(active));
    tab.className = `os-tab px-3 py-1.5 rounded-md text-sm ${active ? "bg-brand-600 text-white" : "text-slate-600 hover:bg-slate-100"}`;
  });
}

function paint() {
  if (!current) return;
  document.getElementById("output").classList.remove("hidden");
  document.getElementById("config-string").value = current.config_string;
  document.getElementById("config-qr").src = current.qr_svg;
  document.getElementById("exe-name").value = current.exe_name;
  document.getElementById("script").value = scriptFor(os, current.config_string);
  const list = document.getElementById("warnings");
  const shown = current.warnings.filter((w) => WARNINGS[w]);
  list.classList.toggle("hidden", shown.length === 0);
  list.innerHTML = shown.map((w) => `<li>${escapeHtml(WARNINGS[w])}</li>`).join("");
  paintTabs();
}

function values() {
  return {
    id_server: document.getElementById("cfg-id").value.trim(),
    relay_server: document.getElementById("cfg-relay").value.trim(),
    api_server: document.getElementById("cfg-api").value.trim(),
    key: document.getElementById("cfg-key").value.trim(),
  };
}

let timer;
async function refresh() {
  const body = values();
  if (!body.id_server) {
    current = null;
    document.getElementById("output").classList.add("hidden");
    document.getElementById("warnings").classList.add("hidden");
    return;
  }
  try {
    current = await api("/api/v1/connect/config", { method: "POST", body: JSON.stringify(body) });
    paint();
  } catch (err) {
    toast(err.message, "error");
  }
}

for (const id of ["cfg-id", "cfg-relay", "cfg-api", "cfg-key"]) {
  document.getElementById(id).addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(refresh, 300);
  });
}

document.querySelectorAll(".os-tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    os = tab.dataset.os;
    paint();
  })
);

document.querySelectorAll("[data-copy]").forEach((button) =>
  button.addEventListener("click", async () => {
    const field = document.getElementById(button.dataset.copy);
    field.select();
    try {
      await navigator.clipboard.writeText(field.value);
      toast("Copied.", "success");
    } catch {
      // Not a secure context, or the browser refused: the text is selected, so Ctrl+C works.
      toast("Press Ctrl+C to copy the selected text.", "info");
    }
  })
);

// --- Windows installer (administrators): build on the server, or download a kit -------------

let installer = { status: null, releases: [] };
let polling = null;

function installerBody() {
  return {
    servers: values(),
    tag: document.getElementById("inst-tag").value,
    arch: document.getElementById("inst-arch").value,
    reset_settings: document.getElementById("inst-reset").checked,
  };
}

function fillReleases() {
  const select = document.getElementById("inst-tag");
  const firstStable = installer.releases.find((r) => !r.prerelease);
  select.innerHTML = installer.releases
    .map((r) => {
      let label = r.tag;
      // Only the release tagged "nightly" is the nightly build; GitHub flags a few old releases as pre-releases too.
      if (r.tag === "nightly") label = t("nightly {1} (pre-release)", [r.version]);
      else if (r.prerelease) label = t("{1} (pre-release)", [r.tag]);
      else if (r === firstStable) label = t("{1} (latest stable)", [r.tag]);
      return `<option value="${escapeHtml(r.tag)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  if (firstStable) select.value = firstStable.tag;
  fillArchitectures();
}

// Older releases have no ARM64 MSI: that choice is left out for them.
function fillArchitectures() {
  const release = installer.releases.find((r) => r.tag === document.getElementById("inst-tag").value);
  const arch = document.getElementById("inst-arch");
  const wanted = arch.value === "arm64" && (!release || release.architectures.includes("arm64")) ? "arm64" : "x64";
  const choices = [["x64", t("x64 (Intel/AMD, 64-bit)")]];
  if (!release || release.architectures.includes("arm64")) choices.push(["arm64", "ARM64"]);
  arch.innerHTML = choices.map(([value, label]) => `<option value="${value}">${escapeHtml(label)}</option>`).join("");
  arch.value = wanted;
}

function paintInstallerStatus() {
  const status = installer.status;
  const kit = document.getElementById("inst-kit");
  const build = document.getElementById("inst-build");
  const noReleases = installer.releases.length === 0;
  build.disabled = !status.available || noReleases;
  kit.disabled = noReleases;
  let text;
  if (noReleases) text = t("The list of RustDesk releases could not be loaded from GitHub. Try again in a moment.");
  else if (status.reason === "no_makensis")
    text = t("NSIS (makensis) is not installed on this server, so it cannot build the file itself. Use the build kit on a Windows computer that has NSIS.");
  else if (status.reason === "disabled")
    text = t("Building on the server is turned off (INSTALLER_BUILD_ENABLED). The build kit still works.");
  else if (status.signing) text = t("Ready to build here. The file is signed by the server (INSTALLER_SIGN_COMMAND).");
  else text = t("Ready to build here. The file is not signed: sign it yourself, or use the build kit on the computer that has your certificate.");
  document.getElementById("inst-status").textContent = text;
}

function showResult(text, html) {
  const box = document.getElementById("inst-result");
  box.classList.toggle("hidden", !text && !html);
  if (html) box.innerHTML = html;
  else box.textContent = text;
}

async function pollBuild(id) {
  try {
    const job = await api(`/api/v1/admin/installer/builds/${encodeURIComponent(id)}`);
    if (job.state === "done") {
      const size = `${(job.size / 1048576).toFixed(1)} MB`;
      const signed = job.signed ? t("signed") : t("not signed");
      const warnings = job.warnings.filter((w) => WARNINGS[w]).map((w) => `<br><span class="text-amber-700 dark:text-amber-400">${escapeHtml(t(WARNINGS[w]))}</span>`).join("");
      showResult(
        "",
        `<a class="text-link underline" href="/api/v1/admin/installer/builds/${encodeURIComponent(job.id)}/file" download>${escapeHtml(t("Download {1}", [job.filename]))}</a> (${escapeHtml(size)}, ${escapeHtml(signed)})${warnings}`
      );
      document.getElementById("inst-build").disabled = false;
      loadFiles().catch(() => {});
      return;
    }
    if (job.state === "failed") {
      showResult(job.message || t("The installer could not be built."));
      document.getElementById("inst-build").disabled = false;
      return;
    }
    showResult(job.state === "downloading" ? t("Downloading RustDesk from GitHub... {1}%", [job.progress]) : t("Building the installer..."));
    polling = setTimeout(() => pollBuild(id), 1000);
  } catch (err) {
    showResult(err.message);
    document.getElementById("inst-build").disabled = false;
  }
}

async function buildInstaller() {
  const build = document.getElementById("inst-build");
  if (!values().id_server) return toast(t("Enter the ID server first."), "error");
  build.disabled = true;
  showResult(t("Starting..."));
  try {
    const job = await api("/api/v1/admin/installer/builds", { method: "POST", body: JSON.stringify(installerBody()) });
    clearTimeout(polling);
    pollBuild(job.id);
  } catch (err) {
    showResult(err.message);
    build.disabled = false;
  }
}

// The kit is a POST (it carries the servers and needs the CSRF header), so it is fetched and
// saved from here rather than linked to.
async function downloadKit() {
  if (!values().id_server) return toast(t("Enter the ID server first."), "error");
  const button = document.getElementById("inst-kit");
  button.disabled = true;
  try {
    const headers = { "Content-Type": "application/json" };
    const csrf = getCookie("rd_csrf");
    if (csrf) headers["X-CSRF-Token"] = csrf;
    const response = await fetch("/api/v1/admin/installer/kit", {
      method: "POST",
      headers,
      credentials: "same-origin",
      body: JSON.stringify(installerBody()),
    });
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        message = (await response.json()).error.message || message;
      } catch {
        // Not a JSON error: the generic message stands.
      }
      throw new Error(message);
    }
    const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(await response.blob());
    link.download = match ? match[1] : "rustdesk-build-kit.zip";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    button.disabled = false;
  }
}

// --- the files the builder keeps on the server ---------------------------------------------

function fileSize(bytes) {
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

async function loadFiles() {
  const stored = await api("/api/v1/admin/installer/files");
  document.getElementById("files-where").textContent = t("Stored in {1}. Both kinds are made again when needed, so deleting them is safe.", [stored.directory]);
  const items = stored.items;
  document.getElementById("files-empty").classList.toggle("hidden", items.length > 0);
  document.getElementById("files-table").classList.toggle("hidden", items.length === 0);
  document.getElementById("files-clear-msi").disabled = !items.some((f) => f.kind === "msi");
  document.getElementById("files-clear-installer").disabled = !items.some((f) => f.kind === "installer");
  document.getElementById("file-rows").innerHTML = items
    .map((f) => {
      const name = escapeHtml(f.name);
      const kind = f.kind === "msi" ? t("RustDesk MSI (downloaded)") : t("Installer (built here)");
      const download =
        f.kind === "installer"
          ? `<a class="text-link underline mr-3" href="/api/v1/admin/installer/files/installer/${encodeURIComponent(f.name)}" download>${escapeHtml(t("Download"))}</a>`
          : "";
      return `<tr class="border-t border-slate-100">
        <td class="py-1.5 pr-3 whitespace-nowrap">${escapeHtml(kind)}</td>
        <td class="py-1.5 pr-3 font-mono text-xs break-all">${name}</td>
        <td class="py-1.5 pr-3 whitespace-nowrap">${escapeHtml(fileSize(f.size))}</td>
        <td class="py-1.5 pr-3 whitespace-nowrap text-slate-500">${escapeHtml(fmtDate(f.modified))}</td>
        <td class="py-1.5 whitespace-nowrap text-right">${download}<button type="button" class="file-delete text-red-600 hover:underline" data-kind="${escapeHtml(f.kind)}" data-name="${name}">${escapeHtml(t("Delete"))}</button></td>
      </tr>`;
    })
    .join("");
}

async function deleteFiles(kind, name, question) {
  if (!confirm(question)) return;
  try {
    const path = `/api/v1/admin/installer/files/${encodeURIComponent(kind)}${name ? `/${encodeURIComponent(name)}` : ""}`;
    const result = await api(path, { method: "DELETE" });
    toast(t("Deleted {1} file(s).", [result.deleted]), "success");
  } catch (err) {
    toast(err.message, "error");
  }
  loadFiles().catch((err) => toast(err.message, "error"));
}

async function loadInstaller() {
  document.getElementById("installer").classList.remove("hidden");
  document.getElementById("inst-tag").addEventListener("change", fillArchitectures);
  document.getElementById("inst-build").addEventListener("click", buildInstaller);
  document.getElementById("inst-kit").addEventListener("click", downloadKit);
  document.getElementById("files-clear-msi").addEventListener("click", () => deleteFiles("msi", null, t("Delete all downloaded RustDesk MSI files?")));
  document.getElementById("files-clear-installer").addEventListener("click", () => deleteFiles("installer", null, t("Delete all built installers?")));
  document.getElementById("file-rows").addEventListener("click", (evt) => {
    const button = evt.target.closest ? evt.target.closest(".file-delete") : null;
    if (button) deleteFiles(button.dataset.kind, button.dataset.name, t("Delete {1}?", [button.dataset.name]));
  });
  loadFiles().catch((err) => toast(err.message, "error"));
  try {
    const [status, releases] = await Promise.all([
      api("/api/v1/admin/installer"),
      api("/api/v1/admin/installer/releases").catch(() => []),
    ]);
    installer = { status, releases };
    fillReleases();
    paintInstallerStatus();
  } catch (err) {
    toast(err.message, "error");
  }
}

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("connect", user);
  try {
    const defaults = await api("/api/v1/connect");
    document.getElementById("cfg-id").value = defaults.id_server;
    document.getElementById("cfg-relay").value = defaults.relay_server;
    document.getElementById("cfg-api").value = defaults.api_server;
    document.getElementById("cfg-key").value = defaults.key;
    paintTabs();
    await refresh();
  } catch (err) {
    toast(err.message, "error");
  }
  if (user.is_admin) loadInstaller();
})();
