/* CAN 回放与功能分析 — 前端逻辑 */
"use strict";

// ---------------------------------------------------------------------------
// 全局状态
// ---------------------------------------------------------------------------
const state = {
  status: null,          // 项目摘要
  messages: [],          // 报文列表（含信号）
  signals: [],           // 信号元数据
  selected: new Set(),   // 已选信号名
  analysis: null,        // 功能分析结果
  t: 0,                  // 当前光标时间
  playing: false,
  speed: 1,
  t0: 0, t1: 1,          // 当前可视时间范围
  chart: null, stateChart: null,
  traceRange: null,      // 已缓存的 trace 时间范围 [a,b]
  traceRows: [],
  valueMode: "selected",  // 右侧信号值面板：默认显示图形中已勾选信号（从上到下）
};

const COLORS = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#06b6d4",
  "#ec4899", "#84cc16", "#f97316", "#14b8a6", "#8b5cf6", "#eab308"];

const STRIP_H = 58;      // 每个信号条带高度
const GAP = 3;

const $ = (id) => document.getElementById(id);

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

function fmtTime(t) { return `${(t ?? 0).toFixed(3)} s`; }
function numFmt(v) {
  if (v == null) return "";
  const a = Math.abs(v);
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  return v.toFixed(2);
}
function colorOf(i) { return COLORS[i % COLORS.length]; }
function shortName(name) { return String(name).length > 18 ? String(name).slice(0, 17) + "…" : String(name); }
function escapeHtml(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

// ---------------------------------------------------------------------------
// 加载 / 刷新
// ---------------------------------------------------------------------------
async function refreshStatus() {
  state.status = await api("/api/status");
  const s = state.status;
  const parts = [];
  if (s.dbc) parts.push(`DBC: ${s.dbc} (${s.message_count} 报文 / ${s.signal_count} 信号)`);
  if (s.log) parts.push(`日志: ${s.log} (${s.frame_count} 帧)`);
  if (s.spec) parts.push(`规格: ${s.spec}`);
  $("status").textContent = parts.length ? parts.join("  ·  ") : "未加载数据";
  if (s.start != null && s.end != null) {
    state.t0 = s.start; state.t1 = s.end;
    if (state.t < s.start || state.t > s.end) state.t = s.start;
    $("rangeLabel").textContent = `范围 ${fmtTime(s.start)} ~ ${fmtTime(s.end)}`;
  }
}

async function refreshMessages() {
  state.messages = await api("/api/messages");
  state.signals = await api("/api/signals");
  $("signalCount").textContent = `(${state.signals.length})`;
  populateDatalist();
  renderSignalTree();
}

async function refreshAnalysis() {
  try { state.analysis = await api("/api/analysis"); }
  catch (_) { state.analysis = null; }
  renderFunctionList();
}

async function refreshAll() {
  await refreshStatus();
  await refreshMessages();
  await refreshAnalysis();
  renderCharts();
  renderTrace();
  scheduleValues();
}

async function loadSample() {
  await api("/api/load/sample", { method: "POST" });
  await refreshAll();
}

async function uploadFile(endpoint, file) {
  const fd = new FormData();
  fd.append("file", file);
  await api(endpoint, { method: "POST", body: fd });
  await refreshAll();
}

// ---------------------------------------------------------------------------
// 信号树
// ---------------------------------------------------------------------------
function renderSignalTree() {
  const tree = $("signalTree");
  const q = $("signalSearch").value.trim().toLowerCase();
  tree.innerHTML = "";
  const selectedArr = [...state.selected];

  for (const msg of state.messages) {
    const sigs = msg.signals.filter(s => !q || s.name.toLowerCase().includes(q) || msg.name.toLowerCase().includes(q));
    if (!sigs.length) continue;
    const mDiv = document.createElement("div");
    mDiv.className = "tree-msg";
    mDiv.textContent = `${msg.name} (0x${msg.frame_id.toString(16).toUpperCase()})`;
    tree.appendChild(mDiv);
    for (const s of sigs) {
      const row = document.createElement("label");
      row.className = "tree-sig";
      const idx = selectedArr.indexOf(s.name);
      const color = colorOf(idx >= 0 ? idx : state.selected.size);
      row.innerHTML = `<input type="checkbox" ${idx >= 0 ? "checked" : ""} />
        <span class="swatch" style="background:${color}"></span>
        <span>${escapeHtml(s.name)}</span><span class="unit">${escapeHtml(s.unit || "")}</span>`;
      row.querySelector("input").addEventListener("change", () => {
        if (row.querySelector("input").checked) state.selected.add(s.name);
        else state.selected.delete(s.name);
        renderCharts();
      });
      tree.appendChild(row);
    }
  }
  if (!tree.childNodes.length) tree.innerHTML = '<div class="muted" style="padding:6px">无匹配信号</div>';
}

$("signalSearch").addEventListener("input", renderSignalTree);

function populateDatalist() {
  const dl = $("signalList");
  dl.innerHTML = "";
  for (const s of state.signals) {
    const o = document.createElement("option");
    o.value = s.name;
    dl.appendChild(o);
  }
}

function addSignalByName() {
  const input = $("signalAdd");
  const name = input.value.trim();
  if (!name) return;
  if (!signalMeta(name)) { input.style.outline = "1px solid var(--red)"; setTimeout(() => input.style.outline = "", 800); return; }
  state.selected.add(name);
  input.value = "";
  renderSignalTree();
  renderCharts();
}
$("btnAddSignal").addEventListener("click", addSignalByName);
$("signalAdd").addEventListener("keydown", e => { if (e.key === "Enter") addSignalByName(); });

// 右侧信号值面板
let valueTimer = null;
function scheduleValues() {
  clearTimeout(valueTimer);
  valueTimer = setTimeout(renderValues, 80);
}
async function renderValues() {
  const box = $("valueList");
  $("valueTime").textContent = `@ ${fmtTime(state.t)}`;
  const sel = state.valueMode === "all" ? state.signals.map(s => s.name) : [...state.selected];
  if (!sel.length) {
    box.innerHTML = '<div class="muted" style="padding:6px">加载数据后显示信号值</div>';
    return;
  }
  const selIndex = new Map([...state.selected].map((n, i) => [n, i]));
  try {
    const vals = await api(`/api/values?signals=${encodeURIComponent(sel.join(","))}&t=${state.t}`);
    let html = "";
    sel.forEach((name) => {
      const m = signalMeta(name);
      const v = vals[name];
      const idx = selIndex.get(name);
      const color = idx != null ? colorOf(idx) : "#4b5563";
      const cls = idx != null ? "vrow" : "vrow muted";
      html += `<div class="${cls}">
        <span class="swatch" style="background:${color}"></span>
        <span class="vname" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
        <span class="vval">${escapeHtml(v == null ? "—" : String(v))}</span>
        <span class="vunit">${escapeHtml(m?.unit || "")}</span>
      </div>`;
    });
    box.innerHTML = html;
  } catch (_) {
    box.innerHTML = '<div class="muted" style="padding:6px">—</div>';
  }
}
$("vmAll").addEventListener("click", () => { state.valueMode = "all"; $("vmAll").classList.add("on"); $("vmSel").classList.remove("on"); renderValues(); });
$("vmSel").addEventListener("click", () => { state.valueMode = "selected"; $("vmSel").classList.add("on"); $("vmAll").classList.remove("on"); renderValues(); });

// ---------------------------------------------------------------------------
// 图表（每信号一行、独立坐标轴的条带式布局，对标 CANoe Graphics）
// ---------------------------------------------------------------------------
function initCharts() {
  if (!window.echarts) { alert("ECharts 未加载"); return; }
  state.chart = echarts.init($("chart"));
  state.stateChart = echarts.init($("stateChart"));
  state.chart.group = "cangroup";
  state.stateChart.group = "cangroup";
  echarts.connect("cangroup");
  window.addEventListener("resize", () => { state.chart.resize(); state.stateChart.resize(); });

  let zoomTimer = null;
  const onZoom = (params) => {
    const b = (params.batch || [params])[0] || {};
    if (b.startValue != null && b.endValue != null) { state.t0 = b.startValue; state.t1 = b.endValue; }
    clearTimeout(zoomTimer);
    zoomTimer = setTimeout(() => renderTrace(), 150);
  };
  state.chart.on("dataZoom", onZoom);
  state.stateChart.on("dataZoom", onZoom);

  // 点击 x 轴 / 图区 / 曲线，选中该时刻 → 右侧显示该时刻所有信号值
  const bindTimeClick = (chart) => {
    chart.on("click", (params) => {
      let t = null;
      if (params.componentType === "xAxis") {
        t = params.value;
      } else if (params.componentType === "series") {
        t = Array.isArray(params.value) ? params.value[0] : params.value;
      } else if (params.componentType === "grid") {
        const xPx = params.event?.offsetX;
        if (xPx != null) t = chart.convertFromPixel({ xAxisIndex: 0 }, xPx);
      }
      if (t != null && !isNaN(t)) setCursor(t);
    });
  };
  bindTimeClick(state.chart);
  bindTimeClick(state.stateChart);
}

async function fetchSeries(names, t0, t1, maxPoints = 20000) {
  if (!names.length) return {};
  return api(`/api/series?signals=${encodeURIComponent(names.join(","))}&start=${t0}&end=${t1}&max_points=${maxPoints}`);
}

function signalMeta(name) { return state.signals.find(s => s.name === name); }

function stripTop(i) { return 8 + i * (STRIP_H + GAP); }

function layoutChartHeights() {
  const N = Math.max(1, state.selected.size);
  const F = (state.analysis?.functions || []).length;
  const h = 10 + N * (STRIP_H + GAP) + 44;
  $("chart").style.height = `${h}px`;
  $("stateChart").style.height = `${Math.max(70, F * 32 + 44)}px`;
  if (state.chart) { state.chart.resize(); state.stateChart.resize(); }
}

function yRange(meta, d, isEnum) {
  if (isEnum && meta.choices) {
    return { min: -0.4, max: Object.keys(meta.choices).length - 0.6, interval: 1 };
  }
  let lo = d.min, hi = d.max;
  if (lo == null || hi == null) {
    const vs = (d.v || []).filter(v => v != null);
    if (!vs.length) return { min: 0, max: 1 };
    lo = Math.min(...vs); hi = Math.max(...vs);
  }
  if (lo === hi) { lo -= 1; hi += 1; }
  else { const pad = (hi - lo) * 0.08; lo -= pad; hi += pad; }
  return { min: lo, max: hi };
}

function buildSignalChart(data, sel) {
  const N = sel.length;
  const grids = [], xAxes = [], yAxes = [], series = [];
  const xIdxList = [];

  sel.forEach((name, i) => {
    const meta = signalMeta(name);
    const isEnum = !!(meta && meta.choices);
    const d = data[name] || { t: [], v: [], min: null, max: null };
    const rng = yRange(meta, d, isEnum);
    const color = colorOf(i);
    const labelFmt = (v) => (isEnum && meta.choices ? (meta.choices[String(Math.round(v))] ?? v) : numFmt(v));

    grids.push({
      left: 120, right: 62, top: stripTop(i), height: STRIP_H, show: true,
      borderColor: "#232c39", borderWidth: 1, backgroundColor: "#141a23",
    });
    xIdxList.push(i);

    xAxes.push({
      gridIndex: i, type: "value", position: "bottom", scale: true,
      axisLabel: { show: i === N - 1, color: "#8a94a3", fontSize: 9, formatter: v => v.toFixed(1) },
      axisLine: { show: i === N - 1, lineStyle: { color: "#2a3340" } },
      axisTick: { show: false }, splitLine: { show: false },
    });

    // 左轴：信号名；右轴：数值刻度（CANoe 风格）
    yAxes.push({
      gridIndex: i, type: "value", position: "left",
      min: rng.min, max: rng.max, interval: rng.interval,
      name: shortName(name), nameTextStyle: { color, fontSize: 10, fontWeight: "normal" },
      nameLocation: "middle", nameGap: 6,
      axisLabel: { show: false }, axisTick: { show: false },
      axisLine: { show: true, lineStyle: { color } },
      splitLine: { show: true, lineStyle: { color: "#202836" } },
    });
    yAxes.push({
      gridIndex: i, type: "value", position: "right",
      min: rng.min, max: rng.max, interval: rng.interval,
      axisLabel: { show: true, color: "#6b7686", fontSize: 9, formatter: labelFmt },
      axisLine: { show: true, lineStyle: { color: "#2a3340" } },
      axisTick: { show: false }, splitLine: { show: false },
    });

    series.push({
      name, type: "line", xAxisIndex: i, yAxisIndex: 2 * i, showSymbol: false,
      step: isEnum ? "end" : false,
      data: d.t.map((x, k) => [x, d.v[k]]),
      lineStyle: { color, width: 1.4 }, itemStyle: { color },
    });

    // 该条带的垂直光标线
    series.push(cursorSeries(i, 2 * i));
  });

  return {
    grid: grids, xAxis: xAxes, yAxis: yAxes, series,
    tooltip: {
      trigger: "axis", axisPointer: { type: "cross" },
      formatter: (params) => tooltipFor(params),
    },
    dataZoom: [
      { type: "inside", xAxisIndex: xIdxList },
      { type: "slider", xAxisIndex: xIdxList, height: 16, bottom: 2, borderColor: "#2a3340" },
    ],
  };
}

function cursorSeries(xi, yi) {
  return {
    name: `__cursor_${xi}`, type: "line", xAxisIndex: xi, yAxisIndex: yi, z: 20,
    data: [[state.t0, 0], [state.t1, 0]],
    lineStyle: { opacity: 0 }, showSymbol: false, silent: true,
    markLine: {
      silent: true, symbol: "none", animation: false,
      lineStyle: { color: "#ffe066", width: 1, type: "dashed" },
      label: { show: false },
      data: [{ xAxis: state.t }],
    },
  };
}

function tooltipFor(params) {
  const x = params[0]?.value?.[0];
  let html = `<div style="font-family:monospace">${fmtTime(x)}</div>`;
  for (const p of params) {
    if (p.seriesName.startsWith("__cursor")) continue;
    const meta = signalMeta(p.seriesName);
    let val = p.value?.[1];
    if (meta && meta.choices && val != null) val = `${meta.choices[String(Math.round(val))] ?? val} (${Math.round(val)})`;
    else val = numFmt(val);
    html += `<div><span style="color:${p.color}">●</span> ${escapeHtml(p.seriesName)}: <b>${val}</b> ${escapeHtml(meta?.unit || "")}</div>`;
  }
  return html;
}

function buildStateChart() {
  const funcs = state.analysis?.functions || [];
  const series = [];
  const LANE = 0.8;

  funcs.forEach((f, i) => {
    const intervals = (state.analysis?.intervals || {})[f.id] || [];
    const data = [];
    for (const [a, b] of intervals) { data.push([a, i], [a, i + LANE], [b, i + LANE], [b, i]); }
    const color = colorOf(i);
    series.push({
      name: f.name || f.id, type: "line", data, step: "end", yAxisIndex: 0,
      lineStyle: { color, width: 1 }, areaStyle: { color, opacity: 0.30 }, showSymbol: false,
    });
  });

  const enters = [], exits = [];
  for (const e of state.analysis?.events || []) {
    const idx = funcs.findIndex(f => f.id === e.function);
    if (idx < 0) continue;
    (e.type === "enter" ? enters : exits).push([e.t, idx + LANE]);
  }
  series.push(
    { name: "进入", type: "scatter", data: enters, symbol: "triangle", symbolSize: 10, z: 5,
      itemStyle: { color: "#22c55e" }, tooltip: { formatter: (p) => `进入 @ ${fmtTime(p.value[0])}` } },
    { name: "退出", type: "scatter", data: exits, symbol: "triangle", symbolRotate: 180, symbolSize: 10, z: 5,
      itemStyle: { color: "#ef4444" }, tooltip: { formatter: (p) => `退出 @ ${fmtTime(p.value[0])}` } },
  );

  return {
    grid: { left: 180, right: 24, top: 8, bottom: 8 },
    xAxis: {
      type: "value", scale: true,
      axisLabel: { color: "#8a94a3", formatter: v => v.toFixed(1) }, splitLine: { show: false },
      axisPointer: { show: true, type: "line", label: { formatter: (p) => fmtTime(p.value) } },
    },
    yAxis: {
      type: "value", min: -0.25, max: Math.max(1, funcs.length) - 0.15, interval: 1,
      axisLabel: {
        color: "#d7dde6", fontSize: 10,
        formatter: (v) => {
          const i = Math.round(v);
          const n = funcs[i] ? (funcs[i].name || funcs[i].id) : "";
          return n.length > 16 ? n.slice(0, 15) + "…" : n;
        },
      },
      splitLine: { show: false },
    },
    tooltip: {
      trigger: "axis", axisPointer: { type: "line" },
      formatter: (params) => {
        const x = params[0]?.value?.[0];
        const active = params
          .filter(p => p.seriesName !== "进入" && p.seriesName !== "退出" && p.value
            && (p.value[1] - Math.floor(p.value[1])) >= 0.4)
          .map(p => p.seriesName);
        return `<div style="font-family:monospace">${fmtTime(x)}</div>` +
          (active.length ? active.map(a => `<div>🟢 ${escapeHtml(a)}</div>`).join("") : '<div style="color:#8a94a3">无激活功能</div>');
      },
    },
    dataZoom: [{ type: "inside", xAxisIndex: 0 }],
    series,
  };
}

async function renderCharts() {
  if (!state.chart) return;
  const sel = [...state.selected];
  if (!state.status || state.status.start == null || state.status.end == null) {
    state.chart.clear(); state.stateChart.clear();
    state.chart.setOption({ title: { text: "请加载报文日志（.asc/.csv/.json…）", left: "center", top: "middle", textStyle: { color: "#555" } } });
    return;
  }
  if (!sel.length) {
    state.chart.clear(); state.stateChart.clear();
    state.chart.setOption({ title: { text: "勾选左侧信号开始绘图", left: "center", top: "middle", textStyle: { color: "#555" } } });
    state.stateChart.setOption(buildStateChart(), true);
    layoutChartHeights();
    return;
  }
  const data = await fetchSeries(sel, state.status.start, state.status.end, 20000);
  layoutChartHeights();
  state.chart.setOption(buildSignalChart(data, sel), true);
  state.stateChart.setOption(buildStateChart(), true);
}

// ---------------------------------------------------------------------------
// 功能列表（侧栏）
// ---------------------------------------------------------------------------
function renderFunctionList() {
  const box = $("functionList");
  const funcs = state.analysis?.functions || [];
  if (!funcs.length) { box.innerHTML = '<div class="muted" style="padding:4px">加载功能规格后显示</div>'; return; }
  box.innerHTML = "";
  for (const f of funcs) {
    const ivs = (state.analysis.intervals || {})[f.id] || [];
    const div = document.createElement("div");
    div.className = "func";
    const actStr = ivs.length ? ivs.map(([a, b]) => `${a.toFixed(1)}~${b.toFixed(1)}s`).join("、") : "全程未激活";
    div.innerHTML = `<div class="fname">${escapeHtml(f.name || f.id)}</div>
      ${f.description ? `<div class="fdesc">${escapeHtml(f.description)}</div>` : ""}
      <div class="fstat">激活区间: ${escapeHtml(actStr)}</div>`;
    div.onclick = () => { if (ivs.length) setCursor(ivs[0][0]); };
    box.appendChild(div);
  }
}

// ---------------------------------------------------------------------------
// 报文跟踪表
// ---------------------------------------------------------------------------
async function renderTrace() {
  const t = state.t;
  const half = 0.75;
  const needFetch = !state.traceRange || t < state.traceRange[0] || t > state.traceRange[1];
  if (needFetch) {
    state.traceRows = await api(`/api/trace?start=${t - half}&end=${t + half}&limit=2000`);
    state.traceRange = [t - half, t + half];
  }
  const rows = state.traceRows;
  const tbody = $("traceTable").querySelector("tbody");
  tbody.innerHTML = "";
  $("traceInfo").textContent = `${rows.length} 帧 @ ${fmtTime(t)}`;

  let nearest = 0, best = Infinity;
  rows.forEach((r, i) => { const d = Math.abs(r.t - t); if (d < best) { best = d; nearest = i; } });

  const WINDOW = 60;
  const start = Math.max(0, nearest - Math.floor(WINDOW / 2));
  const slice = rows.slice(start, start + WINDOW);
  const frag = document.createDocumentFragment();
  slice.forEach((r, i) => {
    const tr = document.createElement("tr");
    if (start + i === nearest) tr.className = "active";
    const sigStr = (r.signals || []).map(s => `<b>${escapeHtml(s.name)}</b>=${escapeHtml(String(s.value))}`).join(" ");
    tr.innerHTML = `<td>${fmtTime(r.t)}</td>
      <td class="td-id">0x${r.id.toString(16).toUpperCase()}</td>
      <td>${escapeHtml(r.name || "")}</td>
      <td class="td-sig">${sigStr}</td>`;
    tr.addEventListener("click", () => setCursor(r.t));
    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
}

// ---------------------------------------------------------------------------
// 光标 / 播放
// ---------------------------------------------------------------------------
function setCursor(t, updateSeek = true) {
  const s = state.status;
  if (s?.start != null && s?.end != null) t = Math.min(Math.max(t, s.start), s.end);
  state.t = t;
  $("timeLabel").textContent = fmtTime(t);
  if (updateSeek && s?.start != null && s?.end != null) {
    $("seek").value = Math.round((t - s.start) / (s.end - s.start) * 1000);
  }
  if (state.chart && state.selected.size > 0) {
    const series = [...state.selected].map((_, i) => ({ name: `__cursor_${i}`, markLine: { data: [{ xAxis: t }] } }));
    state.chart.setOption({ series });
  }
  renderTrace();
  scheduleValues();
}

function setRange(ratio) {
  const s = state.status;
  if (!s || s.start == null) return;
  setCursor(s.start + (s.end - s.start) * ratio, false);
}

let raf = null, lastTs = 0;
function play() {
  if (state.playing) { state.playing = false; $("btnPlay").textContent = "▶"; cancelAnimationFrame(raf); return; }
  if (state.status?.start == null) return;
  if (state.t >= state.status.end) state.t = state.status.start;
  state.playing = true;
  $("btnPlay").textContent = "⏸";
  lastTs = performance.now();
  const step = () => {
    if (!state.playing) return;
    const now = performance.now();
    const dt = (now - lastTs) / 1000;
    lastTs = now;
    state.t += dt * state.speed;
    if (state.t >= state.status.end) { state.t = state.status.end; state.playing = false; $("btnPlay").textContent = "▶"; }
    setCursor(state.t);
    raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
}

// ---------------------------------------------------------------------------
// 规格编辑
// ---------------------------------------------------------------------------
async function openSpec() {
  $("specModal").classList.remove("hidden");
  try { const r = await api("/api/spec"); if (r.text) $("specText").value = r.text; } catch (_) {}
}
$("btnSpec").addEventListener("click", openSpec);
$("specClose").addEventListener("click", () => $("specModal").classList.add("hidden"));
$("specRun").addEventListener("click", async () => {
  try {
    await api("/api/spec", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: $("specText").value }) });
    $("specMsg").textContent = "已保存";
    await refreshAnalysis();
    await refreshStatus();
    renderCharts();
    $("specModal").classList.add("hidden");
  } catch (e) {
    $("specMsg").textContent = `错误: ${e.message}`;
  }
});

// ---------------------------------------------------------------------------
// 事件绑定 & 拖拽
// ---------------------------------------------------------------------------
$("btnSample").addEventListener("click", loadSample);
$("fileDbc").addEventListener("change", e => { if (e.target.files[0]) uploadFile("/api/upload/dbc", e.target.files[0]); });
$("fileLog").addEventListener("change", e => { if (e.target.files[0]) uploadFile("/api/upload/log", e.target.files[0]); });
$("fileSpec").addEventListener("change", e => { if (e.target.files[0]) uploadFile("/api/upload/spec", e.target.files[0]); });

$("btnPlay").addEventListener("click", play);
$("btnPrev").addEventListener("click", () => setCursor(state.t - 0.1));
$("btnNext").addEventListener("click", () => setCursor(state.t + 0.1));
$("seek").addEventListener("input", e => setRange(parseInt(e.target.value) / 1000));
$("speed").addEventListener("change", e => { state.speed = parseFloat(e.target.value); });

window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.code === "Space") { e.preventDefault(); play(); }
  else if (e.key === "ArrowLeft") setCursor(state.t - 0.1);
  else if (e.key === "ArrowRight") setCursor(state.t + 0.1);
});

let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; $("dropzone").classList.remove("hidden"); });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("dropzone").classList.add("hidden"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", async (e) => {
  e.preventDefault(); dragDepth = 0; $("dropzone").classList.add("hidden");
  const files = [...e.dataTransfer.files];
  for (const f of files) {
    const ext = f.name.split(".").pop().toLowerCase();
    try {
      if (ext === "dbc") await uploadFile("/api/upload/dbc", f);
      else if (["asc", "csv", "json", "trc", "blf", "mf4", "log"].includes(ext)) await uploadFile("/api/upload/log", f);
      else if (["yaml", "yml"].includes(ext)) await uploadFile("/api/upload/spec", f);
    } catch (err) { alert(`${f.name}: ${err.message}`); }
  }
});

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------
(async function boot() {
  initCharts();
  try {
    await refreshStatus();
    if (state.status && state.status.dbc == null && state.status.log == null) {
      await loadSample();   // 首次启动自动加载示例，开箱即用
    } else {
      await refreshAll();
    }
  } catch (e) {
    $("status").textContent = `加载失败: ${e.message}`;
  }
})();
