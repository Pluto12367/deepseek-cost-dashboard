/* DeepSeek 成本监控面板前端逻辑 */
const $ = (s) => document.querySelector(s);

const CAT_NAMES = {
  cost_tokens: "模型 Token", cost_search: "联网搜索", cost_fetch: "网页抓取",
  cost_ocr: "OCR", cost_asr: "ASR", cost_tts: "TTS",
};
const CAT_COLORS = {
  cost_tokens: "#4f6ef7", cost_search: "#22c55e", cost_fetch: "#f59e0b",
  cost_ocr: "#8b5cf6", cost_asr: "#ec4899", cost_tts: "#14b8a6",
};

let charts = {};
let pricesData = null;
let keysCache = []; // [{key_id, name, masked, label, calls, cost}]
let page = 0;
const PAGE_SIZE = 20;

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

  const fill = $("#budget-fill");
  const note = $("#budget-note");
  const warn = $("#budget-warning");
  $("#budget-warning").textContent = s.budget_warning || "";
  if (s.budget > 0) {
    const pct = Math.min(100, (s.month_cost / s.budget) * 100);
    fill.style.width = `${pct}%`;
    fill.style.background = pct >= 100 ? "var(--red)" : pct >= 80 ? "var(--amber)" : "var(--green)";
    note.textContent = `本月已用 ${fmtMoney(s.month_cost)} / 预算 ${fmtMoney(s.budget)}(${pct.toFixed(1)}%)`;
    if (!$("#budget-input").value) $("#budget-input").value = s.budget;
  } else {
    fill.style.width = "0";
    warn.textContent = "";
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
  } catch (e) {
    $("#c-balance-foot").textContent = "余额查询失败";
  }
}

/* ---------- 图表 ---------- */

function makeChart(id, cfg) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), cfg);
}

