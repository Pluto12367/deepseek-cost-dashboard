/* DeepSeek 成本监控面板前端逻辑(深色数据工作台) */
const $ = (s) => document.querySelector(s);

/* ---------- 主题 ---------- */

const THEME_KEY = "dscd-theme";

function currentTheme() {
  return document.documentElement.dataset.theme || "dark";
}

function themeColors() {
  const dark = currentTheme() === "dark";
  return {
    tick: dark ? "#8b93a7" : "#6b7280",
    grid: dark ? "rgba(255,255,255,.06)" : "rgba(20,25,40,.08)",
    border: dark ? "rgba(255,255,255,.10)" : "rgba(20,25,40,.12)",
    legend: dark ? "#8b93a7" : "#6b7280",
    total: dark ? "#e8eaf0" : "#1a2030",
    center: dark ? "#e8eaf0" : "#1a2030",
  };
}

$("#theme-toggle")?.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem(THEME_KEY, next);
  $("#theme-toggle").textContent = next === "dark" ? "🌙 深色" : "☀️ 浅色";
  loadCharts(); // 图表配色随主题重绘
});

(function initTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved) document.documentElement.dataset.theme = saved;
  document.addEventListener("DOMContentLoaded", () => {
    $("#theme-toggle").textContent =
      currentTheme() === "dark" ? "🌙 深色" : "☀️ 浅色";
  });
})();

/* ---------- 常量与格式化 ---------- */

const CAT_NAMES = {
  cost_tokens: "模型 Token", cost_search: "联网搜索", cost_fetch: "网页抓取",
  cost_ocr: "OCR", cost_asr: "ASR", cost_tts: "TTS",
};
const CAT_COLORS = {
  cost_tokens: "#637bff", cost_search: "#34d399", cost_fetch: "#fbbf24",
  cost_ocr: "#a78bfa", cost_asr: "#f472b6", cost_tts: "#22d3ee",
};

let charts = {};
let pricesData = null;
let keysCache = []; // [{key_id, name, masked, label, calls, cost}]
let page = 0;
const PAGE_SIZE = 20;
let dailyDates = [];        // 每日图表对应完整日期,供柱子下钻
let lastSeenMaxId = null;   // 新调用行闪烁
let lastWarnText = "";      // 桌面通知去重
let thresholdCache = 0.5;   // 单笔高价标红阈值

const fmtMoney = (n, d = 2) => `¥${Number(n || 0).toFixed(d)}`;
const fmtCost = (n) => {
  n = Number(n || 0);
  if (n === 0) return "¥0";
  if (n >= 0.01) return `¥${n.toFixed(4)}`;
  return `¥${n.toFixed(6)}`;
};
const fmtN = (n) => {
  n = Number(n || 0);
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(Math.round(n));
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("未授权"); }
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function flashMsg(text, ok = true) {
  const el = $("#msg");
  el.textContent = text;
  el.style.color = ok ? "var(--green)" : "var(--red)";
  setTimeout(() => (el.textContent = ""), 3000);
}

/* ---------- 概览 ---------- */

