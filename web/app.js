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
  t: 0,                  // 当前光标时间
  playing: false,
  speed: 1,
  t0: 0, t1: 1,          // 当前可视时间范围
  chart: null,
  valueMode: "selected",  // 右侧信号值面板：默认显示图形中已勾选信号（从上到下）
  functions: [],         // 功能清单（含每个功能引用的信号）
  activeFunction: null,  // 图中当前正在观察的功能 id（手动增删信号后置空）
  yZoom: 1,              // Y 轴整体缩放：所有条带一起变高/变矮
  lastSeries: null,      // {data, sel} — Y 缩放重排时复用，避免重新拉数据

  // 下边框「关键事件」面板
  events: [],            // 当前筛选后的事件（按时间升序）
  evFuncs: new Set(),    // 功能筛选，空 = 全部
  evTypes: new Set(["enter", "exit", "change"]),
  evQuery: "",
  evAllFuncs: [],        // [{id,name,count}]，计数按未筛选全集
  evTotal: 0,
  evTruncated: false,
  evActive: -1,          // 当前高亮事件在 state.events 中的下标
  evFollow: true,        // 光标移动时自动高亮/滚动到当前事件
  evFast: false,         // 快速求值（只在信号变化时求值），默认关

  sigMeta: new Map(),    // 信号名 → 元数据。1861 条信号线性 find 太贵，见 signalMeta()
};

const COLORS = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#06b6d4",
  "#ec4899", "#84cc16", "#f97316", "#14b8a6", "#8b5cf6", "#eab308"];

const STRIP_H = 76;      // 每个信号条带基准高度（yZoom=1 时）
const GAP = 12;          // 条带间距
const Y_MIN = 0.5, Y_MAX = 4, Y_STEP = 1.25;   // Y 整体缩放范围与步进
const X_STEP = 1.6;                            // X 每次缩放倍率

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

// 时间一律按「相对日志起点」显示：BLF/MF4 的时间戳是绝对纪元秒，
// 直接摊在界面上是 1755500000.397 这种没法读的数字。CSV 示例 start=0，显示不变。
function relT(t) { return (t ?? 0) - (state.status?.start ?? 0); }
function fmtTime(t) { return `${relT(t).toFixed(3)} s`; }
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
  if (s.log) parts.push(`日志: ${s.log} (${s.frame_count} 帧 / ${s.log_message_count ?? 0} 个 ID)`);
  if (s.spec) parts.push(`规格: ${s.spec}`);
  $("status").textContent = parts.length ? parts.join("  ·  ") : "未加载数据";
  renderMismatch();
  if (s.start != null && s.end != null) {
    state.t0 = s.start; state.t1 = s.end;
    if (state.t < s.start || state.t > s.end) state.t = s.start;
    $("rangeLabel").textContent = `范围 ${fmtTime(s.start)} ~ ${fmtTime(s.end)}`;
  }
}

// /api/messages 里已经嵌了每条报文的完整信号元数据，/api/signals 只是把同一批
// 数据换个平铺形状再传一遍 —— 1861 条信号就是多下载、多解析一份 1 MB JSON。
// 这里直接从 messages 派生（端点保留不动，外部调用方不受影响）。
async function refreshMessages() {
  state.messages = await api("/api/messages");
  const flat = [];
  const map = new Map();
  for (const msg of state.messages) {
    for (const s of msg.signals) {
      const meta = { ...s, message_id: msg.frame_id };
      flat.push(meta);
      map.set(meta.name, meta);
    }
  }
  flat.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  state.signals = flat;
  state.sigMeta = map;
  $("signalCount").textContent = `(${state.signals.length})`;
  populateDatalist();
  renderSignalTree();
}

async function refreshFunctions() {
  try { state.functions = await api("/api/functions"); }
  catch (_) { state.functions = []; }
  renderFunctionButtons();
  renderMismatch();
}

