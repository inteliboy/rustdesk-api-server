// Server section of the Dashboard (administrators): what the server runs on, and
// CPU/memory history. dashboard.js calls initServerPanel() once it knows the
// viewer is an administrator; wrapped so its names stay out of the page scope.
// Data: GET /api/v1/admin/server?since=<t> - the first call returns the whole
// in-memory window, later ones only the readings newer than the last one seen.
// Charts are plain SVG (no charting library); colors come from Tailwind text
// classes via currentColor, so they follow the theme and accent.

(function () {
const state = { info: null, samples: [], hover: null, intervalMs: 10000, windowSec: 3600 };
let timer = null;

const KB = 1024;
function fmtBytes(n) {
  if (n == null) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(n), i = 0;
  while (value >= KB && i < units.length - 1) { value /= KB; i += 1; }
  return `${value >= 100 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

const fmtPercent = (v) => `${v.toFixed(v >= 10 ? 0 : 1)}%`;
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const clockSeconds = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

// Smallest "round" number (1, 2, 2.5, 5 x 10^k) that is >= value.
function niceCeil(value) {
  if (value <= 0) return 1;
  const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (step * magnitude >= value) return step * magnitude;
  }
  return 10 * magnitude;
}

// ---- charts ----

const CHART_HEIGHT = 190;
const MARGIN = { left: 52, right: 10, top: 8, bottom: 22 };

// spec: { el, series: [{ label, cls, value(sample) }], fixedMax?, minMax, fmtAxis, fmtValue }
function renderChart(spec) {
  const { el, series } = spec;
  const width = Math.max(280, el.clientWidth || 600);
  const plotW = width - MARGIN.left - MARGIN.right;
  const plotH = CHART_HEIGHT - MARGIN.top - MARGIN.bottom;
  const samples = state.samples;

  const latest = samples.length ? samples[samples.length - 1] : null;
  const tMax = latest ? latest.t : Date.now() / 1000;
  const tMin = tMax - state.windowSec;

  let yMax = spec.fixedMax;
  if (!yMax) {
    const peak = Math.max(0, ...samples.flatMap((s) => series.map((line) => line.value(s))));
    yMax = niceCeil(Math.max(spec.minMax, peak * 1.1));
  }
  const x = (t) => MARGIN.left + ((t - tMin) / (tMax - tMin)) * plotW;
  const y = (v) => MARGIN.top + plotH - (Math.min(v, yMax) / yMax) * plotH;

  const grid = [0, 1, 2, 3, 4]
    .map((i) => {
      const v = (yMax / 4) * i;
      const py = y(v);
      return `<line x1="${MARGIN.left}" x2="${width - MARGIN.right}" y1="${py}" y2="${py}" stroke="currentColor" stroke-width="1" class="text-slate-200"/>
        <text x="${MARGIN.left - 6}" y="${py + 4}" text-anchor="end" font-size="11" fill="currentColor" class="text-slate-500">${escapeHtml(spec.fmtAxis(v))}</text>`;
    })
    .join("");

  const ticks = [0, 1, 2, 3, 4]
    .map((i) => {
      const t = tMin + ((tMax - tMin) / 4) * i;
      const anchor = i === 0 ? "start" : i === 4 ? "end" : "middle";
      return `<text x="${x(t)}" y="${CHART_HEIGHT - 6}" text-anchor="${anchor}" font-size="11" fill="currentColor" class="text-slate-500">${clock(t)}</text>`;
    })
    .join("");

  const lines = series
    .map((line, index) => {
      const pts = samples.map((s) => `${x(s.t).toFixed(1)},${y(line.value(s)).toFixed(1)}`);
      if (!pts.length) return "";
      const area =
        index === 0
          ? `<polygon points="${x(samples[0].t).toFixed(1)},${y(0)} ${pts.join(" ")} ${x(latest.t).toFixed(1)},${y(0)}" fill="currentColor" fill-opacity="0.12" stroke="none"/>`
          : "";
      return `<g class="${line.cls}">${area}<polyline points="${pts.join(" ")}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"/></g>`;
    })
    .join("");

  // Hovering pins a reading; otherwise the legend shows the latest one.
  const shown = state.hover && samples.includes(state.hover) ? state.hover : latest;
  let marker = "";
  if (state.hover && shown) {
    const px = x(shown.t);
    marker =
      `<line x1="${px}" x2="${px}" y1="${MARGIN.top}" y2="${MARGIN.top + plotH}" stroke="currentColor" class="text-slate-400" stroke-dasharray="3 3"/>` +
      series
        .map((line) => `<g class="${line.cls}"><circle cx="${px}" cy="${y(line.value(shown))}" r="3.5" fill="currentColor"/></g>`)
        .join("");
  }

  const legend = series
    .map(
      (line) => `<span class="inline-flex items-center gap-1.5 ${line.cls}"><span class="inline-block w-3 h-0.5 bg-current"></span><span class="text-slate-600">${escapeHtml(line.label)}</span><span class="font-medium text-slate-900">${shown ? escapeHtml(spec.fmtValue(line.value(shown))) : "-"}</span></span>`
    )
    .join("");
  const when = shown ? (state.hover ? clockSeconds(shown.t) : "latest") : "";

  const empty = samples.length
    ? ""
    : `<text x="${width / 2}" y="${CHART_HEIGHT / 2}" text-anchor="middle" font-size="12" fill="currentColor" class="text-slate-500">Collecting data - the first reading arrives within ${state.intervalMs / 1000} s</text>`;

  el.innerHTML = `<div class="flex items-center gap-4 text-xs mb-1 flex-wrap">${legend}<span class="text-slate-400 ml-auto">${when}</span></div>
    <svg width="${width}" height="${CHART_HEIGHT}" viewBox="0 0 ${width} ${CHART_HEIGHT}" role="img" aria-label="${escapeHtml(series.map((s) => s.label).join(" and "))} over time" style="display:block">
      ${grid}${ticks}${lines}${marker}${empty}
    </svg>`;
}

function nearestSample(el, clientX) {
  const svg = el.querySelector("svg");
  if (!svg || !state.samples.length) return null;
  const rect = svg.getBoundingClientRect();
  const width = rect.width;
  const latest = state.samples[state.samples.length - 1].t;
  const tMin = latest - state.windowSec;
  const plotW = width - MARGIN.left - MARGIN.right;
  const t = tMin + ((clientX - rect.left - MARGIN.left) / plotW) * state.windowSec;
  let best = null;
  for (const s of state.samples) {
    if (best === null || Math.abs(s.t - t) < Math.abs(best.t - t)) best = s;
  }
  return best;
}

const MB = KB * KB;
const CHARTS = [
  {
    id: "chart-cpu",
    series: [
      { label: "API server", cls: "text-brand-600", value: (s) => s.process_cpu },
      { label: "Whole system", cls: "text-amber-500", value: (s) => s.system_cpu },
    ],
    minMax: 10,
    fmtAxis: (v) => `${Math.round(v * 10) / 10}%`,
    fmtValue: fmtPercent,
  },
  {
    id: "chart-process-memory",
    series: [{ label: "Resident memory", cls: "text-brand-600", value: (s) => s.process_memory / MB }],
    minMax: 32,
    fmtAxis: (v) => fmtBytes(v * MB),
    fmtValue: (v) => fmtBytes(v * MB),
  },
  {
    id: "chart-system-memory",
    series: [{ label: "In use", cls: "text-amber-500", value: (s) => s.system_memory_percent }],
    fixedMax: 100,
    fmtAxis: (v) => `${Math.round(v)}%`,
    fmtValue: fmtPercent,
  },
];

function drawCharts() {
  for (const chart of CHARTS) {
    const el = document.getElementById(chart.id);
    if (el) renderChart({ ...chart, el });
  }
}

function wireChartHover() {
  for (const chart of CHARTS) {
    const el = document.getElementById(chart.id);
    el.addEventListener("mousemove", (evt) => {
      const hit = nearestSample(el, evt.clientX);
      if (hit !== state.hover) {
        state.hover = hit;
        drawCharts();
      }
    });
    el.addEventListener("mouseleave", () => {
      state.hover = null;
      drawCharts();
    });
  }
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawCharts, 100);
  });
}

// ---- cards and details ----

const TONES = {
  brand: "border-l-brand-600",
  amber: "border-l-amber-500",
  green: "border-l-green-500",
  violet: "border-l-violet-500",
  slate: "border-l-slate-400",
  rose: "border-l-rose-500",
};

function statCard(label, value, tone, hint = "") {
  return `<div class="card p-4 border-l-4 ${TONES[tone]}">
    <p class="text-xs text-slate-500">${escapeHtml(label)}</p>
    <p class="text-2xl font-semibold mt-1">${escapeHtml(value)}</p>
    ${hint ? `<p class="text-xs text-slate-400 mt-0.5">${escapeHtml(hint)}</p>` : ""}
  </div>`;
}

function drawStats() {
  const { info, samples } = state;
  const last = samples.length ? samples[samples.length - 1] : null;
  const total = info.hardware.memory_total;
  document.getElementById("server-stats").innerHTML =
    statCard("API server CPU", last ? fmtPercent(last.process_cpu) : "-", "brand", "of the whole machine") +
    statCard("API server memory", last ? fmtBytes(last.process_memory) : "-", "brand", total && last ? `${((last.process_memory / total) * 100).toFixed(1)}% of RAM` : "") +
    statCard("System CPU", last ? fmtPercent(last.system_cpu) : "-", "amber") +
    statCard("System memory", last ? fmtPercent(last.system_memory_percent) : "-", "amber", total ? `of ${fmtBytes(total)}` : "") +
    statCard("API server uptime", fmtDuration(info.process.uptime_seconds), "green") +
    statCard("Threads", last ? String(last.threads) : "-", "violet");
}

function row(label, value) {
  const shown = value === null || value === undefined || value === "" ? "-" : value;
  return `<div class="flex justify-between gap-6 px-4 py-2 text-sm">
    <dt class="text-slate-500 shrink-0">${escapeHtml(label)}</dt>
    <dd class="text-right break-words min-w-0">${escapeHtml(shown)}</dd>
  </div>`;
}

function panel(title, rows) {
  return `<section class="card">
    <h2 class="px-4 pt-4 pb-2 text-sm font-semibold">${escapeHtml(title)}</h2>
    <dl class="divide-y divide-slate-100">${rows.join("")}</dl>
  </section>`;
}

function drawInfo() {
  const { hardware: hw, system: sys, software: sw, database: db, process: proc } = state.info;
  const cores = hw.cpu_physical_cores
    ? `${hw.cpu_physical_cores} cores / ${hw.cpu_logical_cores} threads`
    : `${hw.cpu_logical_cores} threads`;
  const disk = hw.data_disk
    ? `${fmtBytes(hw.data_disk.free)} free of ${fmtBytes(hw.data_disk.total)}`
    : null;
  const packages = Object.entries(sw.packages).map(([name, version]) => row(name, version));

  document.getElementById("info").innerHTML =
    panel("Hardware", [
      row("Processor", hw.cpu_model),
      row("Cores", cores),
      row("Max clock", hw.cpu_max_mhz ? `${(hw.cpu_max_mhz / 1000).toFixed(2)} GHz` : null),
      row("Architecture", hw.architecture),
      row("Memory", fmtBytes(hw.memory_total)),
      row("Swap / page file", hw.swap_total ? fmtBytes(hw.swap_total) : "none"),
      row("Disk holding the data", disk),
    ]) +
    panel("Operating system", [
      row("System", sys.os),
      row("Version", sys.os_version),
      row("Kernel", sys.kernel),
      row("Host name", sys.hostname),
      row("Running in a container", sys.container ? "Yes" : "No"),
      row("Machine up since", fmtDate(sys.boot_time)),
      row("Time zone", sys.utc_offset),
    ]) +
    panel("API server", [
      row("Version", sw.app_version),
      row("Python", `${sw.python_implementation} ${sw.python}`),
      row("SQLite", sw.sqlite),
      // Two different questions: this server's own certificate (usually "No" behind a
      // reverse proxy or a NAS's built-in one, which is fine), and this browser's connection.
      row("Your connection", window.location.protocol === "https:" ? `Encrypted (HTTPS, ${window.location.host})` : "Not encrypted (HTTP)"),
      row("TLS inside the API server", sw.tls ? "Yes" : "No (plain HTTP)"),
      row("Process ID", proc.pid),
      row("Started", fmtDate(proc.started_at)),
      row("Uptime", fmtDuration(proc.uptime_seconds)),
    ]) +
    panel("Database and libraries", [
      row("Engine", db.engine),
      row("Database size", db.size_bytes != null ? fmtBytes(db.size_bytes) : null),
      row("Write-ahead log", db.wal_size_bytes != null ? fmtBytes(db.wal_size_bytes) : null),
      ...packages,
    ]);
}

// ---- loading ----

function showError(message) {
  const box = document.getElementById("error");
  box.textContent = message || "";
  box.classList.toggle("hidden", !message);
}

async function refresh() {
  const last = state.samples.length ? state.samples[state.samples.length - 1].t : null;
  try {
    const data = await api("/api/v1/admin/server" + (last ? `?since=${last}` : ""));
    showError("");
    state.info = data;
    state.intervalMs = Math.max(5, data.interval_seconds) * 1000;
    state.windowSec = data.history_minutes * 60;
    state.samples = state.samples.concat(data.samples);
    const cutoff = (state.samples.length ? state.samples[state.samples.length - 1].t : 0) - state.windowSec;
    state.samples = state.samples.filter((s) => s.t >= cutoff);
    if (state.hover && !state.samples.includes(state.hover)) state.hover = null;
    drawStats();
    drawInfo();
    drawCharts();
    document.getElementById("status-line").textContent =
      `One reading every ${data.interval_seconds} s, last ${data.history_minutes} min kept. Updated ${clockSeconds(Date.now() / 1000)}.`;
  } catch (err) {
    if (err.status === 403) {
      return false;
    }
    showError("Failed to load server status: " + err.message);
  }
  return true;
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    if (document.hidden) return; // resumed by visibilitychange
    if (await refresh()) schedule();
  }, state.intervalMs);
}

let started = false;
async function initServerPanel() {
  if (started) return;
  started = true;
  wireChartHover();
  // Shown before the first draw: charts are sized from their container's width.
  const section = document.getElementById("server-section");
  section.classList.remove("hidden");
  if (await refresh()) schedule();
  else section.classList.add("hidden");
  document.addEventListener("visibilitychange", async () => {
    if (!document.hidden && (await refresh())) schedule();
  });
}

window.initServerPanel = initServerPanel;

})();