async function loadSummary() {
  const s = await api("/api/summary");
  $("#c-today").textContent = fmtMoney(s.today_cost);
  $("#c-today-n").textContent = `${s.today_calls} 次调用`;
  $("#c-month").textContent = fmtMoney(s.month_cost);
  $("#c-month-n").textContent = `${s.month_calls} 次调用`;
  $("#c-total").textContent = fmtMoney(s.total_cost);
  $("#c-total-n").textContent = `${s.total_calls} 次调用`;

  // 浏览器标签页常显今日花费
  document.title = `¥${Number(s.today_cost).toFixed(2)} 今日 · 成本面板`;

  // 首跑引导:还没有任何调用时展示
  $("#onboard").hidden = s.total_calls !== 0;

  // 超预算桌面通知(内容变化才发,避免打扰)
  if (s.budget_warning && s.budget_warning !== lastWarnText) {
    lastWarnText = s.budget_warning;
    if (localStorage.getItem("dscd-notify") === "1" &&
        typeof Notification !== "undefined" && Notification.permission === "granted") {
      new Notification("DeepSeek 成本告警", { body: s.budget_warning });
    }
  } else if (!s.budget_warning) {
    lastWarnText = "";
  }

  // 今日 vs 昨日趋势(全局口径,不受筛选影响)
  const trend = $("#c-today-trend");
  try {
    const daily = await api("/api/daily?days=2");
    const today = new Date().toISOString().slice(0, 10);
    const yest = daily.filter((r) => r.d !== today).at(-1);
    if (yest && Number(yest.cost_total) > 0 && s.today_calls > 0) {
      const pct = (s.today_cost - Number(yest.cost_total)) / Number(yest.cost_total) * 100;
      if (Math.abs(pct) < 1) {
        trend.textContent = "· 持平较昨日";
        trend.className = "trend";
        trend.style.color = "var(--muted)";
      } else {
        trend.textContent = `${pct >= 0 ? "▲" : "▼"} ${Math.abs(pct).toFixed(0)}% 较昨日`;
        trend.className = `trend ${pct >= 0 ? "up" : "down"}`;
        trend.style.color = "";
      }
    } else {
      trend.textContent = "";
    }
  } catch { trend.textContent = ""; }

  // 预算:细进度条 + 状态徽章
  const fill = $("#budget-fill");
  const pill = $("#budget-pill");
  const note = $("#budget-note");
  const warn = $("#budget-warning");
  $("#budget-warning").textContent = s.budget_warning || "";
  if (s.budget > 0) {
    const pct = Math.min(100, (s.month_cost / s.budget) * 100);
    const cls = pct >= 100 ? "r" : pct >= 80 ? "a" : "g";
    const color = pct >= 100 ? "var(--red)" : pct >= 80 ? "var(--amber)" : "var(--green)";
    fill.style.width = `${pct}%`;
    fill.style.background = color;
    pill.className = `pill ${cls}`;
    pill.textContent = `${pct.toFixed(0)}%`;
    note.innerHTML = `本月已用 <b>${fmtMoney(s.month_cost)}</b> / 预算 <b>${fmtMoney(s.budget)}</b>`;
    if (!$("#budget-input").value) $("#budget-input").value = s.budget;
  } else {
    fill.style.width = "0";
    pill.className = "pill";
    pill.textContent = "--";
    warn.textContent = "";
  }

  // 月底费用预测
  const fnote = $("#forecast-note");
  if (s.total_calls > 0 && s.forecast_daily_avg > 0) {
    const over = s.budget > 0 && s.forecast_month > s.budget;
    fnote.innerHTML =
      `按近 7 天日均 <b>${fmtMoney(s.forecast_daily_avg)}</b> 推算,` +
      `月底预计 <b style="color:${over ? "var(--red)" : "inherit"}">¥${Number(s.forecast_month).toFixed(2)}</b>` +
      (s.budget > 0 ? `(预算 ${fmtMoney(s.budget)} 的 ${(s.forecast_month / s.budget * 100).toFixed(0)}%)` : "") +
      ` · 距月底 ${s.days_remaining} 天`;
  } else {
    fnote.innerHTML = "";
  }
}

async function loadBalance() {
  try {
    const b = await api("/api/balance");
    if (b.available && b.balance_infos.length) {
      const info = b.balance_infos[0];
      const symbol = info.currency === "CNY" ? "¥" : "$";
      $("#c-balance").textContent = `${symbol}${Number(info.total_balance).toFixed(2)}`;
      $("#c-balance-foot").textContent =
        `充值 ${Number(info.topped_up_balance).toFixed(2)} · 赠金 ${Number(info.granted_balance).toFixed(2)}`;
    } else {
      $("#c-balance").textContent = "--";
      $("#c-balance-foot").textContent = b.reason || "未配置 Key";
    }
  } catch {
    $("#c-balance-foot").textContent = "余额查询失败";
  }
}

/* ---------- 图表 ---------- */

function setEmpty(id, show) {
  $(id)?.classList.toggle("show", show);
}

function makeChart(id, cfg) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), cfg);
}

// 环形图中心显示总费用
const centerTotal = {
  id: "centerTotal",
  afterDraw(chart) {
    if (chart.config.type !== "doughnut" || !chart.data.datasets[0]?.data.length) return;
    const tc = themeColors();
    const { ctx, chartArea } = chart;
    const x = (chartArea.left + chartArea.right) / 2;
    const y = (chartArea.top + chartArea.bottom) / 2;
    const total = chart.data.datasets[0].data.reduce((s, v) => s + Number(v || 0), 0);
    ctx.save();
    ctx.textAlign = "center";
    ctx.fillStyle = tc.center;
    ctx.font = "700 17px 'Segoe UI', 'Microsoft YaHei', sans-serif";
    ctx.fillText(fmtMoney(total), x, y - 1);
    ctx.fillStyle = tc.tick;
    ctx.font = "10px 'Segoe UI', 'Microsoft YaHei', sans-serif";
    ctx.fillText("统计期内总费用", x, y + 16);
    ctx.restore();
  },
};