// ---------------------------------------------------------------------------
// DBC / 日志 / 规格 三者不匹配的告警
//
// 三个文件是各自独立替换的：换了 DBC 和规格但忘了换日志，界面照样能画出坐标轴
// （轴信息来自 DBC），只是每条曲线都是空的、关键事件是 0 条。以前这种情况完全
// 没有提示，只能靠自己盯着顶栏的文件名发现。这里把它直接摆到台面上。
// ---------------------------------------------------------------------------
function renderMismatch() {
  const box = $("mismatch");
  if (!box) return;
  const s = state.status;
  if (!s || !s.dbc || !s.log) { box.classList.add("hidden"); return; }

  const notes = [];
  const unknown = s.log_unknown_ids || [];
  const total = s.log_message_count || 0;
  // 少量 ID 不在 DBC 里很正常（诊断 ID、网管帧……），只有占比高才说明装错了。
  // 不设阈值的话这条告警会常驻，很快就被当成壁纸忽略掉。
  if (unknown.length && total && unknown.length / total > 0.3) {
    const shown = unknown.slice(0, 6).map(i => `0x${i.toString(16).toUpperCase()}`).join(" ");
    notes.push(`日志里有 ${unknown.length}/${total} 个报文 ID 不在当前 DBC 中（${shown}${unknown.length > 6 ? " …" : ""}）—— DBC 与日志可能不配套`);
  }
  const funcs = state.functions || [];
  if (funcs.length) {
    const dead = funcs.filter(f => !(f.available || []).length);
    const nodata = new Set();
    for (const f of funcs) for (const n of (f.nodata || [])) nodata.add(n);
    if (dead.length === funcs.length) {
      notes.push(`规格里 ${funcs.length} 个功能引用的信号在当前日志中全部没有数据 —— 多半是日志装错了（规格是给另一份日志写的）`);
    } else if (dead.length) {
      notes.push(`规格里有 ${dead.length}/${funcs.length} 个功能，其引用的信号在当前日志中没有任何报文`);
    } else if (nodata.size) {
      notes.push(`规格引用的信号中有 ${nodata.size} 个在当前日志中没有数据`);
    }
  }

  if (!notes.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.innerHTML = `<span class="mm-icon">⚠</span><span>${notes.map(escapeHtml).join("；")}</span>`;
}

async function refreshAll() {
  // 三个请求互不依赖，并发发出去（大 DBC 下 /api/messages 本身就有 1 MB）
  const [, ,] = await Promise.all([refreshStatus(), refreshMessages(), refreshFunctions()]);
  renderMismatch();          // 需要 status + functions 都到位后再判一次
  renderCharts();
  refreshEvents();
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
// 1861 条信号逐行 createElement + addEventListener 要上百毫秒，而搜索框每敲一个
// 键都会重建整棵树。改成拼一次 HTML 字符串 + 事件委托，重建成本降一个数量级。
function renderSignalTree() {
  const tree = $("signalTree");
  const q = $("signalSearch").value.trim().toLowerCase();
  const selIndex = new Map([...state.selected].map((n, i) => [n, i]));
  const fallbackColor = colorOf(state.selected.size);
  const parts = [];

  for (const msg of state.messages) {
    const msgHit = msg.name.toLowerCase().includes(q);
    const sigs = q ? msg.signals.filter(s => msgHit || s.name.toLowerCase().includes(q)) : msg.signals;
    if (!sigs.length) continue;
    // 报文没在日志里出现过 → 整组置灰，省得用户勾了半天全是空曲线
    const noData = msg.has_data === false;
    parts.push(`<div class="tree-msg${noData ? " nodata" : ""}"${noData ? ' title="当前日志中没有这个报文"' : ""}>`
      + `${escapeHtml(msg.name)} (0x${msg.frame_id.toString(16).toUpperCase()})</div>`);
    for (const s of sigs) {
      const idx = selIndex.get(s.name);
      const color = idx != null ? colorOf(idx) : fallbackColor;
      // has_data=false：DBC 里有这个信号，但当前日志里它所属的报文一帧都没有
      const dead = s.has_data === false;
      parts.push(`<label class="tree-sig${dead ? " nodata" : ""}" data-sig="${escapeHtml(s.name)}"`
        + (dead ? ` title="${escapeHtml(s.name)}：当前日志中没有该报文，勾上是空曲线"` : "")
        + `><input type="checkbox"${idx != null ? " checked" : ""} />`
        + `<span class="swatch" style="background:${color}"></span>`
        + `<span>${escapeHtml(s.name)}</span><span class="unit">${escapeHtml(s.unit || "")}</span></label>`);
    }
  }
  tree.innerHTML = parts.length ? parts.join("") : '<div class="muted" style="padding:6px">无匹配信号</div>';
}

$("signalTree").addEventListener("change", (e) => {
  const box = e.target;
  if (box.tagName !== "INPUT") return;
  const name = box.closest(".tree-sig")?.dataset.sig;
  if (!name) return;
  if (box.checked) state.selected.add(name); else state.selected.delete(name);
  // 手动改过勾选，图里就不再是某个功能的原样链路了
  state.activeFunction = null;
  renderFunctionButtons();
  renderCharts();
});

let sigSearchTimer = null;
$("signalSearch").addEventListener("input", () => {
  clearTimeout(sigSearchTimer);
  sigSearchTimer = setTimeout(renderSignalTree, 120);
});

function populateDatalist() {
  $("signalList").innerHTML =
    state.signals.map(s => `<option value="${escapeHtml(s.name)}"></option>`).join("");
}

function addSignalByName() {
  const input = $("signalAdd");
  const name = input.value.trim();
  if (!name) return;
  if (!signalMeta(name)) { input.style.outline = "1px solid var(--red)"; setTimeout(() => input.style.outline = "", 800); return; }
  state.selected.add(name);
  state.activeFunction = null;
  input.value = "";
  renderFunctionButtons();
  renderSignalTree();
  renderCharts();
}
$("btnAddSignal").addEventListener("click", addSignalByName);
$("signalAdd").addEventListener("keydown", e => { if (e.key === "Enter") addSignalByName(); });

// ---------------------------------------------------------------------------
// 功能触发按钮：点一下把该功能引用的信号追加勾选到 Graphics
// ---------------------------------------------------------------------------
function renderFunctionButtons() {
  const box = $("functionButtons");
  const funcs = state.functions || [];
  $("funcCount").textContent = funcs.length ? `(${funcs.length})` : "";
  if (!funcs.length) {
    box.innerHTML = '<div class="muted" style="padding:4px;font-size:12px">加载功能规格（YAML）后显示</div>';
    return;
  }
  box.innerHTML = "";
  for (const f of funcs) {
    const avail = f.available || [];    // DBC 里有 且 当前日志里有数据
    const nodata = f.nodata || [];      // DBC 里有 但 当前日志里一帧都没有
    const missing = f.missing || [];    // DBC 里就没有
    const total = avail.length + nodata.length + missing.length;

    const shown = [...avail, ...nodata];
    const tips = [];
    if (shown.length) {
      tips.push(`点击：只显示该功能的 ${shown.length} 个信号（清空图中其他信号），` +
                `事件栏同步筛到该功能\n  ${shown.join("\n  ")}`);
      tips.push("Ctrl / Shift + 点击：追加到当前图中（跨功能对照用）");
    }
    if (nodata.length) tips.push(`其中 ${nodata.length} 个在当前日志中没有任何报文（DBC 里有定义，画出来是空曲线）：\n  ${nodata.join("\n  ")}\n→ 多半是日志装错了，换成录有这些报文的日志`);
    if (missing.length) tips.push(`当前 DBC 中不存在：\n  ${missing.join("\n  ")}\n→ 换成配套的 DBC`);

    const btn = document.createElement("button");
    // 只有"全都不在 DBC 里"才真的没什么可点；只是没数据仍允许勾上，
    // 让用户亲眼看到那条曲线是空的，比按钮变灰不给理由更好排查
    btn.className = "funcbtn"
      + (avail.length ? "" : " fb-dead")
      + (state.activeFunction === f.id ? " on" : "");
    btn.disabled = !shown.length;
    btn.title = tips.join("\n\n");
    btn.innerHTML =
      `<span class="fb-name">${escapeHtml(f.name || f.id)}</span>` +
      (nodata.length || missing.length ? `<span class="fb-warn">⚠</span>` : "") +
      `<span class="fb-n">${avail.length < total ? `${avail.length}/${total}` : total}</span>`;
    btn.addEventListener("click", (e) => pickFunctionSignals(f, e.ctrlKey || e.shiftKey || e.metaKey));
    box.appendChild(btn);
  }
}

// 功能面板折叠：状态记在 localStorage，刷新后保持
const FUNC_COLLAPSE_KEY = "canprobe.funcCollapsed";
function setFuncCollapsed(collapsed) {
  $("funcPanel").classList.toggle("collapsed", collapsed);
  $("funcToggle").setAttribute("aria-expanded", String(!collapsed));
  localStorage.setItem(FUNC_COLLAPSE_KEY, collapsed ? "1" : "0");
}
$("funcToggle").addEventListener("click", () => {
  setFuncCollapsed(!$("funcPanel").classList.contains("collapsed"));
});
setFuncCollapsed(localStorage.getItem(FUNC_COLLAPSE_KEY) === "1");

let funcMsgTimer = null;
function funcMsg(text) {
  $("funcMsg").textContent = text;
  clearTimeout(funcMsgTimer);
  funcMsgTimer = setTimeout(() => { $("funcMsg").textContent = ""; }, 2500);
}

// 切功能 = 换一个观察视角：清空其他信号，只留这个功能链路上的那几条。
// 追加式的话点两下就是十几条无关曲线叠在一起，反而看不出这个功能的问题。
// 需要跨功能对照时（比如 HDC 与 ESC/TCS 的互锁）按住 Ctrl / Shift 点。
function pickFunctionSignals(f, append = false) {
  const avail = f.available || [];
  const nodata = f.nodata || [];
  const pick = [...avail, ...nodata];
  if (!pick.length) { funcMsg("信号都不在当前 DBC 中"); return; }

  if (append) {
    let added = 0;
    for (const n of pick) {
      if (!state.selected.has(n)) { state.selected.add(n); added++; }
    }
    state.activeFunction = null;     // 混合视图，不再对应某一个功能
    funcMsg(added ? `+${added} 个信号` : "已在图中");
  } else {
    state.selected = new Set(pick);
    state.activeFunction = f.id;
    // 把"加进去了但没数据"当场说清楚，而不是让用户对着空曲线猜
    funcMsg(nodata.length ? `${pick.length} 个信号（${nodata.length} 个本日志无数据）`
                          : `${pick.length} 个信号`);
  }
  renderFunctionButtons();
  renderSignalTree();
  renderCharts();
  if (!append) syncEventsToFunction(f.id);
}

// 图切到哪个功能，下方事件栏就跟着筛到哪个功能 —— 曲线和事件历程讲同一件事。
// 事件栏的功能 chip 会同步高亮，点「全部」即可放开。
function syncEventsToFunction(id) {
  if (state.evFuncs.size === 1 && state.evFuncs.has(id)) return;
  state.evFuncs = new Set([id]);
  refreshEvents();
}

// 右侧信号值面板。回放时光标每帧都在动，这里必须做两件事：节流，以及把还在飞的
// 旧请求掐掉 —— 否则慢一点的响应会排成队，面板上的值反而落在光标后面。
let valueTimer = null;
let valueAbort = null;
function scheduleValues() {
  clearTimeout(valueTimer);
  valueTimer = setTimeout(renderValues, state.playing ? 120 : 80);
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
  if (valueAbort) valueAbort.abort();
  valueAbort = new AbortController();
  const signal = valueAbort.signal;
  try {
    const vals = await api(`/api/values?signals=${encodeURIComponent(sel.join(","))}&t=${state.t}`, { signal });
    if (signal.aborted) return;
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
  } catch (e) {
    if (e.name === "AbortError") return;   // 被后一次光标移动取代，不是错误
    box.innerHTML = '<div class="muted" style="padding:6px">—</div>';
  }
}
$("vmAll").addEventListener("click", () => { state.valueMode = "all"; $("vmAll").classList.add("on"); $("vmSel").classList.remove("on"); renderValues(); });
$("vmSel").addEventListener("click", () => { state.valueMode = "selected"; $("vmSel").classList.add("on"); $("vmAll").classList.remove("on"); renderValues(); });

// 右侧信号值面板：拖拽把手调整宽度，宽度记住在 localStorage
(function initValuePanelResize() {
  const bar = $("vresizer");
  const panel = $("valuePanel");
  if (!bar || !panel) return;

  const MIN = 220, DEFAULT = 330, MAX_FRAC = 0.6, KEY = "canprobe.valuePanelWidth";
  const clampW = (w) => Math.max(MIN, Math.min(w, Math.round(window.innerWidth * MAX_FRAC)));
  // 宽度立即生效（纯 CSS，便宜）；ECharts resize 走 rAF 节流，避免大图拖拽掉帧
  let resizeRaf = null;
  const applyW = (w) => {
    panel.style.width = `${clampW(w)}px`;
    if (!state.chart || resizeRaf) return;
    resizeRaf = requestAnimationFrame(() => { resizeRaf = null; state.chart.resize(); renderRibbon(); });
  };

  const saved = parseInt(localStorage.getItem(KEY) || "", 10);
  if (!isNaN(saved)) applyW(saved);

  let dragging = false;
  bar.addEventListener("pointerdown", (e) => {
    dragging = true;
    bar.setPointerCapture(e.pointerId);
    bar.classList.add("dragging");
    document.body.classList.add("vresizing");
    e.preventDefault();
  });
  bar.addEventListener("pointermove", (e) => {
    if (dragging) applyW(window.innerWidth - e.clientX);   // 面板右缘贴窗口右侧
  });
  const endDrag = (e) => {
    if (!dragging) return;
    dragging = false;
    try { bar.releasePointerCapture(e.pointerId); } catch (_) {}
    bar.classList.remove("dragging");
    document.body.classList.remove("vresizing");
    localStorage.setItem(KEY, String(parseInt(panel.style.width, 10) || DEFAULT));
  };
  bar.addEventListener("pointerup", endDrag);
  bar.addEventListener("pointercancel", endDrag);

  bar.addEventListener("dblclick", () => { applyW(DEFAULT); localStorage.setItem(KEY, String(DEFAULT)); });

  // 窗口变窄时重新夹紧，避免面板挤掉主区域
  window.addEventListener("resize", () => applyW(parseInt(panel.style.width, 10) || panel.offsetWidth));
})();

// ---------------------------------------------------------------------------
// 图表（每信号一行、独立坐标轴的条带式布局，对标 CANoe Graphics）
// ---------------------------------------------------------------------------
function initCharts() {
  if (!window.echarts) { alert("ECharts 未加载"); return; }
  state.chart = echarts.init($("chart"));
  window.addEventListener("resize", () => { state.chart.resize(); renderRibbon(); });

  let zoomTimer = null;
  const onZoom = (params) => {
    const b = (params.batch || [params])[0] || {};
    if (b.startValue != null && b.endValue != null) { state.t0 = b.startValue; state.t1 = b.endValue; }
    updateZoomButtons();
    // ribbon / 光标跟着 X 窗口重画；连续滚轮缩放时节流，避免每帧重建几百个刻度
    clearTimeout(zoomTimer);
    zoomTimer = setTimeout(renderRibbon, 80);
  };
  state.chart.on("dataZoom", onZoom);

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
}

async function fetchSeries(names, t0, t1, maxPoints = 20000) {
  if (!names.length) return {};
  return api(`/api/series?signals=${encodeURIComponent(names.join(","))}&start=${t0}&end=${t1}&max_points=${maxPoints}`);
}

// tooltip formatter 与信号值面板都是逐行调用，1861 条信号线性 find 会成为热点
function signalMeta(name) { return state.sigMeta.get(name); }

// 条带几何随 Y 整体缩放变化
function stripH() { return Math.round(STRIP_H * state.yZoom); }
function stripGap() { return Math.round(GAP * state.yZoom); }
function stripTop(i) { return 8 + i * (stripH() + stripGap()); }

function layoutChartHeights() {
  const N = Math.max(1, state.selected.size);
  const h = 10 + N * (stripH() + stripGap()) + 44;
  $("chart").style.height = `${h}px`;
  if (state.chart) state.chart.resize();
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
      left: 120, right: 62, top: stripTop(i), height: stripH(), show: true,
      borderColor: "#232c39", borderWidth: 1, backgroundColor: "#141a23",
    });
    xIdxList.push(i);

    xAxes.push({
      gridIndex: i, type: "value", position: "bottom", scale: true,
      axisLabel: { show: i === N - 1, color: "#8a94a3", fontSize: 9, formatter: v => relT(v).toFixed(1) },
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

    // 预分配再填，比 d.t.map(...) 少一次数组增长；每条曲线上万个点时这一步
    // 本身就是几毫秒量级
    const n = d.t.length;
    const pts = new Array(n);
    for (let k = 0; k < n; k++) pts[k] = [d.t[k], d.v[k]];

    series.push({
      name, type: "line", xAxisIndex: i, yAxisIndex: 2 * i, showSymbol: false,
      step: isEnum ? "end" : false,
      data: pts,
      // minmax 抽稀按像素收点，但每个像素列的极值一定保留 —— 排故看的就是尖峰，
      // lttb 那种保形抽稀有可能把单点毛刺抹掉，这里不能用；枚举是阶梯线，
      // 抽稀会把窄脉冲的台阶抹平，索性不抽。
      // 抽稀之后每条曲线实际描的点已经只有几千个，再叠 large 批渲染收益有限，
      // 而它会改变单点交互的处理路径，不值得为这点收益冒险。
      sampling: isEnum ? undefined : "minmax",
      animation: false,
      lineStyle: { color, width: 1.4 }, itemStyle: { color },
    });
  });

  return {
    animation: false,
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

// ---- 时间光标：DOM 叠加层 --------------------------------------------------
// 以前每个条带挂一条 markLine 系列，光标一动就要 setOption 整张图。回放时那是
// 每帧一次全量 option 合并，几万个点的图直接掉帧。现在只改一个 div 的 transform。
function renderCursor() {
  const el = $("cursorLine");
  if (!el) return;
  if (!state.chart || !state.selected.size) { el.classList.add("hidden"); return; }
  const x = state.chart.convertToPixel({ xAxisIndex: 0 }, state.t);
  if (x == null || isNaN(x) || x < 0 || x > $("chart").clientWidth) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  el.style.transform = `translateX(${x}px)`;
}

function tooltipFor(params) {
  const x = params[0]?.value?.[0];
  let html = `<div style="font-family:monospace">${fmtTime(x)}</div>`;
  for (const p of params) {
    const meta = signalMeta(p.seriesName);
    let val = p.value?.[1];
    if (meta && meta.choices && val != null) val = `${meta.choices[String(Math.round(val))] ?? val} (${Math.round(val)})`;
    else val = numFmt(val);
    html += `<div><span style="color:${p.color}">●</span> ${escapeHtml(p.seriesName)}: <b>${val}</b> ${escapeHtml(meta?.unit || "")}</div>`;
  }
  return html;
}

// 取点数保持 20000/信号不变：放大后要看细节，靠的就是这批多余的点。
// 画的时候由 ECharts 的 minmax 抽稀按像素收，既快又不会把尖峰抹掉。
const MAX_POINTS = 20000;

async function renderCharts() {
  if (!state.chart) return;
  const sel = [...state.selected];
  if (!state.status || state.status.start == null || state.status.end == null) {
    state.chart.clear();
    state.chart.setOption({ title: { text: "请加载报文日志（.asc/.csv/.json…）", left: "center", top: "middle", textStyle: { color: "#555" } } });
    renderCursor();
    return;
  }
  if (!sel.length) {
    state.chart.clear();
    state.lastSeries = null;
    state.chart.setOption({ title: { text: "勾选左侧信号开始绘图", left: "center", top: "middle", textStyle: { color: "#555" } } });
    layoutChartHeights();
    updateZoomButtons();
    renderRibbon();
    return;
  }
  const data = await fetchSeries(sel, state.status.start, state.status.end, MAX_POINTS);
  state.lastSeries = { data, sel };
  layoutChartHeights();
  state.chart.setOption(buildSignalChart(data, sel), true);
  applyXWindow();
  updateZoomButtons();
  renderRibbon();
  // 勾选变化后右侧值面板要跟着换一批信号；以前只有移动光标才会刷新，
  // 用功能按钮一次加进来 8 条曲线时，右边还停在上一组，看着像没生效
  scheduleValues();
}

// Y 缩放只改几何，数据不变 —— 复用缓存重排，不再打后端
function relayoutCharts() {
  if (!state.chart || !state.lastSeries) { layoutChartHeights(); return; }
  const { data, sel } = state.lastSeries;
  layoutChartHeights();
  state.chart.setOption(buildSignalChart(data, sel), true);
  applyXWindow();
}

// ---------------------------------------------------------------------------
// X（时间轴）/ Y（纵向整体）缩放
// ---------------------------------------------------------------------------
function xAxisIdx() { return [...state.selected].map((_, i) => i); }

function fullRange() {
  const s = state.status;
  return (s && s.start != null && s.end != null) ? [s.start, s.end] : null;
}

// setOption(…, true) 会把 dataZoom 打回全量，重排后需要把窗口贴回去
function applyXWindow() {
  const full = fullRange();
  if (!state.chart || !full || !state.selected.size) return;
  const [lo, hi] = full;
  const a = state.t0, b = state.t1;
  if (!(b > a) || (a <= lo && b >= hi)) return;   // 全量则无需 dispatch
  state.chart.dispatchAction({ type: "dataZoom", startValue: a, endValue: b, xAxisIndex: xAxisIdx() });
}

// factor < 1 放大（窗口变窄），> 1 缩小。以光标为锚点，保持其在窗口中的相对位置
function zoomX(factor) {
  const full = fullRange();
  if (!state.chart || !full || !state.selected.size) return;
  const [lo, hi] = full;
  const total = hi - lo;
  let a = state.t0, b = state.t1;
  if (!(b > a)) { a = lo; b = hi; }
  const span = b - a;
  const newSpan = Math.min(total, Math.max(span * factor, total / 5000));

  const inWin = state.t >= a && state.t <= b;
  const anchor = inWin ? state.t : (a + b) / 2;
  const frac = inWin ? (anchor - a) / span : 0.5;
  let na = anchor - newSpan * frac;
  let nb = na + newSpan;
  if (na < lo) { na = lo; nb = lo + newSpan; }
  if (nb > hi) { nb = hi; na = Math.max(lo, hi - newSpan); }

  state.t0 = na; state.t1 = nb;
  state.chart.dispatchAction({ type: "dataZoom", startValue: na, endValue: nb, xAxisIndex: xAxisIdx() });
  updateZoomButtons();
  renderRibbon();
}

function zoomY(mult) {
  const next = Math.min(Y_MAX, Math.max(Y_MIN, state.yZoom * mult));
  if (Math.abs(next - state.yZoom) < 1e-6) return;
  state.yZoom = next;
  relayoutCharts();
  updateZoomButtons();
}

function resetZoom() {
  const full = fullRange();
  state.yZoom = 1;
  if (full) { state.t0 = full[0]; state.t1 = full[1]; }
  relayoutCharts();          // notMerge 重建，dataZoom 自然回到全量
  updateZoomButtons();
  renderRibbon();
}

function updateZoomButtons() {
  const full = fullRange();
  const has = !!(full && state.selected.size);
  const total = full ? full[1] - full[0] : 0;
  const span = (state.t1 > state.t0) ? state.t1 - state.t0 : total;
  $("btnXIn").disabled = !has || span <= total / 4999;
  $("btnXOut").disabled = !has || span >= total - 1e-9;
  $("btnYIn").disabled = !has || state.yZoom >= Y_MAX - 1e-6;
  $("btnYOut").disabled = !has || state.yZoom <= Y_MIN + 1e-6;
  $("btnZoomReset").disabled = !has || (state.yZoom === 1 && span >= total - 1e-9);
}

// ---------------------------------------------------------------------------
// 下边框「关键事件」面板
//
// 定位问题的关键时间点集中管理在这里：功能规格算出的每一次进入 / 退出 / 状态
// 迁移都是一行，点一下把 Graphics 光标打到那个时刻（必要时平移 X 窗口），
// 反过来回放时也会自动高亮当前所处的事件。上方 ribbon 是同一批事件在时间轴上
// 的投影，与图表 X 轴共用同一套左右留白，所以刻度和曲线是对齐的。
// ---------------------------------------------------------------------------
const EV_TAGS = {
  enter:  { mark: "▲", label: "进入", cls: "enter" },
  exit:   { mark: "▼", label: "退出", cls: "exit" },
  change: { mark: "◆", label: "变化", cls: "change" },
};
const EV_RENDER_CAP = 600;    // DOM 行数上限：几千行硬渲染会把滚动拖垮
const EV_TICK_CAP = 400;      // ribbon 刻度上限（只画落在当前 X 窗口内的）
const RIBBON_PAD_L = 120;     // 与 buildSignalChart() 里 grid.left 对齐
const RIBBON_PAD_R = 62;      // 与 grid.right 对齐

function evTag(type) { return EV_TAGS[type] || { mark: "•", label: type, cls: "change" }; }

async function refreshEvents() {
  const list = $("evList");
  if (!state.status || !state.status.spec) {
    state.events = []; state.evAllFuncs = []; state.evActive = -1; state.evTotal = 0;
    $("evCount").textContent = "";
    $("evFuncs").innerHTML = "";
    list.innerHTML = '<div class="ev-empty">加载功能规格（YAML）后，这里按时间列出每个功能的进入 / 退出时刻</div>';
    renderRibbon();
    return;
  }
  if (!state.evTypes.size) {
    state.events = []; state.evActive = -1;
    renderEventList();
    renderRibbon();
    return;
  }

  // 日志装好后分析是惰性跑的（几百万帧跑一遍功能规格是分钟级的，挂在上传请求上
  // 会让加载看着像卡死），所以这里的"正在分析…"是真的在算。大日志下顺手把
  // 「快速求值」指出来，否则用户只会觉得卡住了、不知道有这条快路。
  const heavy = !state.evFast && (state.status?.frame_count || 0) > 500000;
  list.innerHTML = `<div class="ev-empty">正在分析…`
    + (heavy ? `（${state.status.frame_count} 帧逐时间戳精确求值，可能要几分钟；`
             + `需要先看个大概可以打开上方的「快速求值」）` : "")
    + `</div>`;
  const qs = new URLSearchParams();
  qs.set("types", [...state.evTypes].join(","));
  if (state.evFuncs.size) qs.set("functions", [...state.evFuncs].join(","));
  if (state.evQuery) qs.set("q", state.evQuery);
  if (state.evFast) qs.set("fast", "1");
  try {
    const r = await api(`/api/events?${qs}`);
    state.events = r.events || [];
    state.evAllFuncs = r.functions || [];
    state.evTotal = r.total || 0;
    state.evTruncated = !!r.truncated;
  } catch (e) {
    state.events = []; state.evAllFuncs = []; state.evActive = -1; state.evTotal = 0;
    $("evCount").textContent = "";
    $("evFuncs").innerHTML = "";
    list.innerHTML = `<div class="ev-empty">${escapeHtml(e.message)}</div>`;
    renderRibbon();
    return;
  }
  state.evActive = -1;
  renderEventFuncs();
  renderEventList();
  renderRibbon();
  syncActiveEvent(state.t, true);
}

// 「没有事件」有好几种成因，笼统说一句"没有事件"等于把排查甩回给用户
function emptyEventsReason() {
  const funcs = state.functions || [];
  if (state.evFuncs.size || state.evQuery || state.evTypes.size < 3) {
    return "当前筛选下没有事件 —— 放宽功能 chip / 类型 / 搜索词试试";
  }
  if (funcs.length) {
    const dead = funcs.filter(f => !(f.available || []).length);
    if (dead.length === funcs.length) {
      const s = state.status || {};
      return `规格里 ${funcs.length} 个功能引用的信号，在当前日志「${s.log || "?"}」中全都没有数据，` +
             `所以一个事件都算不出来。检查日志是不是装错了（顶栏黄色告警里有细节）。`;
    }
  }
  return "日志跑完了，但没有任何功能触发进入 / 退出 —— 条件可能一次都没满足";
}

function renderEventFuncs() {
  const box = $("evFuncs");
  const funcs = state.evAllFuncs || [];
  if (!funcs.length) { box.innerHTML = ""; return; }
  const total = funcs.reduce((a, f) => a + f.count, 0);
  const sel = state.evFuncs;
  const parts = [`<button class="evchip${sel.size ? "" : " on"}" data-fn="" title="不按功能筛选">全部 <b>${total}</b></button>`];
  for (const f of funcs) {
    // count=0 的功能不隐藏：「这个功能一次都没触发」本身就是调查结论
    const cls = `evchip${sel.has(f.id) ? " on" : ""}${f.count ? "" : " zero"}`;
    const tip = f.count ? f.id : `${f.id}\n全程未触发`;
    parts.push(`<button class="${cls}" data-fn="${escapeHtml(f.id)}" title="${escapeHtml(tip)}">${escapeHtml(f.name)} <b>${f.count}</b></button>`);
  }
  box.innerHTML = parts.join("");
  box.querySelectorAll(".evchip").forEach((el) => el.addEventListener("click", () => {
    const id = el.dataset.fn;
    if (!id) state.evFuncs.clear();
    else if (state.evFuncs.has(id)) state.evFuncs.delete(id);
    else state.evFuncs.add(id);
    refreshEvents();
  }));
}

function renderEventList() {
  const list = $("evList");
  const evs = state.events;
  const shown = Math.min(evs.length, EV_RENDER_CAP);
  const capped = evs.length > shown || state.evTruncated;
  $("evCount").textContent = `(${state.evTotal}${capped ? ` · 显示前 ${shown}` : ""})`;

  if (!evs.length) {
    list.innerHTML = `<div class="ev-empty">${escapeHtml(emptyEventsReason())}</div>`;
    return;
  }
  // 拼一次字符串 + 事件委托（见下方 #evList 的 click 监听），
  // 比 600 次 createElement + addEventListener 快一个数量级
  const parts = [];
  for (let k = 0; k < shown; k++) {
    const e = evs[k];
    const tag = evTag(e.type);
    parts.push(
      `<div class="evrow ${tag.cls}" data-k="${k}">` +
      `<span class="ev-mark ${tag.cls}">${tag.mark}</span>` +
      `<span class="ev-t">${relT(e.t).toFixed(3)}</span>` +
      `<span class="ev-tag ${tag.cls}">${tag.label}</span>` +
      `<span class="ev-fn" title="${escapeHtml(e.function)}">${escapeHtml(e.name)}</span>` +
      `<span class="ev-why" title="${escapeHtml(e.summary)}">${escapeHtml(e.summary)}</span></div>`);
  }
  if (capped) {
    parts.push(`<div class="ev-empty">还有 ${state.evTotal - shown} 条未显示 —— 用上方功能 chip 或搜索框收窄范围</div>`);
  }
  list.innerHTML = parts.join("");
  markActiveRow();
}

$("evList").addEventListener("click", (e) => {
  const k = e.target.closest?.(".evrow")?.dataset.k;
  if (k != null) gotoEvent(parseInt(k, 10));
});

// 点事件 → 光标落到该时刻，并在需要时把 X 窗口平移过去
function gotoEvent(k) {
  const e = state.events[k];
  if (!e) return;
  state.evActive = k;
  markActiveRow(true);
  panToTime(e.t);
  setCursor(e.t);
}

function stepEvent(dir) {
  if (!state.events.length) return;
  let k = state.evActive;
  if (k < 0) k = dir > 0 ? -1 : Math.min(state.events.length, EV_RENDER_CAP);
  k = Math.max(0, Math.min(Math.min(state.events.length, EV_RENDER_CAP) - 1, k + dir));
  gotoEvent(k);
}

// 光标 → 事件：高亮最后一个 t <= 光标 的事件
function syncActiveEvent(t, force = false) {
  if (!state.evFollow && !force) return;
  const evs = state.events;
  if (!evs.length) return;
  const cur = state.evActive >= 0 ? evs[state.evActive] : null;
  if (cur && Math.abs(cur.t - t) < 1e-9) { markActiveRow(force); return; }

  let lo = 0, hi = evs.length - 1, idx = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (evs[mid].t <= t + 1e-9) { idx = mid; lo = mid + 1; } else hi = mid - 1;
  }
  if (idx === state.evActive && !force) return;
  state.evActive = idx;
  markActiveRow(true);
}

function markActiveRow(scroll = false) {
  const list = $("evList");
  const prev = list.querySelector(".evrow.active");
  if (prev) prev.classList.remove("active");
  if (state.evActive >= 0) {
    const el = list.querySelector(`.evrow[data-k="${state.evActive}"]`);
    if (el) {
      el.classList.add("active");
      if (scroll) {
        const lr = list.getBoundingClientRect(), er = el.getBoundingClientRect();
        if (er.top < lr.top || er.bottom > lr.bottom) el.scrollIntoView({ block: "nearest" });
      }
    }
  }
  updateRibbonActive();
}

// ---- 事件 ribbon：当前 X 窗口内事件在时间轴上的投影 ----------------------
function renderRibbon() {
  // 光标线和 ribbon 依赖同一套「时间→像素」映射，凡是重画 ribbon 的时机
  // （缩放、resize、换信号、平移窗口）光标都要跟着挪，放在一起最不容易漏
  renderCursor();
  const track = $("evRibbonTrack");
  const bar = $("evRibbon");
  if (!track || !bar) return;
  // 没勾信号时图表没有 grid，ribbon 无从对齐——直接收起，避免给出错误的位置暗示
  const usable = !!state.chart && state.selected.size > 0 && state.events.length > 0 && state.t1 > state.t0;
  bar.classList.toggle("hidden", !usable);
  if (!usable) { track.innerHTML = ""; return; }

  // 位置一律走 ECharts 自己的坐标换算，不按 [t0,t1] 线性插值：xAxis 开了 scale
  // 且会取整齐刻度，实际轴范围比数据范围宽出一截（EP35 日志约 2 s），线性插值
  // 在两端会偏出好几个像素——对一个卖点就是"和曲线对齐"的控件不能接受。
  // track 宽度贴 #chart 的实际宽度，这样 .charts-scroll 出竖向滚动条时也不会错位。
  const w = $("chart").clientWidth;
  track.style.width = `${w}px`;

  const a = state.t0, b = state.t1;
  // 用两次 convertToPixel 标定映射，而不是每个刻度都调一次：xAxis 是 type:"value"，
  // 时间→像素严格线性，两个点就能定死这条直线。精度和逐个换算完全一样，
  // 但 400 个刻度只需要 2 次 ECharts 调用。
  const px0 = state.chart.convertToPixel({ xAxisIndex: 0 }, a);
  const px1 = state.chart.convertToPixel({ xAxisIndex: 0 }, b);
  if (px0 == null || px1 == null || isNaN(px0) || isNaN(px1) || b <= a) { track.innerHTML = ""; return; }
  const k2px = (px1 - px0) / (b - a);

  const parts = [];
  let drawn = 0;
  for (let k = 0; k < state.events.length && drawn < EV_TICK_CAP; k++) {
    const e = state.events[k];
    if (e.t < a || e.t > b) continue;
    const x = px0 + (e.t - a) * k2px;
    if (isNaN(x) || x < 0 || x > w) continue;
    const tag = evTag(e.type);
    const title = `${relT(e.t).toFixed(3)} s  ${tag.label}  ${e.name}\n${e.summary}`;
    parts.push(`<span class="ev-tick ${tag.cls}" style="left:${x}px" data-k="${k}" title="${escapeHtml(title)}"></span>`);
    drawn++;
  }
  track.innerHTML = parts.join("");
  updateRibbonActive();
}

function updateRibbonActive() {
  const track = $("evRibbonTrack");
  if (!track) return;
  const prev = track.querySelector(".ev-tick.on");
  if (prev) prev.classList.remove("on");
  if (state.evActive < 0) return;
  const el = track.querySelector(`.ev-tick[data-k="${state.evActive}"]`);
  if (el) el.classList.add("on");
}

$("evRibbonTrack").addEventListener("click", (e) => {
  const k = e.target?.dataset?.k;
  if (k != null) gotoEvent(parseInt(k, 10));
});

// 事件时刻落在当前 X 窗口外时平移窗口过去（保持缩放级别）
function panToTime(t) {
  const full = fullRange();
  if (!state.chart || !full || !state.selected.size) return;
  const [lo, hi] = full;
  const a = state.t0, b = state.t1;
  if (!(b > a) || (a <= lo && b >= hi)) return;   // 全量视图：无需平移
  if (t >= a && t <= b) return;                   // 已在窗口内
  const span = b - a;
  let na = t - span / 2, nb = na + span;
  if (na < lo) { na = lo; nb = lo + span; }
  if (nb > hi) { nb = hi; na = Math.max(lo, hi - span); }
  state.t0 = na; state.t1 = nb;
  state.chart.dispatchAction({ type: "dataZoom", startValue: na, endValue: nb, xAxisIndex: xAxisIdx() });
  updateZoomButtons();
  renderRibbon();
}

// ---- 面板控件 -------------------------------------------------------------
$("evTypes").querySelectorAll(".evtype").forEach((btn) => btn.addEventListener("click", () => {
  const ty = btn.dataset.type;
  if (state.evTypes.has(ty)) state.evTypes.delete(ty); else state.evTypes.add(ty);
  btn.classList.toggle("on", state.evTypes.has(ty));
  refreshEvents();
}));

let evSearchTimer = null;
$("evSearch").addEventListener("input", (e) => {
  clearTimeout(evSearchTimer);
  const v = e.target.value.trim();
  evSearchTimer = setTimeout(() => { state.evQuery = v; refreshEvents(); }, 220);
});

$("evFollow").addEventListener("click", () => {
  state.evFollow = !state.evFollow;
  $("evFollow").classList.toggle("on", state.evFollow);
  if (state.evFollow) syncActiveEvent(state.t, true);
});

// 快速求值：只在被引用信号变化的时刻求值。默认关（逐时间戳精确求值），
// 选择记在 localStorage —— 一份大日志里用户往往要来回筛好几轮
const EV_FAST_KEY = "canprobe.evFast";
state.evFast = localStorage.getItem(EV_FAST_KEY) === "1";
$("evFast").classList.toggle("on", state.evFast);
$("evFast").addEventListener("click", () => {
  state.evFast = !state.evFast;
  $("evFast").classList.toggle("on", state.evFast);
  localStorage.setItem(EV_FAST_KEY, state.evFast ? "1" : "0");
  refreshEvents();
});
$("evPrev").addEventListener("click", () => stepEvent(-1));
$("evNext").addEventListener("click", () => stepEvent(1));

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
  renderCursor();
  syncActiveEvent(t);
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
    await refreshStatus();
    await refreshFunctions();
    renderCharts();
    refreshEvents();
    $("specModal").classList.add("hidden");
  } catch (e) {
    $("specMsg").textContent = `错误: ${e.message}`;
  }
});

