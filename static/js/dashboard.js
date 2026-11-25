"use strict";

// Open-GEX dashboard. API data is only ever written with textContent.

const REFRESH_MS = Number(document.body.dataset.refreshSeconds || 300) * 1000;
const COLORS = {
  positive: "#22c55e",
  negative: "#ef4444",
  spot: "#e5e7eb",
  zeroGamma: "#c4b5fd",
  expectedMove: "#fde047",
  grid: "#1c2027",
  text: "#9ca3af",
};
const LABEL_HEIGHT_PX = 20; // a level label (10 px font + padding): two closer than this overlap
// ColorBrewer RdYlGn (plotly.js has no such named scale): red for negative VEX, green for positive.
const RDYLGN = ["#a50026", "#d73027", "#f46d43", "#fdae61", "#fee08b", "#ffffbf", "#d9ef8b", "#a6d96a", "#66bd63", "#1a9850", "#006837"]
  .map((color, i, all) => [i / (all.length - 1), color]);

const state = {
  ticker: document.querySelector("#ticker-group button")?.dataset.ticker || "SPY",
  expiry: "0dte",
  metric: "gex",
  data: null,
};
let requestSeq = 0;
let vannaSeq = 0;

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

function fmtPct(value) {
  return value == null || !Number.isFinite(value) ? "–" : `${(value * 100).toFixed(1)}%`;
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

function setTotal(id, value, [positiveText, negativeText]) {
  const el = $(`kpi-${id}`);
  el.textContent = fmtMoney(value);
  el.style.color = value == null ? "" : value >= 0 ? COLORS.positive : COLORS.negative;
  $(`kpi-${id}-sub`).textContent = value == null ? "–" : value >= 0 ? positiveText : negativeText;
}

function renderHeader(d) {
  const badge = $("source-badge");
  const delayed = d.source === "yfinance" || d.timeframe !== "REAL-TIME";
  const names = { alpaca: "Alpaca", massive: "Massive" };
  badge.textContent = d.source in names ? `${names[d.source]} · ${d.timeframe ?? "unknown"}` : "yfinance · delayed";
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

  setTotal("gex", d.totals.net_gex, ["Positive gamma", "Negative gamma"]);
  setTotal("vex", d.totals.net_vex, ["Positive vanna", "Negative vanna"]);
  // total_charm is the dealers' hedge flow: positive = they buy the underlying as time passes.
  setTotal("charm", d.total_charm, ["Dealers buy into close", "Dealers sell into close"]);
}

function renderVolatility(d) {
  const iv = d.atm_iv;
  const rv = d.realized_vol;
  $("vol-iv").textContent = fmtPct(iv);
  $("vol-rv").textContent = fmtPct(rv);
  const spread = iv - rv;
  $("vol-ratio").textContent =
    iv == null || rv == null || !(rv > 0)
      ? "–"
      : `×${(iv / rv).toFixed(2)} (${spread >= 0 ? "+" : ""}${(spread * 100).toFixed(1)} pts)`;
  $("vol-em").textContent =
    d.expected_move == null
      ? "–"
      : `±$${d.expected_move.toFixed(2)} (${fmtPrice(d.em_lower)} – ${fmtPrice(d.em_upper)})`;
}

function levelLines(d) {
  const { min, max } = d.display_range;
  return [
    { y: d.spot, label: "Spot", color: COLORS.spot, dash: "dot", width: 1.5 },
    { y: d.levels.zero_gamma, label: "Zero Gamma", color: COLORS.zeroGamma, dash: "dot", width: 1.5 },
    { y: d.levels.call_wall, label: "Call Wall", color: COLORS.positive, dash: "solid", width: 1 },
    { y: d.levels.put_wall, label: "Put Wall", color: COLORS.negative, dash: "solid", width: 1 },
    { y: d.em_upper, label: "EM high", color: COLORS.expectedMove, dash: "dot", width: 1, em: true },
    { y: d.em_lower, label: "EM low", color: COLORS.expectedMove, dash: "dot", width: 1, em: true },
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
  // shortcut: screen size is read at each render, not on resize; add a resize listener if rotation matters.
  const small = window.innerWidth < 768;
  const margin = { l: 56, r: small ? 8 : 16, t: 8, b: 40 };
  // The move often lands next to a wall or Zero Gamma: an EM label that would cover theirs goes on the left.
  const pxPerDollar = ($("chart").clientHeight - margin.t - margin.b) / (d.display_range.max - d.display_range.min);
  const crowded = (em) => lines.some((line) => !line.em && Math.abs(line.y - em.y) * pxPerDollar < LABEL_HEIGHT_PX);
  const layout = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: COLORS.text, size: 11, family: "ui-monospace, SFMono-Regular, Menlo, monospace" },
    margin,
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
    annotations: lines.map((line) => {
      const left = line.em && crowded(line);
      return {
        xref: "paper",
        x: left ? 0.02 : 0.98,
        xanchor: left ? "left" : "right",
        yref: "y",
        y: line.y,
        yanchor: "bottom",
        text: `${line.label} ${line.y.toFixed(2)}`,
        showarrow: false,
        font: { color: line.color, size: 10 },
        bgcolor: "rgba(15, 23, 42, 0.85)", // stays readable over bars of the same color
        borderpad: 3,
      };
    }),
  };
  Plotly.react("chart", [trace], layout, {
    responsive: true,
    displaylogo: false,
    displayModeBar: small ? false : "hover", // on phones the toolbar covers the bars
  });
}