function chartFilters() {
  const p = new URLSearchParams();
  const m = $("#f-model").value.trim();
  const e = $("#f-endpoint").value.trim();
  const s = $("#f-source").value;
  const k = $("#f-key").value;
  const d = $("#f-date").value;
  if (d) {
    p.set("date", d); // 精确到某天时不再叠加 days 范围
  } else {
    p.set("days", $("#f-days").value || "365");
  }
  if (m) p.set("model", m);
  if (e) p.set("endpoint", e);
  if (s) p.set("source", s);
  if (k) p.set("key_id", k);
  return p;
}

async function loadCharts() {
  const p = chartFilters();
  const tc = themeColors();
  const tooltipStyle = {
    backgroundColor: currentTheme() === "dark" ? "#1c212b" : "#ffffff",
    titleColor: currentTheme() === "dark" ? "#e8eaf0" : "#1a2030",
    bodyColor: currentTheme() === "dark" ? "#c6cbdb" : "#4b5563",
    borderColor: currentTheme() === "dark" ? "#262c38" : "#e4e7f0",
    borderWidth: 1,
    padding: 10,
  };

  const daily = await api(`/api/daily?${p}`);
  const hasDaily = daily.length > 0;
  setEmpty("#empty-daily", !hasDaily);
  dailyDates = daily.map((r) => r.d); // 供柱子下钻
  const labels = daily.map((r) => r.d.slice(5));
  const datasets = Object.keys(CAT_NAMES).map((k) => ({
    label: CAT_NAMES[k],
    data: daily.map((r) => r[k]),
    backgroundColor: CAT_COLORS[k],
    stack: "cost",
    borderWidth: 0,
    borderRadius: 3,
    barPercentage: 0.7,
  }));
  datasets.push({
    label: "合计",
    type: "line",
    data: daily.map((r) => r.cost_total),
    borderColor: tc.total,
    borderWidth: 1.2,
    pointRadius: 0,
    tension: 0.35,
  });
  makeChart("#chart-daily", {
    type: "bar",
    data: { labels, datasets },
    options: {
      maintainAspectRatio: false,
      onClick(evt, els) { // 点击某天柱子 → 日志精确跳到那天
        if (!els.length || !dailyDates[els[0].index]) return;
        $("#f-date").value = dailyDates[els[0].index];
        page = 0;
        refreshAll();
        $("#calls-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      },
      onHover(evt, els) { // 有柱子的地方显示手型,提示可点
        evt.native.target.style.cursor = els.length ? "pointer" : "default";
      },
      scales: {
        x: { stacked: true, grid: { display: false }, border: { color: tc.border },
             ticks: { color: tc.tick, font: { size: 10 } } },
        y: { stacked: true, beginAtZero: true,
             grid: { color: tc.grid }, border: { display: false },
             ticks: { color: tc.tick, font: { size: 10 }, maxTicksLimit: 6,
                      callback: (v) => `¥${v}` } },
      },
      plugins: {
        legend: { labels: { boxWidth: 8, boxHeight: 8, color: tc.legend,
                            font: { size: 10 }, usePointStyle: true, pointStyle: "circle" } },
        tooltip: { ...tooltipStyle,
                   callbacks: { label: (c) => `${c.dataset.label}: ${fmtCost(c.raw)}` } },
      },
    },
  });

  const bd = await api(`/api/breakdown?${p}`);
  setEmpty("#empty-breakdown", bd.length === 0);
  makeChart("#chart-breakdown", {
    type: "doughnut",
    data: {
      labels: bd.map((r) => r.category),
      datasets: [{
        data: bd.map((r) => r.cost),
        backgroundColor: bd.map((r) => CAT_COLORS[r.key] || "#999"),
        borderWidth: 2,
        borderColor: currentTheme() === "dark" ? "#161a22" : "#ffffff",
        hoverOffset: 6,
      }],
    },
    options: {
      maintainAspectRatio: false,
      cutout: "68%",
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 8, boxHeight: 8,
                   color: tc.legend, font: { size: 10 }, usePointStyle: true,
                   pointStyle: "circle", padding: 10 } },
        tooltip: { ...tooltipStyle,
                   callbacks: { label: (c) => `${c.label}: ${fmtCost(c.raw)}` } },
      },
    },
    plugins: [centerTotal],
  });

  const models = await api(`/api/models?${p}`);
  setEmpty("#empty-models", models.length === 0);
  const mGrad = models.map((_, i) => CAT_COLORS[Object.keys(CAT_NAMES)[i % 6]]);
  makeChart("#chart-models", {
    type: "bar",
    data: {
      labels: models.map((r) => r.model),
      datasets: [{ label: "费用", data: models.map((r) => r.cost),
                   backgroundColor: "#637bff", borderRadius: 6, barPercentage: 0.55 }],
    },
    options: {
      maintainAspectRatio: false,
      scales: {
        x: { grid: { display: false }, border: { color: tc.border },
             ticks: { color: tc.tick, font: { size: 10 } } },
        y: { beginAtZero: true, grid: { color: tc.grid }, border: { display: false },
             ticks: { color: tc.tick, font: { size: 10 }, maxTicksLimit: 6,
                      callback: (v) => `¥${v}` } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { ...tooltipStyle,
                   callbacks: { label: (c) => `${fmtCost(c.raw)}(${models[c.dataIndex].calls} 次)` } },
      },
    },
  });
  void mGrad; // 预留:按模型着色
}