// ---------------------------------------------------------------------------
// 通信诊断
// ---------------------------------------------------------------------------
const SEV_ORDER = { critical: 0, error: 1, warn: 2, info: 3 };
const SEV_BADGE = { critical: "sev-crit", error: "sev-err", warn: "sev-warn", info: "sev-info" };
const diagState = { report: null, ranking: null, message: null, messageLoading: false, messageError: "" };

async function openDiag() {
  $("diagModal").classList.remove("hidden");
  $("diagBody").innerHTML = '<div class="muted" style="padding:14px">正在诊断…</div>';
  $("diagMessageList").innerHTML = state.messages.map(m =>
    `<option value="0x${m.frame_id.toString(16).toUpperCase()}">${escapeHtml(m.name)}</option>`
  ).join("");
  diagState.report = null;
  diagState.ranking = null;
  diagState.message = null;
  diagState.messageError = "";
  $("diagExport").href = "/api/diag/export?fmt=md";
  const reportRequest = api("/api/diag/report");
  const rankingRequest = api("/api/diag/messages?limit=30");
  try {
    diagState.report = await reportRequest;
    renderDiag();
  } catch (e) {
    $("diagBody").innerHTML = `<div class="diag-error">诊断失败: ${escapeHtml(e.message)}</div>`;
  }
  rankingRequest.then(ranking => {
    diagState.ranking = ranking;
    if (diagState.report) renderDiag();
  }).catch(e => {
    diagState.ranking = [];
    diagState.messageError = `报文排行加载失败: ${e.message}`;
    if (diagState.report) renderDiag();
  });
}

