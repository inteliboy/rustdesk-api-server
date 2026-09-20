let catalog = [];
let editingId = null;

function optionControl(spec, value) {
  const id = `opt-${spec.key}`;
  const current = value === undefined ? "" : String(value);
  if (spec.kind === "int") {
    return `<input id="${escapeHtml(id)}" data-key="${escapeHtml(spec.key)}" type="number" min="${Number(spec.minimum)}" max="${Number(spec.maximum)}"
      value="${escapeHtml(current)}" placeholder="not set" class="opt-control border border-slate-300 rounded px-2 py-1 text-sm w-28" />`;
  }
  const choices = spec.choices.map(
    (c) => `<option value="${escapeHtml(c)}" ${c === current ? "selected" : ""}>${escapeHtml(c)}</option>`
  );
  return `<select id="${escapeHtml(id)}" data-key="${escapeHtml(spec.key)}" class="opt-control border border-slate-300 rounded px-2 py-1 text-sm">
    <option value="">not set</option>${choices.join("")}</select>`;
}

function renderOptions(options) {
  const sections = [];
  for (const spec of catalog) {
    let section = sections.find((s) => s.title === spec.section);
    if (!section) {
      section = { title: spec.section, specs: [] };
      sections.push(section);
    }
    section.specs.push(spec);
  }
  document.getElementById("st-options").innerHTML = sections
    .map(
      (section) => `<div>
        <h3 class="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">${escapeHtml(section.title)}</h3>
        <div class="space-y-2">${section.specs
          .map(
            (spec) => `<div class="flex items-start justify-between gap-4 text-sm">
              <label for="opt-${escapeHtml(spec.key)}" class="min-w-0">
                <span class="font-medium">${escapeHtml(spec.label)}</span>
                <span class="block text-xs text-slate-500">${escapeHtml(spec.key)}${spec.help ? " &middot; " + escapeHtml(spec.help) : ""}</span>
              </label>
              ${optionControl(spec, options[spec.key])}
            </div>`
          )
          .join("")}</div>
      </div>`
    )
    .join("");
}

function collectOptions() {
  const options = {};
  document.querySelectorAll(".opt-control").forEach((el) => {
    if (el.value !== "") options[el.dataset.key] = el.value;
  });
  return options;
}

function openEditor(strategy) {
  editingId = strategy ? strategy.id : null;
  document.getElementById("editor-title").textContent = strategy ? "Edit strategy" : "New strategy";
  document.getElementById("st-name").value = strategy ? strategy.name : "";
  document.getElementById("st-description").value = strategy && strategy.description ? strategy.description : "";
  renderOptions(strategy ? strategy.options : {});
  document.getElementById("editor").classList.remove("hidden");
  document.getElementById("st-name").focus();
}

function closeEditor() {
  document.getElementById("editor").classList.add("hidden");
  editingId = null;
}

async function loadStrategies() {
  const strategies = await api("/api/v1/strategies");
  const rows = document.getElementById("strategy-rows");
  const empty = document.getElementById("empty-state");
  if (strategies.length === 0) {
    rows.innerHTML = "";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  rows.innerHTML = strategies
    .map(
      (s) => `<tr>
        <td class="px-4 py-3 font-medium">${escapeHtml(s.name)}</td>
        <td class="px-4 py-3 text-slate-500">${escapeHtml(s.description || "-")}</td>
        <td class="px-4 py-3 text-slate-500">${Object.keys(s.options).length}</td>
        <td class="px-4 py-3 text-slate-500">${s.device_count}</td>
        <td class="px-4 py-3 text-slate-500">${s.group_count}</td>
        <td class="px-4 py-3 text-right space-x-3 whitespace-nowrap">
          <button data-id="${s.id}" class="edit-strategy text-slate-500 hover:text-slate-900">Edit</button>
          <button data-id="${s.id}" class="delete-strategy text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Delete</button>
        </td>
      </tr>`
    )
    .join("");

  document.querySelectorAll(".edit-strategy").forEach((btn) =>
    btn.addEventListener("click", () => openEditor(strategies.find((s) => s.id === parseInt(btn.dataset.id, 10))))
  );
  document.querySelectorAll(".delete-strategy").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const strategy = strategies.find((s) => s.id === parseInt(btn.dataset.id, 10));
      const used = strategy.device_count + strategy.group_count;
      const warning = used
        ? ` It is assigned to ${strategy.device_count} device(s) and ${strategy.group_count} group(s); their clients are reset to their own defaults.`
        : "";
      if (!confirm("Delete this strategy?" + warning)) return;
      try {
        await api(`/api/v1/strategies/${strategy.id}`, { method: "DELETE" });
        toast("Strategy deleted.", "success");
        loadStrategies();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
}

document.getElementById("new-strategy-btn").addEventListener("click", () => openEditor(null));
document.getElementById("editor-close").addEventListener("click", closeEditor);
document.getElementById("st-cancel").addEventListener("click", closeEditor);

document.getElementById("st-save").addEventListener("click", async () => {
  const name = document.getElementById("st-name").value.trim();
  if (!name) {
    toast("A strategy needs a name.", "error");
    return;
  }
  const body = JSON.stringify({
    name,
    description: document.getElementById("st-description").value.trim() || null,
    options: collectOptions(),
  });
  try {
    if (editingId === null) {
      await api("/api/v1/strategies", { method: "POST", body });
    } else {
      await api(`/api/v1/strategies/${editingId}`, { method: "PUT", body });
    }
    toast("Strategy saved.", "success");
    closeEditor();
    loadStrategies();
  } catch (err) {
    toast(err.message, "error");
  }
});

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("strategies", user);
  if (!user.is_admin) {
    document.querySelector("main").innerHTML =
      `<p class="text-sm text-slate-500">Administrator access is required to view this page.</p>`;
    return;
  }
  try {
    catalog = await api("/api/v1/strategies/options");
    await loadStrategies();
  } catch (err) {
    toast(err.message, "error");
  }
})();