/* ---------- Key 分组 ---------- */

async function loadKeys() {
  const p = chartFilters();
  keysCache = await api(`/api/keys?${p}`);
  renderKeys();
  renderKeyOptions();
}

function renderKeys() {
  const body = $("#keys-body");
  body.innerHTML = "";
  const total = keysCache.reduce((s, k) => s + Number(k.cost || 0), 0);
  for (const k of keysCache) {
    const tr = document.createElement("tr");
    const pct = total > 0 ? `${(Number(k.cost) / total * 100).toFixed(1)}%` : "-";
    tr.innerHTML = `
      <td>${k.name || "(未带 Key)"}</td>
      <td class="hint" style="margin:0">${k.masked || "-"}</td>
      <td><input type="text" class="keys-input" value="${(k.label || "").replace(/"/g, "&quot;")}"
           placeholder="自定义标签" data-key-id="${k.key_id}"></td>
      <td class="r num">${k.calls}</td>
      <td class="r num cost">${fmtCost(k.cost)}</td>
      <td class="r num">${pct}</td>`;
    body.appendChild(tr);
  }
  if (!keysCache.length) {
    body.innerHTML = `<tr><td colspan="6" class="hint" style="text-align:center">暂无数据</td></tr>`;
  }
  body.querySelectorAll("input[data-key-id]").forEach((inp) => {
    inp.addEventListener("change", async () => {
      await post("/api/keys/label", { key_id: inp.dataset.keyId, label: inp.value });
      flashMsg("label 已保存");
      await loadKeys();
    });
  });
}