function findingHtml(f) {
  const conf = f.confidence != null && f.confidence < 1
    ? `<span class="chip chip-conf">推断 ${Math.round(f.confidence * 100)}%</span>` : "";
  const tinfo = f.time_start != null
    ? `<span class="diag-time">t=${fmtTime(f.time_start)}~${fmtTime(f.time_end ?? f.time_start)}</span>` : "";
  return `<div class="diag-finding ${SEV_BADGE[f.severity] || ""}" data-t="${f.time_start ?? ""}">
    <div class="diag-finding-head"><span class="sev-badge">${escapeHtml(f.severity)}</span><b>${escapeHtml(f.title)}</b>${conf}${tinfo}</div>
    <div class="diag-finding-body">${escapeHtml(f.explanation)}</div>
    ${f.suggestion ? `<div class="diag-finding-sug">建议：${escapeHtml(f.suggestion)}</div>` : ""}
  </div>`;
}

function renderMessageDiagnosis() {
  if (diagState.messageLoading) return '<div class="muted">正在体检该报文…</div>';
  if (diagState.messageError) return `<div class="diag-error">${escapeHtml(diagState.messageError)}</div>`;
  const report = diagState.message;
  if (!report) return '<div class="muted">输入报文 ID 或名称，查看它在当前日志中的全部异常事实。</div>';

  const ident = report.identity || {};
  const stats = report.stats;
  const per = stats?.period_ms;
  const cells = [
    ["报文", `${ident.hex || "—"}${ident.name ? ` · ${ident.name}` : ""}`],
    ["日志帧数", stats?.frames ?? 0],
    ["实测周期", per ? `${numFmt(per.median)} ms` : "—"],
    ["DBC 周期", ident.dbc_cycle_ms != null ? `${numFmt(ident.dbc_cycle_ms)} ms` : "—"],
    ["时间范围", stats ? `${fmtTime(stats.first)} ~ ${fmtTime(stats.last)}` : "—"],
    ["DLC 分布", stats ? Object.entries(stats.dlc || {}).map(([n, count]) => `${n}B×${count}`).join(" / ") : "—"],
    ["通道", stats?.channels?.join(", ") || "—"],
    ["发送节点", ident.senders?.join(", ") || "—"],
  ];
  let html = `<div class="diag-message-title"><div><b>${escapeHtml(ident.hex || "")}</b> ${escapeHtml(ident.name || "未在 DBC 定义")}</div>`;
  if (report.signals?.length) html += '<button id="diagAddSignals" class="btn">全部信号加入 Graphics</button>';
  html += `</div><div class="diag-grid">${cells.map(([key, value]) =>
    `<div class="diag-cell"><div class="diag-cell-v">${escapeHtml(String(value))}</div><div class="diag-cell-k">${escapeHtml(key)}</div></div>`
  ).join("")}</div>`;
  const findings = [...(report.findings || [])].sort((a, b) =>
    (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9));
  html += `<div class="diag-subtitle">报文结论 <span class="muted">(${findings.length})</span></div>`;
  html += findings.length ? `<div class="diag-findings">${findings.map(findingHtml).join("")}</div>`
    : '<div class="muted">未发现报文级或信号级异常。</div>';

  if (report.signals?.length) {
    html += '<div class="diag-subtitle">信号体检</div><div class="diag-table-wrap"><table class="diag-table"><thead><tr><th>名称</th><th>采样</th><th>变化</th><th>范围</th><th>结论</th></tr></thead><tbody>';
    for (const signal of report.signals) {
      const signalFinding = findings.find(f => (f.entities || []).includes(signal.name) && f.time_start != null);
      const range = signal.min == null ? "—" : `${numFmt(signal.min)} ~ ${numFmt(signal.max)}${signal.unit ? ` ${signal.unit}` : ""}`;
      const issues = signal.issues?.length ? signal.issues.map(issue => `<span class="chip chip-issue">${escapeHtml(issue)}</span>`).join(" ") : '<span class="chip chip-ok">正常</span>';
      html += `<tr data-t="${signalFinding?.time_start ?? ""}"><td>${escapeHtml(signal.name)}</td><td>${signal.samples}</td><td>${signal.changes}</td><td>${escapeHtml(range)}</td><td>${issues}</td></tr>`;
    }
    html += '</tbody></table></div>';
  }
  return html;
}