function render() {
  const d = state.data;
  renderHeader(d);
  renderKpis(d);
  renderVolatility(d);
  renderChart();
}

function clearDisplay() {
  state.data = null;
  for (const id of ["kpi-spot", "kpi-zg", "kpi-cw", "kpi-pw", "kpi-gex", "kpi-vex", "kpi-charm", "vol-iv", "vol-rv", "vol-ratio", "vol-em"]) {
    $(id).textContent = "–";
  }
  for (const id of ["kpi-spot-sub", "kpi-zg-sub", "kpi-cw-sub", "kpi-pw-sub", "kpi-gex-sub", "kpi-vex-sub", "kpi-charm-sub"]) {
    $(id).textContent = "";
  }
  for (const id of ["kpi-gex", "kpi-vex", "kpi-charm"]) $(id).style.color = ""; // no green / red dash
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

function hideVannaSurface() {
  vannaSeq++; // drops a surface still being computed for the previous selection
  $("vanna-3d-container").hidden = true;
  $("vanna-3d-btn").setAttribute("aria-expanded", "false");
  Plotly.purge("vanna-3d");
}

function renderVannaSurface(surface) {
  const small = window.innerWidth < 768;
  const axis = (title, extra = {}) => ({
    title: { text: title },
    color: COLORS.text,
    gridcolor: "#374151",
    zerolinecolor: "#4b5563",
    backgroundcolor: "rgba(0,0,0,0)",
    ...extra,
  });
  const trace = {
    type: "surface",
    x: surface.x,
    y: surface.y,
    z: surface.z,
    text: surface.z.map((row) => row.map(fmtMoney)),
    colorscale: RDYLGN,
    cmid: 0,
    // Phones: horizontal bar under the scene, to keep the width for it. Desktop: below the toolbar.
    colorbar: small
      ? { orientation: "h", x: 0.5, xanchor: "center", y: 0, yanchor: "bottom", len: 0.9, thickness: 10, exponentformat: "B" }
      : { len: 0.6, y: 0.38, thickness: 12, exponentformat: "B" },
    hovertemplate: "Spot %{x:.2f}<br>IV ×%{y:.2f}<br>Net VEX %{text}<extra></extra>",
  };
  const layout = {
    paper_bgcolor: "rgba(0,0,0,0)",
    font: { color: COLORS.text, size: 11, family: "ui-monospace, SFMono-Regular, Menlo, monospace" },
    margin: { l: 0, r: 0, t: 0, b: 0 },
    scene: {
      domain: small ? { x: [0, 1], y: [0.14, 1] } : { x: [0, 1], y: [0, 1] },
      xaxis: axis("Spot Price"),
      yaxis: axis("IV Shock", { tickformat: ".2f" }),
      zaxis: axis("Net VEX", { exponentformat: "B" }),
      // A narrow screen sees less of the scene: the camera steps back so it fits the width.
      camera: { eye: small ? { x: 2.1, y: -2.3, z: 1.2 } : { x: 1.5, y: -1.6, z: 0.8 } },
    },
  };
  Plotly.react("vanna-3d", [trace], layout, {
    responsive: true,
    displaylogo: false,
    displayModeBar: small ? false : "hover", // on phones the toolbar covers the plot
  });
}

async function loadVannaSurface() {
  const seq = ++vannaSeq;
  const { ticker, expiry } = state;
  $("vanna-3d-container").hidden = false;
  $("vanna-3d-btn").setAttribute("aria-expanded", "true");
  $("vanna-3d-title").textContent = `Net VEX surface · ${ticker} ${expiry === "all" ? "All" : "0DTE"}`;
  $("vanna-3d-error").hidden = true;
  $("vanna-3d-loading").style.display = "inline-flex";
  try {
    const params = new URLSearchParams({ ticker, expiry });
    const response = await fetch(`/api/vanna-surface?${params}`);
    const body = await response.json().catch(() => ({}));
    if (seq !== vannaSeq) return; // a newer request or a selection change superseded this one
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    renderVannaSurface(body);
  } catch (error) {
    if (seq !== vannaSeq) return;
    Plotly.purge("vanna-3d");
    $("vanna-3d-error").textContent = error.message || "Request failed";
    $("vanna-3d-error").hidden = false;
  } finally {
    if (seq === vannaSeq) $("vanna-3d-loading").style.display = "none";
  }
}

$("ticker-group").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.ticker = button.dataset.ticker;
  setPressed("ticker-group", "ticker", state.ticker);
  hideVannaSurface(); // never show a surface computed for another selection
  load();
});

$("expiry-group").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.expiry = button.dataset.expiry;
  setPressed("expiry-group", "expiry", state.expiry);
  hideVannaSurface();
  load();
});

$("vanna-3d-btn").addEventListener("click", loadVannaSurface);

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