function renderKeyOptions() {
  const sel = $("#f-key");
  const cur = sel.value;
  const opts = [`<option value="">全部 Key</option>`];
  for (const k of keysCache) {
    if (!k.key_id) continue;
    opts.push(`<option value="${k.key_id}">${k.name || k.masked}</option>`);
  }
  sel.innerHTML = opts.join("");
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

/* ---------- 调用日志 ---------- */

async function loadCalls() {
  const p = chartFilters();
  p.set("limit", PAGE_SIZE);
  p.set("offset", page * PAGE_SIZE);
  const { rows, total } = await api(`/api/calls?${p}`);

  const body = $("#calls-body");
  body.innerHTML = "";
  let maxId = lastSeenMaxId;
  for (const r of rows) {
    const tr = document.createElement("tr");
    if (r.status_code >= 400) tr.className = "err";
    if (lastSeenMaxId !== null && r.id > lastSeenMaxId) tr.classList.add("fresh"); // 新调用闪烁
    if (maxId === null || r.id > maxId) maxId = r.id;
    const tokens = `${fmtN(r.input_cache_hit)} / ${fmtN(r.input_cache_miss)} / ${fmtN(r.output_tokens)}`;
    const tools = `${fmtN(r.search_calls)} / ${fmtN(r.image_search_calls)} / ${fmtN(r.fetch_calls)}`;
    const keyInfo = keysCache.find((k) => k.key_id === r.key_id);
    const keyName = keyInfo ? keyInfo.name : (r.key_id || "-");
    const others = [];
    if (r.ocr_pages) others.push(`${fmtN(r.ocr_pages)} 页`);
    if (r.asr_seconds) others.push(`${Math.round(r.asr_seconds)}s 音频`);
    if (r.tts_chars) others.push(`${fmtN(r.tts_chars)} 字`);
    // 只显示非零费用项,消除 ¥0 噪音
    const parts = [];
    if (r.cost_tokens) parts.push(`token ${fmtCost(r.cost_tokens)}`);
    if (r.cost_search) parts.push(`搜索 ${fmtCost(r.cost_search)}`);
    if (r.cost_fetch) parts.push(`抓取 ${fmtCost(r.cost_fetch)}`);
    if (r.cost_ocr) parts.push(`OCR ${fmtCost(r.cost_ocr)}`);
    if (r.cost_asr) parts.push(`ASR ${fmtCost(r.cost_asr)}`);
    if (r.cost_tts) parts.push(`TTS ${fmtCost(r.cost_tts)}`);
    const detail = parts.length ? parts.join(" · ")
                                : `<span class="zero">–</span>`;
    const detailFull = ["token", "搜索", "抓取", "OCR", "ASR", "TTS"].join(" · ");
    const high = Number(r.cost_total) >= thresholdCache && Number(r.cost_total) > 0;
    const errBadge = r.error
      ? `<span class="badge err" data-err="1" style="cursor:pointer" title="点击展开错误详情">${r.status_code} ⓘ</span>`
      : "";
    const streamBadge = r.stream
      ? `<span class="badge stream">流式</span>` : `<span class="badge ok">完成</span>`;
    const src = r.source === "demo" ? `<span class="badge demo">演示</span> ` : "";
    // 模型/Key/端点可点击下钻筛选
    const modelCell = r.model
      ? `<span class="linkish" data-drill="model" data-val="${r.model}">${r.model}</span>` : "-";
    const keyCell = r.key_id
      ? `<span class="linkish" data-drill="key" data-val="${r.key_id}">${keyName}</span>` : "-";
    const epCell = `<span class="linkish" data-drill="endpoint" data-val="${r.endpoint}">${r.endpoint}</span>`;
    tr.innerHTML = `
      <td>${r.ts}</td>
      <td title="${r.protocol}">${epCell}</td>
      <td>${modelCell}</td>
      <td>${keyCell}</td>
      <td class="r num">${tokens}</td>
      <td class="r num">${tools}</td>
      <td>${others.join(" · ") || `<span class="zero">–</span>`}</td>
      <td title="${detailFull}">${detail}</td>
      <td class="r num cost ${high ? "high" : ""}"
          ${high ? `title="单笔费用达到告警阈值 ${fmtMoney(thresholdCache)}"` : ""}>${fmtCost(r.cost_total)}${high ? " ⚠" : ""}</td>
      <td>${src}${errBadge || streamBadge}${r.latency_ms ? ` <span class="zero">${r.latency_ms}ms</span>` : ""}</td>`;
    body.appendChild(tr);
    if (r.error) { // 错误详情行(点击徽章展开/收起)
      const detailRow = document.createElement("tr");
      detailRow.className = "err-detail";
      detailRow.hidden = true;
      const safe = String(r.error).replace(/&/g, "&amp;")
        .replace(/</g, "&lt;").replace(/>/g, "&gt;");
      detailRow.innerHTML = `<td colspan="10"><pre>${safe}</pre></td>`;
      body.appendChild(detailRow);
      tr.querySelector("[data-err]").addEventListener("click", () => {
        detailRow.hidden = !detailRow.hidden;
      });
    }
  }
  lastSeenMaxId = maxId;

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  $("#page-info").textContent = `第 ${page + 1} / ${totalPages} 页 · 共 ${total} 条`;
  $("#prev").disabled = page <= 0;
  $("#next").disabled = page >= totalPages - 1;

  $("#export-btn").href = `/api/calls/export.csv?${chartFilters()}`;
}

/* ---------- 价格表编辑 ---------- */

function setPath(obj, path, val) {
  const ks = path.split(".");
  let o = obj;
  for (let i = 0; i < ks.length - 1; i++) o = o[ks[i]];
  o[ks[ks.length - 1]] = val;
}

const TOOL_LABELS = {
  web_search_requests: "联网搜索", tool_search: "深度搜索",
  tool_image_search: "图片搜索", web_fetch_requests: "网页抓取", tool_fetch: "深度抓取",
};

function priceCard(title, rows) {
  const items = rows
    .map(([label, path, value]) =>
      `<div class="row"><span>${label}</span>
       <input type="number" step="0.0001" data-path="${path}" value="${value}"></div>`)
    .join("");
  return `<div class="price-card"><h3>${title}</h3>${items}</div>`;
}

function renderPrices() {
  const P = pricesData;
  const el = $("#prices-editor");
  const parts = [];
  for (const [name, m] of Object.entries(P.models)) {
    parts.push(priceCard(
      `模型:${name}${m.alias?.length ? `(别名 ${m.alias.join("/")})` : ""}(元/百万 tokens)`,
      [
        ["缓存命中 · 高峰", `models.${name}.input_cache_hit.peak`, m.input_cache_hit.peak],
        ["缓存命中 · 空闲", `models.${name}.input_cache_hit.off_peak`, m.input_cache_hit.off_peak],
        ["未命中 · 高峰", `models.${name}.input_cache_miss.peak`, m.input_cache_miss.peak],
        ["未命中 · 空闲", `models.${name}.input_cache_miss.off_peak`, m.input_cache_miss.off_peak],
        ["输出 · 高峰", `models.${name}.output.peak`, m.output.peak],
        ["输出 · 空闲", `models.${name}.output.off_peak`, m.output.off_peak],
      ]));
  }
  parts.push(priceCard("工具(元/千次)",
    Object.keys(P.tools_per_1k).map((k) => [TOOL_LABELS[k] || k, `tools_per_1k.${k}`, P.tools_per_1k[k]])));
  parts.push(priceCard("OCR(元/千页)",
    [["单价", "standalone.ocr.price_per_1k_pages", P.standalone.ocr.price_per_1k_pages]]));
  parts.push(priceCard("ASR(元/小时)", [
    ["刊例价", "standalone.asr.price_per_hour", P.standalone.asr.price_per_hour],
    ["折扣价", "standalone.asr.discount_price_per_hour", P.standalone.asr.discount_price_per_hour],
  ]));
  parts.push(priceCard("TTS(元/万字符)", [
    ["刊例价", "standalone.tts.price_per_10k_chars", P.standalone.tts.price_per_10k_chars],
    ["折扣价", "standalone.tts.discount_price_per_10k_chars", P.standalone.tts.discount_price_per_10k_chars],
  ]));
  el.innerHTML = `<div class="price-grid">${parts.join("")}</div>`;
  $("#p-discount").checked = !!P.use_discount;
}

async function loadPrices() {
  pricesData = await api("/api/prices");
  renderPrices();
}

async function savePrices() {
  const clone = JSON.parse(JSON.stringify(pricesData));
  document.querySelectorAll("#prices-editor input[data-path]").forEach((inp) => {
    const v = parseFloat(inp.value);
    if (!Number.isNaN(v)) setPath(clone, inp.dataset.path, v);
  });
  clone.use_discount = $("#p-discount").checked;
  await post("/api/prices", clone);
  flashMsg("价格表已保存,立即生效");
  await Promise.all([loadCharts(), loadCalls()]);
}

/* ---------- 设置 ---------- */

async function loadSettings() {
  const s = await api("/api/settings");
  $("#budget-input").value = s.monthly_budget ?? "";
  $("#key-masked").textContent = s.has_key ? `已保存:${s.api_key_masked}` : "";
  $("#inject-usage").checked = !!s.inject_usage;
  $("#webhook-url").value = s.webhook_url || "";
  $("#webhook-type").value = s.webhook_type || "generic";
  $("#webhook-last").textContent = s.webhook_last_result || "";
  $("#cost-threshold").value = s.cost_alert_threshold;
  $("#retention-days").value = s.retention_days;
  thresholdCache = Number(s.cost_alert_threshold) || 0.5;
  $("#token-state").textContent = s.has_access_token
    ? (s.token_from_env ? "已启用(由环境变量 DSCD_TOKEN 配置)" : "已启用")
    : "未启用(本地模式,所有请求放行)";
  $("#access-token").placeholder = s.has_access_token && !s.token_from_env ? "输入新令牌可覆盖" : "设置访问令牌";
}

async function saveAccessToken() {
  const v = $("#access-token").value.trim();
  if (!v) return flashMsg("令牌不能为空", false);
  await post("/api/settings", { access_token: v });
  $("#access-token").value = "";
  flashMsg("访问令牌已启用,本浏览器已自动登录");
  await loadSettings();
}

async function clearAccessToken() {
  await post("/api/settings", { access_token: "" });
  flashMsg("访问令牌已清除,回到本地开放模式");
  await loadSettings();
}

async function saveBudget() {
  const v = $("#budget-input").value.trim();
  await post("/api/settings", { monthly_budget: v === "" ? null : parseFloat(v) });
  flashMsg("预算已保存");
  await loadSummary();
}

async function saveKey() {
  const v = $("#api-key").value.trim();
  await post("/api/settings", { api_key: v });
  $("#api-key").value = "";
  flashMsg(v ? "API Key 已保存" : "API Key 已清除");
  await Promise.all([loadSettings(), loadBalance()]);
}

async function saveInject() {
  await post("/api/settings", { inject_usage: $("#inject-usage").checked });
  flashMsg(`流式注入已${$("#inject-usage").checked ? "开启" : "关闭"}`);
}

async function saveWebhook() {
  await post("/api/settings", { webhook_url: $("#webhook-url").value.trim() });
  flashMsg("Webhook URL 已保存");
  await loadSettings();
}

async function testWebhook() {
  const r = await post("/api/webhook/test");
  $("#webhook-last").textContent = r.result || r.reason || "";
  flashMsg(r.ok ? "测试通知已发送" : "发送失败,见右侧结果", r.ok);
}

async function saveThreshold() {
  const v = parseFloat($("#cost-threshold").value);
  await post("/api/settings", { cost_alert_threshold: v > 0 ? v : 0.5 });
  thresholdCache = v > 0 ? v : 0.5;
  flashMsg("高价告警阈值已保存");
  await loadCalls();
}

async function saveRetention() {
  const v = parseInt($("#retention-days").value, 10);
  await post("/api/settings", { retention_days: v > 0 ? v : 90 });
  flashMsg("数据保留期已保存");
}

async function cleanupNow() {
  const r = await post("/api/cleanup");
  $("#cleanup-msg").textContent = `已清理 ${r.removed} 条 ${r.days} 天前的记录`;
  flashMsg(`清理完成:${r.removed} 条`);
  await refreshAll();
}

async function saveWebhookType() {
  await post("/api/settings", { webhook_type: $("#webhook-type").value });
  flashMsg("机器人类型已保存");
}

async function toggleDesktopNotify() {
  const on = $("#notify-desktop").checked;
  localStorage.setItem("dscd-notify", on ? "1" : "0");
  if (on && typeof Notification !== "undefined" &&
      Notification.permission !== "granted") {
    const perm = await Notification.requestPermission();
    flashMsg(perm === "granted" ? "桌面通知已开启" : "浏览器拒绝了通知权限",
             perm === "granted");
    if (perm !== "granted") $("#notify-desktop").checked = false;
  } else {
    flashMsg(`桌面通知已${on ? "开启" : "关闭"}`);
  }
}

/* ---------- 首跑引导与接入片段 ---------- */

async function seedDemo() {
  await post("/api/demo/seed");
  flashMsg("演示数据已生成");
  page = 0;
  await refreshAll();
}

async function wipeDemo() {
  await post("/api/demo/wipe");
  flashMsg("演示数据已清除");
  page = 0;
  lastSeenMaxId = null;
  await refreshAll();
}

function copySnippet(id, btn) {
  const text = document.getElementById(id).innerText;
  const done = () => {
    btn.textContent = "已复制 ✓";
    setTimeout(() => (btn.textContent = "复制"), 1500);
  };
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch { /* 忽略 */ }
  document.body.removeChild(ta);
}

/* ---------- 事件与启动 ---------- */

$("#refresh").addEventListener("click", () => refreshAll());
$("#f-apply").addEventListener("click", () => { page = 0; refreshAll(); });
$("#prev").addEventListener("click", () => { if (page > 0) { page--; loadCalls(); } });
$("#next").addEventListener("click", () => { page++; loadCalls(); });
$("#budget-save").addEventListener("click", saveBudget);
$("#key-save").addEventListener("click", saveKey);
$("#prices-save").addEventListener("click", savePrices);
$("#inject-usage").addEventListener("change", saveInject);
$("#webhook-save").addEventListener("click", saveWebhook);
$("#webhook-test").addEventListener("click", testWebhook);
$("#webhook-type").addEventListener("change", saveWebhookType);
$("#cost-threshold").addEventListener("change", saveThreshold);
$("#retention-days").addEventListener("change", saveRetention);
$("#cleanup-now").addEventListener("click", cleanupNow);
$("#notify-desktop").addEventListener("change", toggleDesktopNotify);
$("#demo-seed").addEventListener("click", seedDemo);
$("#demo-wipe").addEventListener("click", wipeDemo);
$("#token-save").addEventListener("click", saveAccessToken);
$("#token-clear").addEventListener("click", clearAccessToken);

// 筛选变更即生效(免点查询);文本输入做 400ms 防抖
let filterTimer = null;
const autoApply = () => { page = 0; refreshAll(); };
const debouncedApply = () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(autoApply, 400);
};
$("#f-days").addEventListener("change", autoApply);
$("#f-key").addEventListener("change", autoApply);
$("#f-source").addEventListener("change", autoApply);
$("#f-date").addEventListener("change", autoApply);
$("#f-model").addEventListener("input", debouncedApply);
$("#f-endpoint").addEventListener("input", debouncedApply);