function renderRanking() {
  if (diagState.ranking == null) return '<div class="muted">正在扫描帧级异常…</div>';
  if (!diagState.ranking.length) return '<div class="muted">未发现需要排行的帧级异常报文。</div>';
  return `<div class="diag-table-wrap"><table class="diag-table diag-rank"><thead><tr><th>报文</th><th>帧数</th><th>周期 / 期望</th><th>缺口</th><th>评分</th><th>摘要</th></tr></thead><tbody>${diagState.ranking.map(row =>
    `<tr data-ident="${row.hex}"><td><b>${row.hex}</b>${row.name ? `<small>${escapeHtml(row.name)}</small>` : ""}</td><td>${row.frames}</td><td>${row.period_ms ?? "—"} / ${row.expected_ms ?? "—"} ms</td><td>${row.gaps}</td><td>${row.score}</td><td>${escapeHtml(row.headline)}</td></tr>`
  ).join("")}</tbody></table></div>`;
}

function renderDiag() {
  const report = diagState.report;
  if (!report) return;
  const cap = report.capabilities || {};
  const sum = report.summary || {};
  const metrics = report.metrics || {};
  const findings = [...(report.findings || [])]
    .sort((a, b) => (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9));

  let html = "";

  html += `<div class="diag-section"><div class="diag-title">报文定位结果</div>${renderMessageDiagnosis()}</div>`;
  html += `<div class="diag-section"><div class="diag-title">问题报文排行</div>${renderRanking()}</div>`;

  // 概览
  const loadMean = (metrics.load_mean_pct || [])[0];
  const loadPeak = (metrics.load_peak_pct || [])[0];
  const cells = [
    ["数据帧", sum.frame_count ?? 0],
    ["时间跨度", `${(sum.span_s ?? 0).toFixed(2)} s`],
    ["报文 ID", sum.message_ids ?? 0],
    ["节点", (sum.nodes || []).join(", ") || "—"],
    ["错误事件", sum.error_events ?? 0],
    ["状态事件", sum.status_events ?? 0],
    ["平均负载", loadMean != null ? `${loadMean} %` : "—"],
    ["峰值负载", loadPeak != null ? `${loadPeak} %` : "—"],
  ];
  html += `<div class="diag-section"><div class="diag-title">概览</div><div class="diag-grid">`;
  for (const [k, v] of cells) html += `<div class="diag-cell"><div class="diag-cell-v">${escapeHtml(String(v))}</div><div class="diag-cell-k">${escapeHtml(k)}</div></div>`;
  html += `</div></div>`;

  // 能力清单
  html += `<div class="diag-section"><div class="diag-title">能力清单</div>`;
  html += `<div class="diag-cap">可用：${(cap.available_checks || []).map(c => `<span class="chip chip-on">${escapeHtml(c)}</span>`).join(" ") || '<span class="muted">—</span>'}</div>`;
  if (cap.unavailable_checks && cap.unavailable_checks.length) {
    html += `<div class="diag-cap">不可用：${cap.unavailable_checks.map(u => `<span class="chip chip-off" title="${escapeHtml(u.reason)}">${escapeHtml(u.check)}</span>`).join(" ")}</div>`;
  }
  html += `</div>`;

  // 诊断结论
  html += `<div class="diag-section"><div class="diag-title">诊断结论 <span class="muted">(${findings.length})</span></div>`;
  if (!findings.length) {
    html += `<div class="muted" style="padding:4px">未发现异常（注意：仅在「可用检查」范围内有效）。</div>`;
  } else {
    html += `<div class="diag-findings">`;
    for (const f of findings) {
      html += findingHtml(f);
    }
    html += `</div>`;
  }
  html += `</div>`;

  $("diagBody").innerHTML = html;

  // 点击结论跳转到对应时刻（证据链落地）
  document.querySelectorAll(".diag-finding[data-t]").forEach((el) => {
    const t = parseFloat(el.dataset.t);
    if (!isNaN(t)) el.addEventListener("click", () => setCursor(t));
  });
  document.querySelectorAll(".diag-table tr[data-t]").forEach(el => {
    const t = parseFloat(el.dataset.t);
    if (!isNaN(t)) el.addEventListener("click", () => setCursor(t));
  });
  document.querySelectorAll(".diag-rank tr[data-ident]").forEach(el => {
    el.addEventListener("click", () => {
      $("diagMsgInput").value = el.dataset.ident;
      locateDiagMessage();
    });
  });
  $("diagAddSignals")?.addEventListener("click", () => {
    const mid = diagState.message?.identity?.id;
    const msg = state.messages.find(item => item.frame_id === mid);
    if (!msg) return;
    for (const signal of msg.signals) state.selected.add(signal.name);
    state.activeFunction = null;
    renderFunctionButtons();
    renderSignalTree();
    renderCharts();
    $("diagModal").classList.add("hidden");
  });
}

