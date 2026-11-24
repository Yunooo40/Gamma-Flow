"use strict";

// Open-GEX dashboard. API data is only ever written with textContent.

const REFRESH_MS = Number(document.body.dataset.refreshSeconds || 300) * 1000;
const COLORS = {
  positive: "#22c55e",
  negative: "#ef4444",
  spot: "#e5e7eb",
  zeroGamma: "#c4b5fd",
  grid: "#1c2027",
  text: "#9ca3af",
};

const state = {
  ticker: document.querySelector("#ticker-group button")?.dataset.ticker || "SPY",
  expiry: "0dte",
  metric: "gex",
  data: null,
};
let requestSeq = 0;

const $ = (id) => document.getElementById(id);

function fmtMoney(value) {
  if (value == null || !Number.isFinite(value)) return "–";
  const abs = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(0)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

function fmtPrice(value) {
  return value == null || !Number.isFinite(value) ? "–" : value.toFixed(2);
}

function fmtDistance(level, spot) {
  const pct = (level / spot - 1) * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}% vs spot`;
}

function setPressed(groupId, key, value) {
  for (const button of $(groupId).querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset[key] === value));
  }
}

function setLoading(on) {
  $("loading").style.display = on ? "inline-flex" : "none";
  $("loading-text").textContent =
    state.expiry === "all" ? "Loading… (first load of All can take a few seconds)" : "Loading…";
}

function showMessages(errors, warnings) {
  const list = $("banner-list");
  list.replaceChildren();
  const items = [
    ...errors.map((text) => [text, "text-red-400"]),
    ...warnings.map((text) => [text, "text-amber-300"]),
  ];
  for (const [text, cls] of items) {
    const li = document.createElement("li");
    li.textContent = text;
    li.className = cls;
    list.appendChild(li);
  }
  $("banner").classList.toggle("hidden", items.length === 0);
}

function setLevel(id, level, spot, missingText) {
  $(`kpi-${id}`).textContent = fmtPrice(level);
  $(`kpi-${id}-sub`).textContent = level == null || spot == null ? missingText : fmtDistance(level, spot);
}

function renderHeader(d) {
  const badge = $("source-badge");
  const delayed = d.source !== "massive" || d.timeframe !== "REAL-TIME";
  badge.textContent = d.source === "massive" ? `Massive · ${d.timeframe ?? "unknown"}` : "yfinance · delayed";
  badge.className = `badge ${delayed ? "badge-delayed" : "badge-live"}`;

  const time = new Date(d.fetched_at).toLocaleTimeString("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  $("updated").textContent = `Updated ${time} ET${d.cached ? " (cached)" : ""}`;
}

function renderKpis(d) {
  $("kpi-spot").textContent = fmtPrice(d.spot);
  $("kpi-spot-sub").textContent =
    d.expirations.length === 1 ? `Expiry ${d.expirations[0]}` : `${d.expirations.length} expiries`;
  setLevel("zg", d.levels.zero_gamma, d.spot, "Out of range");
  setLevel("cw", d.levels.call_wall, d.spot, "–");
  setLevel("pw", d.levels.put_wall, d.spot, "–");

  const net = d.totals.net_gex;
  const gex = $("kpi-gex");
  gex.textContent = fmtMoney(net);
  gex.style.color = net == null ? "" : net >= 0 ? COLORS.positive : COLORS.negative;
  $("kpi-gex-sub").textContent = net == null ? "–" : net >= 0 ? "Positive gamma" : "Negative gamma";
}

function levelLines(d) {
  const { min, max } = d.display_range;
  return [
    { y: d.spot, label: "Spot", color: COLORS.spot, dash: "dot", width: 1.5 },
    { y: d.levels.zero_gamma, label: "Zero Gamma", color: COLORS.zeroGamma, dash: "dot", width: 1.5 },
    { y: d.levels.call_wall, label: "Call Wall", color: COLORS.positive, dash: "solid", width: 1 },
    { y: d.levels.put_wall, label: "Put Wall", color: COLORS.negative, dash: "solid", width: 1 },
  ].filter((line) => line.y != null && line.y >= min && line.y <= max);
}

function renderChart() {
  const d = state.data;
  const key = state.metric === "gex" ? "net_gex" : "net_vex";
  const label = state.metric === "gex" ? "Net GEX ($ per 1% move)" : "Net Vanna ($ per 1 vol point)";
  $("chart-title").textContent = `${state.metric === "gex" ? "Net GEX" : "Net Vanna"} by strike`;

  const values = d.strikes.map((row) => row[key] ?? 0);
  const trace = {
    type: "bar",
    orientation: "h",
    x: values,
    y: d.strikes.map((row) => row.strike),
    marker: { color: values.map((v) => (v >= 0 ? COLORS.positive : COLORS.negative)) },
    customdata: d.strikes.map((row) => [
      fmtMoney(row.call_gex),
      fmtMoney(row.put_gex),
      fmtMoney(row.net_gex),
      fmtMoney(row.net_vex),
    ]),
    hovertemplate:
      "<b>Strike %{y}</b><br>Call GEX: %{customdata[0]}<br>Put GEX: %{customdata[1]}" +
      "<br>Net GEX: %{customdata[2]}<br>Net Vanna: %{customdata[3]}<extra></extra>",
  };

  const lines = levelLines(d);
  const layout = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: COLORS.text, size: 11, family: "ui-monospace, SFMono-Regular, Menlo, monospace" },
    margin: { l: 56, r: 16, t: 8, b: 40 },
    bargap: 0.15,
    showlegend: false,
    hoverlabel: { bgcolor: "#111318", bordercolor: "#374151", font: { color: "#e5e7eb" } },
    xaxis: {
      title: { text: label },
      gridcolor: COLORS.grid,
      zerolinecolor: "#4b5563",
      exponentformat: "B",
    },
    yaxis: {
      type: "linear",
      range: [d.display_range.min, d.display_range.max],
      title: { text: "Strike" },
      gridcolor: COLORS.grid,
    },
    shapes: lines.map((line) => ({
      type: "line",
      xref: "paper",
      x0: 0,
      x1: 1,
      yref: "y",
      y0: line.y,
      y1: line.y,
      line: { color: line.color, width: line.width, dash: line.dash },
    })),
    annotations: lines.map((line) => ({
      xref: "paper",
      x: 1,
      xanchor: "right",
      yref: "y",
      y: line.y,
      yanchor: "bottom",
      text: `${line.label} ${line.y.toFixed(2)}`,
      showarrow: false,
      font: { color: line.color, size: 10 },
    })),
  };
  Plotly.react("chart", [trace], layout, { responsive: true, displaylogo: false });
}

function render() {
  const d = state.data;
  renderHeader(d);
  renderKpis(d);
  renderChart();
}

function clearDisplay() {
  state.data = null;
  for (const id of ["kpi-spot", "kpi-zg", "kpi-cw", "kpi-pw", "kpi-gex"]) $(id).textContent = "–";
  for (const id of ["kpi-spot-sub", "kpi-zg-sub", "kpi-cw-sub", "kpi-pw-sub", "kpi-gex-sub"]) $(id).textContent = "";
  $("source-badge").textContent = "–";
  $("updated").textContent = "–";
  Plotly.purge("chart");
}

async function load() {
  const seq = ++requestSeq;
  setLoading(true);
  try {
    const params = new URLSearchParams({ ticker: state.ticker, expiry: state.expiry });
    const response = await fetch(`/api/gex?${params}`);
    const body = await response.json().catch(() => ({}));
    if (seq !== requestSeq) return; // a newer request superseded this one
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    state.data = body;
    render();
    showMessages([], body.warnings || []);
  } catch (error) {
    if (seq !== requestSeq) return;
    // Keep the last data only if it matches the current selection.
    if (state.data && (state.data.ticker !== state.ticker || state.data.expiry_filter !== state.expiry)) {
      clearDisplay();
    }
    showMessages([error.message || "Request failed"], []);
  } finally {
    if (seq === requestSeq) setLoading(false);
  }
}

$("ticker-group").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.ticker = button.dataset.ticker;
  setPressed("ticker-group", "ticker", state.ticker);
  load();
});

$("expiry-group").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.expiry = button.dataset.expiry;
  setPressed("expiry-group", "expiry", state.expiry);
  load();
});

$("metric-group").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.metric = button.dataset.metric;
  setPressed("metric-group", "metric", state.metric);
  if (state.data) renderChart();
});

$("refresh-btn").addEventListener("click", load);

load();
setInterval(load, REFRESH_MS);