// 日志表:点击模型/Key/端点 → 自动填入筛选并刷新(下钻)
$("#calls-body").addEventListener("click", (evt) => {
  const t = evt.target.closest("[data-drill]");
  if (!t) return;
  const kind = t.dataset.drill;
  const val = t.dataset.val;
  if (kind === "model") $("#f-model").value = ($("#f-model").value === val ? "" : val);
  if (kind === "endpoint") $("#f-endpoint").value = ($("#f-endpoint").value === val ? "" : val);
  if (kind === "key") {
    const sel = $("#f-key");
    if ([...sel.options].some((o) => o.value === val)) sel.value = (sel.value === val ? "" : val);
    else sel.value = ""; // 该 Key 不在当前统计列表里时,退回全部
  }
  page = 0;
  refreshAll();
});

// Key 分组面板:点 Key 名也下钻
$("#keys-body").addEventListener("click", (evt) => {
  const t = evt.target.closest("tr");
  if (!t || !t.querySelector("input[data-key-id]")) return;
  const kid = t.querySelector("input[data-key-id]").dataset.keyId;
  const sel = $("#f-key");
  if ([...sel.options].some((o) => o.value === kid)) {
    sel.value = kid === sel.value ? "" : kid;
    page = 0;
    refreshAll();
  }
});