async function locateDiagMessage() {
  const ident = $("diagMsgInput").value.trim();
  if (!ident || diagState.messageLoading) return;
  diagState.messageLoading = true;
  diagState.messageError = "";
  renderDiag();
  try {
    diagState.message = await api(`/api/diag/message?ident=${encodeURIComponent(ident)}`);
    $("diagExport").href = `/api/diag/export?fmt=md&ident=${encodeURIComponent(ident)}`;
  } catch (e) {
    diagState.message = null;
    diagState.messageError = `报文定位失败: ${e.message}`;
  } finally {
    diagState.messageLoading = false;
    renderDiag();
  }
}

$("btnDiag").addEventListener("click", openDiag);
$("diagClose").addEventListener("click", () => $("diagModal").classList.add("hidden"));
$("diagMsgLocate").addEventListener("click", locateDiagMessage);
$("diagMsgInput").addEventListener("keydown", e => { if (e.key === "Enter") locateDiagMessage(); });

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

$("btnXIn").addEventListener("click", () => zoomX(1 / X_STEP));
$("btnXOut").addEventListener("click", () => zoomX(X_STEP));
$("btnYIn").addEventListener("click", () => zoomY(Y_STEP));
$("btnYOut").addEventListener("click", () => zoomY(1 / Y_STEP));
$("btnZoomReset").addEventListener("click", resetZoom);

window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.code === "Space") { e.preventDefault(); play(); }
  else if (e.key === "ArrowLeft") setCursor(state.t - 0.1);
  else if (e.key === "ArrowRight") setCursor(state.t + 0.1);
  else if (e.key === ",") { e.preventDefault(); stepEvent(-1); }   // 上一个关键事件
  else if (e.key === ".") { e.preventDefault(); stepEvent(1); }    // 下一个关键事件
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
