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
})();