// 接入片段复制
document.querySelectorAll(".copy-btn").forEach((btn) => {
  btn.addEventListener("click", () => copySnippet(btn.dataset.copy, btn));
});

/* ---------- 自动刷新(live tail) ---------- */

let autoTimer = null;

function setAutoRefresh(sec) {
  clearInterval(autoTimer);
  autoTimer = null;
  if (Number(sec) > 0) {
    autoTimer = setInterval(() => {
      loadSummary();
      loadBalance();
      if (page === 0) loadCalls(); // 翻页查看历史时不打扰
      if (Number(sec) >= 30) loadCharts();
    }, Number(sec) * 1000);
  }
}

$("#auto-refresh").addEventListener("change", () => {
  const sec = $("#auto-refresh").value;
  localStorage.setItem("dscd-auto", sec);
  setAutoRefresh(sec);
});

(function initAutoRefresh() {
  const saved = localStorage.getItem("dscd-auto") ?? "30";
  $("#auto-refresh").value = saved;
  setAutoRefresh(saved);
})();

async function refreshAll() {
  await loadKeys(); // 日志表的 Key 列依赖 keysCache,需先加载
  await Promise.all([loadSummary(), loadCharts(), loadCalls()]);
}

setInterval(() => {
  $("#clock").textContent = new Date().toLocaleString("zh-CN", { hour12: false });
}, 1000);

loadSettings();
loadPrices();
refreshAll();
loadBalance();
setInterval(() => { loadSummary(); loadBalance(); }, 60000);