function chartFilters() {
  const p = new URLSearchParams();
  p.set("days", $("#f-days").value || "365");
  const m = $("#f-model").value.trim();
  const e = $("#f-endpoint").value.trim();
  const s = $("#f-source").value;
  const k = $("#f-key").value;
  if (m) p.set("model", m);
  if (e) p.set("endpoint", e);
  if (s) p.set("source", s);
  if (k) p.set("key_id", k);
  return p;
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
      <td class="hint">${k.masked || "-"}</td>
      <td><input type="text" value="${(k.label || "").replace(/"/g, "&quot;")}"
           placeholder="自定义标签" data-key-id="${k.key_id}"
           style="width:110px;padding:4px 7px;border:1px solid var(--border);border-radius:6px;font-size:12.5px"></td>
      <td>${k.calls}</td>
      <td class="cost">${fmtCost(k.cost)}</td>
      <td>${pct}</td>`;
    body.appendChild(tr);
  }
  if (!keysCache.length) {
    body.innerHTML = `<tr><td colspan="6" class="hint">暂无数据</td></tr>`;
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

async function loadCharts() {
  const p = chartFilters();

  const daily = await api(`/api/daily?${p}`);
  const labels = daily.map((r) => r.d.slice(5));
  const datasets = Object.keys(CAT_NAMES).map((k) => ({
    label: CAT_NAMES[k],
    data: daily.map((r) => r[k]),
    backgroundColor: CAT_COLORS[k],
    stack: "cost",
    borderWidth: 0,
  }));
  datasets.push({
    label: "合计",
    type: "line",
    data: daily.map((r) => r.cost_total),
    borderColor: "#1f2430",
    backgroundColor: "#1f2430",
    borderWidth: 1.5,
    pointRadius: 2,
    tension: 0.3,
  });
  makeChart("#chart-daily", {
    type: "bar",
    data: { labels, datasets },
    options: {
      maintainAspectRatio: false,
      scales: {
        x: { stacked: true, grid: { display: false } },
        y: { stacked: true, ticks: { callback: (v) => `¥${v}` } },
      },
      plugins: { legend: { labels: { boxWidth: 10, font: { size: 10 } } } },
    },
  });

  const bd = await api(`/api/breakdown?${p}`);
  makeChart("#chart-breakdown", {
    type: "doughnut",
    data: {
      labels: bd.map((r) => r.category),
      datasets: [{
        data: bd.map((r) => r.cost),
        backgroundColor: bd.map((r) => CAT_COLORS[r.key] || "#999"),
        borderWidth: 2, borderColor: "#fff",
      }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 10, font: { size: 10 } } },
        tooltip: { callbacks: { label: (c) => `${c.label}: ${fmtCost(c.raw)}` } },
      },
    },
  });

  const models = await api(`/api/models?${p}`);
  makeChart("#chart-models", {
    type: "bar",
    data: {
      labels: models.map((r) => r.model),
      datasets: [{
        label: "费用",
        data: models.map((r) => r.cost),
        backgroundColor: "#4f6ef7",
        borderRadius: 6,
      }],
    },
    options: {
      maintainAspectRatio: false,
      scales: { y: { ticks: { callback: (v) => `¥${v}` } } },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (c) => `${fmtCost(c.raw)}(${models[c.dataIndex].calls} 次)` } },
      },
    },
  });
}

/* ---------- 调用日志 ---------- */

async function loadCalls() {
  const p = chartFilters();
  p.set("limit", PAGE_SIZE);
  p.set("offset", page * PAGE_SIZE);
  const { rows, total } = await api(`/api/calls?${p}`);

  const body = $("#calls-body");
  body.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    if (r.status_code >= 400) tr.className = "err";
    const tokens = `${fmtN(r.input_cache_hit)} / ${fmtN(r.input_cache_miss)} / ${fmtN(r.output_tokens)}`;
    const tools = `${fmtN(r.search_calls)} / ${fmtN(r.image_search_calls)} / ${fmtN(r.fetch_calls)}`;
    const keyInfo = keysCache.find((k) => k.key_id === r.key_id);
    const keyName = keyInfo ? keyInfo.name : (r.key_id || "-");
    const others = [];
    if (r.ocr_pages) others.push(`${fmtN(r.ocr_pages)} 页`);
    if (r.asr_seconds) others.push(`${Math.round(r.asr_seconds)}s 音频`);
    if (r.tts_chars) others.push(`${fmtN(r.tts_chars)} 字`);
    const detail = [
      `token ${fmtCost(r.cost_tokens)}`, `搜索 ${fmtCost(r.cost_search)}`,
      `抓取 ${fmtCost(r.cost_fetch)}`, `OCR ${fmtCost(r.cost_ocr)}`,
      `ASR ${fmtCost(r.cost_asr)}`, `TTS ${fmtCost(r.cost_tts)}`,
    ].join(" · ");
    const status = r.error
      ? `<span class="badge err" title="${String(r.error).replace(/"/g, "&quot;")}">${r.status_code}</span>`
      : `<span class="badge">${r.stream ? "流式" : "完成"}</span>`;
    const src = r.source === "demo" ? `<span class="badge">演示</span> ` : "";
    tr.innerHTML = `
      <td>${r.ts}</td>
      <td class="endpoint-cell" title="${r.protocol}">${r.endpoint}</td>
      <td>${r.model || "-"}</td>
      <td>${keyName}</td>
      <td>${tokens}</td>
      <td>${tools}</td>
      <td>${others.join(" · ") || "-"}</td>
      <td class="hint" style="white-space:normal" title="${detail}">${detail}</td>
      <td class="cost">${fmtCost(r.cost_total)}</td>
      <td>${src}${status}${r.latency_ms ? ` <span class="hint">${r.latency_ms}ms</span>` : ""}</td>`;
    body.appendChild(tr);
  }

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
  $("#webhook-last").textContent = s.webhook_last_result || "";
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

async function refreshAll() {
  await Promise.all([loadSummary(), loadCharts(), loadKeys(), loadCalls()]);
}

setInterval(() => {
  $("#clock").textContent = new Date().toLocaleString("zh-CN", { hour12: false });
}, 1000);

loadSettings();
loadPrices();
refreshAll();
loadBalance();
setInterval(() => { loadSummary(); loadBalance(); }, 60000);
