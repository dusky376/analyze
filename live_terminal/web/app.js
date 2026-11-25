/* 悟空真经 · 二板排板终端 —— 原生 JS，无构建步骤。
 * 全站红涨绿跌；所有数字来自 /api/* 真实接口，前端不伪造任何行情或结论。 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const has = (v) => v !== null && v !== undefined && v !== "" && !Number.isNaN(v);

const fmtPct = (v, digits = 2) => {
  if (!has(v)) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? `${n > 0 ? "+" : ""}${(n * 100).toFixed(digits)}%` : "—";
};
const fmtNum = (v, digits = 2) => {
  if (!has(v)) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : "—";
};
const fmtInt = (v) => (has(v) ? Number(v).toLocaleString("zh-CN") : "—");
const fmtDay = (v) => {
  const s = String(v ?? "");
  return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}` : (s || "—");
};
const fmtTs = (v) => {
  const s = String(v ?? "");
  if (!s) return "—";
  const m = s.match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : s;
};
const cls = (v) => (!has(v) ? "flat" : Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "flat");

const roleTag = (role, cn) => {
  const k = String(role || "").toLowerCase();
  const map = { rabbit: "rabbit", turtle: "turtle", heavy: "heavy" };
  return `<span class="tag ${map[k] || ""}">${esc(cn || role || "—")}</span>`;
};
const statusTag = (s) => {
  const map = {
    filled: ["filled", "已成交"],
    queued_unfilled: ["unfilled", "排队未成"],
    queued: ["unfilled", "排队中"],
    unknown: ["unknown", "不可判定"],
    closed: ["filled", "已结算"],
    holding: ["unfilled", "持有中"],
  };
  const [c, label] = map[s] || ["unknown", s || "—"];
  return `<span class="tag ${c}">${esc(label)}</span>`;
};

async function getJSON(url, timeout = 45000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const r = await fetch(url, { signal: ctrl.signal, headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

function setPill(id, text, tone) {
  const n = $(id);
  if (!n) return;
  n.textContent = text;
  if (tone) n.className = `pill dot ${tone}`;
}

function setNotice(id, msg) {
  const n = $(id);
  if (!n) return;
  if (!msg) { n.classList.add("hidden"); n.textContent = ""; return; }
  n.classList.remove("hidden");
  n.textContent = msg;
}

/* ---------------------------------------------------------- app state */
const state = {
  activeTab: "today",
  posFilter: "all",
  lastLiveData: null,
  lastGeneratedAt: null,
  marketTrading: false,
  inFlight: { health: false, live: false, evo: false, bt: false, exitAlt: false },
  loaded: { evo: false, bt: false, exitAlt: false },
  sse: null,
};

/* --------------------------------------------------------------- tabs */
function switchTab(tab) {
  state.activeTab = tab;
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${tab}`));
  if (tab === "evo") {
    if (!state.loaded.evo) loadEvolution();
    if (!state.loaded.bt) loadBacktest();
  }
}

/* ------------------------------------------------------------- health */
function renderHealth(h) {
  const ok = h && h.ok;
  const last = h?.panel?.last_trade_date || "—";
  const live = h?.live_archive?.latest || "—";
  const ac = h?.archive_check || {};
  const tc = ac.themes || {};
  const archiveBad = ac.status === "missing" || ac.status === "stale";
  const themeBad = tc.status === "missing" || tc.status === "stale";

  setPill("#healthPill",
    !ok ? "数据异常" : archiveBad ? "留档缺失" : themeBad ? "题材断更" : "数据正常",
    !ok ? "bad" : (archiveBad || themeBad) ? "warn" : "ok");
  $("#healthPill").title = [
    ac.message || "",
    tc.message ? `题材池：${tc.message}` : "",
    `panel=${last} · live=${live} · recorder=${h?.recorder?.running ? "running" : "stopped"}`,
  ].filter(Boolean).join("\n");

  const market = h?.market?.reason || "";
  state.marketTrading = Boolean(h?.market?.trading);
  setPill("#marketPill", market ? `A股 ${market}` : "A股 —");
  $("#dayPill").textContent = fmtDay(h?.day);

  $("#freshPanel").innerHTML = `日线面板：<b>${esc(last)}</b>`;
  $("#freshLive").innerHTML = `留档最新：<b>${esc(fmtDay(live))}</b> (${h?.recorder?.running ? "采集中" : "未采集"})`;

  const warns = [];
  if (!ok) warns.push("后端健康检查失败，请确认数据目录与服务状态。");
  if (archiveBad) warns.push(`留档自检：${ac.message}`);
  if (themeBad) warns.push(`题材池：${tc.message}`);
  setNotice("#healthNotice", warns.join(" ｜ "));
}

/* ----------------------------------------------------------- live sim */
function buyQualityCell(o) {
  const bq = o.buy_quality;
  if (!bq) {
    const tv = has(o.turn_at_seal) ? `${Number(o.turn_at_seal).toFixed(1)}%` : "—";
    return `<td class="num muted">换手 ${tv}</td>`;
  }
  const gMap = { A: ["grade-a", "A 优"], B: ["grade-b", "B 良"], C: ["grade-c", "C 弱"], REJECT: ["grade-reject", "回避"] };
  const [cls2, label] = gMap[bq.grade] || ["unknown", bq.grade];
  const tip = (bq.reasons || []).join("；") || "";
  const warnBadge = bq.cancel_warn ? `<div class="code down">⚠ 建议撤单</div>` : "";
  const tv = has(o.turn_at_seal) ? `换手 ${Number(o.turn_at_seal).toFixed(1)}%` : `分 ${bq.score}`;
  return `<td class="num" title="${esc(tip)}">
    <span class="tag ${cls2}">${label} (${bq.score})</span>
    <div class="code">${tv}</div>${warnBadge}
  </td>`;
}

function valueCell(o) {
  if (!has(o.hold_value)) return `<td class="num muted">—</td>`;
  const pnl = has(o.hold_pnl) ? Number(o.hold_pnl) : 0;
  const pct = has(o.hold_pnl_pct) ? Number(o.hold_pnl_pct) : 0;
  const tone = cls(pnl);
  const sign = pnl > 0 ? "+" : "";
  return `<td class="num" title="${esc(o.value_basis || "")} · 现价 ${fmtNum(o.live_price)}">
    ${fmtInt(o.hold_value)}
    <div class="code ${tone}">${sign}${fmtInt(pnl)} (${sign}${pct.toFixed(2)}%)</div>
  </td>`;
}

function renderLiveSim(d) {
  state.lastLiveData = d;
  state.lastGeneratedAt = d.generated_at || null;
  const day = d.day || "";
  $("#dayPill").textContent = fmtDay(day);

  const nToday = d.n_today ?? 0;
  const nFilled = d.n_filled_today ?? 0;
  const nHolding = d.n_holding ?? 0;
  const nPairs = d.n_pairs_today ?? 0;
  $("#kToday").textContent = fmtInt(nToday);
  $("#kFilled").textContent = fmtInt(nFilled);
  $("#kHolding").textContent = fmtInt(nHolding);
  $("#kPairs").textContent = fmtInt(nPairs);

  const f = d.freshness || {};
  $("#freshSync").innerHTML = `账本同步：<b>${esc(fmtTs(d.generated_at))}</b>${f.market_trading ? " (盘中实时)" : ""}`;
  if (f.price_source) $("#freshSource").textContent = `数据源：${f.price_source}`;

  const title = $("#heroTitle");
  const reason = $("#heroReason");
  if (d.no_orders) {
    title.textContent = "今日不开仓";
    const why = (Array.isArray(d.notes) && d.notes.length) ? d.notes.join(" ") : "";
    reason.textContent = why || d.no_orders_reason || "当日无二板委托。";
    setNotice("#strategyNotice", why || d.no_orders_reason || "");
  } else if (nToday > 0) {
    title.textContent = `今日 ${nToday} 张二板排板单`;
    reason.textContent = `已成交 ${nFilled} 张，排队未成 ${d.n_unfilled_today ?? 0} 张，不可判定 ${d.n_unknown_today ?? 0} 张（Q037 撮合）。`;
    setNotice("#strategyNotice", "");
  } else {
    title.textContent = "今日暂无委托";
    reason.textContent = "涨停池留档中暂未形成二板龟兔组合。";
    setNotice("#strategyNotice", "");
  }

  const coreText = typeof d.core_definition === "object" && d.core_definition
    ? d.core_definition.core : d.core_definition;
  $("#rules").innerHTML = [
    ["买点", d.entry_rule],
    ["卖点", d.exit_rule],
    ["核心", coreText],
  ].filter(([, v]) => v).map(([k, v]) => `<span>${esc(k)}：${esc(v)}</span>`).join("");

  $("#todayMeta").textContent =
    `${fmtDay(day)} · 涨停池帧 ${fmtInt(d.frames)} · 今日委托 ${fmtInt(nToday)} · 已成交 ${fmtInt(nFilled)}`;

  // 保留展开的图表上下文（定位到原表格，避免跳表）
  const saved = inlineChart.code ? {
    code: inlineChart.code, tab: inlineChart.tab, tableId: inlineChart.tableId,
  } : null;
  closeInlineChart();

  renderTodayOrders(d.today_orders || [], day);
  renderPairs(d.pairs, day);
  renderPositions(d);
  renderAccount(d);
  renderCost(d);
  renderEmotion(d.ice, d.emotion);

  if (saved) {
    const scope = saved.tableId ? $(`#${saved.tableId}`) : document;
    const prev = (scope || document).querySelector(`tr.chart-row[data-code="${CSS.escape(saved.code)}"]`);
    if (prev) {
      toggleInlineChart(prev, saved.code, prev.getAttribute("data-name"), saved.tab);
    }
  }
  $("#footerMeta").textContent =
    `账本生成 ${d.generated_at || "—"} · ${d.strategy || ""} · 快照源 ${d.source || "sync"}`;
}

function renderTodayOrders(rows, day) {
  const body = $("#todayBody");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="11" class="empty">${esc(fmtDay(day))} 无排板委托 —— 当日策略未开仓，不回退展示历史单。</td></tr>`;
    return;
  }
  // 修复旧版列错位：第 8 列严格为「预算」，第 9 列严格为「持有价值」
  body.innerHTML = rows.map((o) => {
    const g = o.guitu || {};
    const reason = o.fill_reason || o.state_note || (o.queue_sim && o.queue_sim.detail) || "—";
    return `<tr class="chart-row" data-code="${esc(o.ts_code)}" data-name="${esc(o.name)}" title="点击展开高清 K 线 / 1分钟分时">
      <td><div class="name">${esc(o.name || "—")}</div><div class="code">${esc(o.ts_code || "")}</div></td>
      <td>${esc(o.guitu_theme || o.industry || "—")}</td>
      <td>${roleTag(g.role, g.role_cn)}</td>
      ${buyQualityCell(o)}
      <td>${esc(fmtTs(o.ordered_at))}</td>
      <td class="num">${fmtNum(o.limit_price)}</td>
      <td class="num">${has(o.qty) ? `${fmtInt(o.qty)}股` : "—"}</td>
      <td class="num" title="${esc(o.budget_reason || "")}">${has(o.size_budget_yuan) ? fmtInt(o.size_budget_yuan) : "—"}</td>
      ${valueCell(o)}
      <td>${statusTag(o.status)}</td>
      <td class="muted">${esc(reason)}</td>
    </tr>`;
  }).join("");
}

function renderPairs(pairs, day) {
  const wrap = $("#pairsWrap");
  const list = (pairs && pairs.pairs) || [];
  if (!list.length) {
    wrap.innerHTML = `<div class="pair-card"><h4>龟兔组合</h4><div class="empty-card">${esc(fmtDay(day))} 暂无同题材同身位龟兔配对。</div></div>`;
    return;
  }
  wrap.innerHTML = list.map((p) => {
    const members = (p.members || []).map((m) => `
      <div class="member">
        ${roleTag(m.role, m.role_cn)}
        <span class="who"><b>${esc(m.name || "")}</b> <span class="code">${esc(m.ts_code || "")}</span></span>
        <span class="why">${esc(m.role_reason || "")}</span>
      </div>`).join("");
    return `<div class="pair-card">
      <h4>${esc(p.theme || "—")} · ${esc(p.streak || 2)} 板</h4>
      <div class="meta">兔子 ${fmtInt(p.n_rabbit)} · 乌龟 ${fmtInt(p.n_turtle)}</div>
      ${members}
    </div>`;
  }).join("");
}

function renderPositions(d) {
  const body = $("#posBody");
  const all = (d.positions || []).slice().reverse();
  const filtered = all.filter((p) => {
    if (state.posFilter === "holding") return p.status === "holding";
    if (state.posFilter === "closed") return p.status === "closed";
    return true;
  });
  $("#posCountMeta").textContent = `共 ${all.length} 笔（当前显示 ${filtered.length} 笔）· 点击行查看 K 线与买卖点标注`;
  if (!filtered.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty">当前筛选条件下暂无持仓或结算记录。</td></tr>`;
    return;
  }
  body.innerHTML = filtered.slice(0, 100).map((p) => {
    const g = p.gross_return;
    const n = has(p.net_return) ? p.net_return : p.r_net;
    return `<tr class="chart-row" data-code="${esc(p.ts_code)}" data-name="${esc(p.name)}" title="点击查看该股 K 线、分时与买卖点标记">
      <td><div class="name">${esc(p.name || "—")}</div><div class="code">${esc(p.ts_code || "")}</div></td>
      <td>${esc(fmtDay(p.entry_date))}</td>
      <td>${statusTag(p.status)}</td>
      <td class="num">${fmtNum(p.entry_price)}</td>
      <td class="num">${p.exit_date ? esc(fmtDay(p.exit_date)) : "—"}</td>
      <td class="num">${has(p.exit_price) ? fmtNum(p.exit_price) : "—"}</td>
      <td class="num ${cls(g)}">${fmtPct(g)}</td>
      <td class="num ${cls(n)}">${fmtPct(n)}</td>
    </tr>`;
  }).join("");
}

function renderAccount(d) {
  const el = $("#accountSummary");
  const capWrap = $("#capBarWrap");
  const a = d.account;
  if (!a) { el.innerHTML = ""; capWrap.classList.add("hidden"); return; }
  const pnl = a.total_pnl;
  const ret = a.total_return;
  const todayPnl = a.today_pnl;
  const chips = [
    ["总资金", fmtInt(a.capital_yuan)],
    ["总资产", fmtInt(a.equity_yuan)],
    ["可用现金", fmtInt(a.cash_yuan)],
    ["持仓市值", fmtInt(a.position_value_yuan)],
    ["今日盈亏", has(todayPnl) ? `${todayPnl > 0 ? "+" : ""}${fmtInt(todayPnl)}` : "—", cls(todayPnl)],
    ["累计盈亏", has(pnl) ? `${pnl > 0 ? "+" : ""}${fmtInt(pnl)} (${fmtPct(ret)})` : "—", cls(pnl)],
  ];
  el.innerHTML = chips.map(([k, v, c]) =>
    `<div class="summary-chip"><span>${esc(k)}</span><b class="${c || ""}">${esc(v)}</b></div>`
  ).join("");

  const cap = Number(a.capital_yuan || 0);
  const posVal = Number(a.position_value_yuan || 0);
  if (cap > 0) {
    const pct = Math.min(100, (posVal / cap) * 100);
    capWrap.classList.remove("hidden");
    $("#capBarFill").style.width = `${pct.toFixed(1)}%`;
    $("#capBarText").textContent = `${pct.toFixed(1)}% (${fmtInt(posVal)} / 上限 ${fmtInt(cap * 0.8)})`;
  } else {
    capWrap.classList.add("hidden");
  }
}

function renderCost(d) {
  const s = d.summary || {};
  const chips = [
    ["已结算", fmtInt(s.n_closed)],
    ["持有中", fmtInt(s.n_holding)],
    ["胜率(毛)", fmtPct(s.win_rate_gross, 1)],
    ["均值毛", fmtPct(s.avg_gross), cls(s.avg_gross)],
    ["均值净(万0.84免五)", fmtPct(s.avg_net), cls(s.avg_net)],
    ["单笔上限", `${fmtInt(d.position_size_yuan || 100000)} 元`],
  ];
  $("#costSummary").innerHTML = chips.map(([k, v, c]) =>
    `<div class="summary-chip"><span>${esc(k)}</span><b class="${c || ""}">${esc(v)}</b></div>`
  ).join("");

  const body = $("#costBody");
  if (!s.n_closed) {
    body.innerHTML = `<tr><td colspan="5" class="empty">尚无已结算交易，结算后自动汇总。</td></tr>`;
    return;
  }
  body.innerHTML = `<tr>
    <td>${esc(d.cost_label || "万0.84免五")}</td>
    <td class="num">${fmtInt(s.n_closed)}</td>
    <td class="num">${fmtPct(s.win_rate_net, 1)}</td>
    <td class="num ${cls(s.avg_net)}">${fmtPct(s.avg_net)}</td>
    <td class="num ${cls(s.median_net)}">${fmtPct(s.median_net)}</td>
  </tr>`;
}

function renderEmotion(ice, emo) {
  const iceEl = $("#iceBox");
  const emoEl = $("#emotionBox");

  if (!ice || !ice.available) {
    iceEl.innerHTML = `<h4>市场冰点 · 仓位档位</h4><div class="empty-card">${esc((ice && ice.reason) || "市场冰点数据暂不可用")}</div>`;
  } else {
    const isIce = Boolean(ice.is_ice);
    const lv = ice.severity_level || "—";
    const sf = has(ice.size_factor) ? Number(ice.size_factor) : 1;
    const tagCls = isIce ? (lv === "L5" ? "grade-reject" : "unfilled") : "grade-a";
    const tagTxt = isIce ? `${lv} 冰点 · 降仓 ×${sf.toFixed(2)}` : `${lv} 常态 · 满档 ×1.00`;
    const m = ice.metrics || {};
    const dl = ice.dim_levels || {};
    const rows = [
      ["下跌占比", has(m.down_share) ? `${(m.down_share * 100).toFixed(1)}%` : "—", dl.down_share || "—"],
      ["跌停家数", has(m.limit_down_n) ? `${fmtInt(m.limit_down_n)} 家` : "—", dl.limit_down_n || "—"],
      ["跌停/涨停", has(m.dt_ratio) ? fmtNum(m.dt_ratio, 2) : "—", dl.dt_ratio || "—"],
      ["昨日≥3板断板", has(m.broken_hi_n) ? `${fmtInt(m.broken_hi_n)} 家` : "—", dl.broken_hi_n || "—"],
    ];
    iceEl.innerHTML = `<h4>市场冰点 · 仓位档位</h4>
      <div class="status"><span class="tag ${tagCls}">${esc(tagTxt)}</span><span>${esc(ice.verdict || "")}</span></div>
      <table><thead><tr><th>维度</th><th class="num">实测</th><th class="num">分位档</th></tr></thead>
      <tbody>${rows.map(([k, v, l]) => `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td><td class="num">${esc(l)}</td></tr>`).join("")}</tbody></table>
      <p class="muted">基准日 ${esc(ice.day || "—")} · 数据源 ${esc(ice.source || "—")}</p>`;
  }

  if (!emo || !emo.available) {
    emoEl.innerHTML = `<h4>个股情绪 · 打板闸门</h4><div class="empty-card">${esc((emo && emo.reason) || "个股情绪数据暂不可用")}</div>`;
  } else {
    const blocked = Boolean(emo.blocked);
    const tagCls = blocked ? "grade-reject" : "grade-a";
    const tagTxt = blocked ? "闸门拦截 · 今日不开仓" : "闸门通过 · 可做二板";
    const rows = [
      ["昨日≥2板高标数", has(emo.n_high) ? `${fmtInt(emo.n_high)} 只` : "—", ""],
      ["高标今日均涨", has(emo.avg_pct) ? `${emo.avg_pct > 0 ? "+" : ""}${Number(emo.avg_pct).toFixed(2)}%` : "—", cls(emo.avg_pct)],
      ["高标跌停占比", has(emo.limit_down_share) ? `${(emo.limit_down_share * 100).toFixed(1)}%` : "—", ""],
    ];
    emoEl.innerHTML = `<h4>个股情绪 · 打板闸门</h4>
      <div class="status"><span class="tag ${tagCls}">${esc(tagTxt)}</span><span>${esc(emo.reason || "")}</span></div>
      <table><thead><tr><th>指标</th><th class="num">数值</th></tr></thead>
      <tbody>${rows.map(([k, v, c]) => `<tr><td>${esc(k)}</td><td class="num ${c}">${esc(v)}</td></tr>`).join("")}</tbody></table>`;
  }
}

/* ------------------------------------------- HiDPI Interactive Chart */
const inlineChart = { code: null, name: null, tab: "kline", data: null, timer: null, rowEl: null, detailEl: null, tableId: null };
const UP_COLOR = "#f25c5c";
const DOWN_COLOR = "#2ec27e";
const VWAP_COLOR = "#f59e0b";

function closeInlineChart() {
  if (inlineChart.timer) { clearInterval(inlineChart.timer); inlineChart.timer = null; }
  if (inlineChart.detailEl) { inlineChart.detailEl.remove(); inlineChart.detailEl = null; }
  if (inlineChart.rowEl) { inlineChart.rowEl.classList.remove("selected"); inlineChart.rowEl = null; }
  inlineChart.code = null; inlineChart.data = null; inlineChart.tableId = null;
}

function toggleInlineChart(tr, code, name, initialTab = "kline") {
  if (inlineChart.code === code && inlineChart.rowEl === tr) { closeInlineChart(); return; }
  closeInlineChart();
  inlineChart.code = code;
  inlineChart.name = name || code;
  inlineChart.tab = initialTab;
  inlineChart.rowEl = tr;
  inlineChart.tableId = tr.closest("tbody")?.id || null;
  tr.classList.add("selected");

  const colspan = Math.max(1, tr.children.length);
  const detail = document.createElement("tr");
  detail.className = "chart-detail-row";
  detail.innerHTML = `<td colspan="${colspan}">
    <div class="chart-inline">
      <div class="chart-inline-head">
        <b>${esc(name || code)} (${esc(code)})</b>
        <div class="chart-tabs">
          <button type="button" class="chart-tab ${initialTab === "kline" ? "active" : ""}" data-tab="kline">日K线 + 买卖点</button>
          <button type="button" class="chart-tab ${initialTab === "intraday" ? "active" : ""}" data-tab="intraday">1分钟分时 + VWAP</button>
        </div>
        <button type="button" class="chart-close">收起 ✕</button>
      </div>
      <div class="chart-inline-meta muted">单股实时快查中…</div>
      <div class="chart-stage">
        <canvas class="chart-canvas"></canvas>
        <div class="chart-tip hidden"></div>
      </div>
    </div>
  </td>`;
  tr.after(detail);
  inlineChart.detailEl = detail;

  detail.querySelector(".chart-close").addEventListener("click", (e) => { e.stopPropagation(); closeInlineChart(); });
  detail.querySelectorAll(".chart-tab").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      inlineChart.tab = btn.dataset.tab;
      detail.querySelectorAll(".chart-tab").forEach((b) => b.classList.toggle("active", b === btn));
      drawInlineChart();
    });
  });

  fetchAndDrawChart();
  inlineChart.timer = setInterval(() => {
    if (!document.hidden) fetchAndDrawChart();
  }, state.marketTrading ? 12000 : 60000);
}

async function fetchAndDrawChart() {
  if (!inlineChart.code || !inlineChart.detailEl) return;
  const code = inlineChart.code;
  const meta = inlineChart.detailEl.querySelector(".chart-inline-meta");
  try {
    const d = await getJSON(`/api/chart?code=${encodeURIComponent(code)}&kdays=45`, 20000);
    if (inlineChart.code !== code) return;
    inlineChart.data = d;
    drawInlineChart();
  } catch (e) {
    if (meta) meta.textContent = `图表加载失败：${e.message || e}`;
  }
}

function setupHiDPI(canvas, cssH = 300) {
  const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  const cssW = Math.max(480, Math.floor(canvas.parentElement.clientWidth || 900));
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  canvas.style.height = `${cssH}px`;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, W: cssW, H: cssH };
}

function drawInlineChart(hoverX = null) {
  if (!inlineChart.detailEl || !inlineChart.data) return;
  const d = inlineChart.data;
  const canvas = inlineChart.detailEl.querySelector(".chart-canvas");
  const meta = inlineChart.detailEl.querySelector(".chart-inline-meta");
  const tip = inlineChart.detailEl.querySelector(".chart-tip");
  const { ctx, W, H } = setupHiDPI(canvas, 300);

  // 绑定一次悬停十字光标事件
  if (!canvas.dataset.bound) {
    canvas.dataset.bound = "1";
    canvas.addEventListener("mousemove", (ev) => {
      const rect = canvas.getBoundingClientRect();
      drawInlineChart(ev.clientX - rect.left);
    });
    canvas.addEventListener("mouseleave", () => {
      tip.classList.add("hidden");
      drawInlineChart(null);
    });
  }

  ctx.clearRect(0, 0, W, H);
  if (inlineChart.tab === "kline") {
    drawKline(ctx, W, H, d.kline, d.markers || [], meta, tip, hoverX);
  } else {
    drawIntraday(ctx, W, H, d.intraday, meta, tip, hoverX);
  }
}

function drawKline(ctx, W, H, k, markers, meta, tip, hoverX) {
  if (!k || !k.available || !k.bars || !k.bars.length) {
    meta.textContent = (k && k.reason) || "暂无 K 线数据";
    return;
  }
  const bars = k.bars;
  const last = bars[bars.length - 1];
  const badge = k.live ? "🟢 盘中实时" : (k.today_included ? "🟡 当日快照" : "⚪ 已收盘");
  meta.textContent = `${badge} · ${last.date} 现价 ${fmtNum(last.close)} (${has(last.pct) ? `${last.pct > 0 ? "+" : ""}${Number(last.pct).toFixed(2)}%` : "—"}) · ${k.note || ""}`;

  const padL = 56, padR = 16, padT = 16, volH = 52, gap = 10, padB = 22;
  const priceH = H - padT - volH - gap - padB;
  const plotW = W - padL - padR;

  let lo = Infinity, hi = -Infinity, maxVol = 0;
  for (const b of bars) {
    if (b.low < lo) lo = b.low;
    if (b.high > hi) hi = b.high;
    if (b.volume && b.volume > maxVol) maxVol = b.volume;
  }
  if (!Number.isFinite(lo) || hi <= lo) { lo = 0; hi = 1; }
  const margin = (hi - lo) * 0.08;
  lo -= margin; hi += margin;
  const yOf = (v) => padT + priceH * (1 - (v - lo) / (hi - lo));

  // 网格
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.fillStyle = "#8b98ab";
  ctx.font = "11px monospace";
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4;
    const y = yOf(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillText(v.toFixed(2), 6, y + 4);
  }

  const step = plotW / bars.length;
  const bw = Math.max(3, Math.min(13, step * 0.66));
  const volTop = padT + priceH + gap;

  const markerByDate = new Map();
  for (const m of markers) {
    const arr = markerByDate.get(m.date) || [];
    arr.push(m);
    markerByDate.set(m.date, arr);
  }

  bars.forEach((b, i) => {
    const cx = padL + i * step + step / 2;
    const isUp = b.close >= b.open;
    const col = isUp ? UP_COLOR : DOWN_COLOR;
    ctx.strokeStyle = col; ctx.fillStyle = col;
    ctx.beginPath(); ctx.moveTo(cx, yOf(b.high)); ctx.lineTo(cx, yOf(b.low)); ctx.stroke();
    const top = yOf(Math.max(b.open, b.close));
    const bot = yOf(Math.min(b.open, b.close));
    ctx.fillRect(cx - bw / 2, top, bw, Math.max(1.5, bot - top));

    if (maxVol > 0 && b.volume) {
      const vh = (b.volume / maxVol) * volH;
      ctx.globalAlpha = 0.55;
      ctx.fillRect(cx - bw / 2, volTop + volH - vh, bw, vh);
      ctx.globalAlpha = 1;
    }

    // 买卖点标记
    const ms = markerByDate.get(b.date);
    if (ms) {
      ms.forEach((m) => {
        const my = m.kind === "buy" ? Math.min(padT + priceH - 6, yOf(b.low) + 12) : Math.max(padT + 10, yOf(b.high) - 10);
        ctx.fillStyle = m.kind === "buy" ? "#f59e0b" : "#38bdf8";
        ctx.font = "bold 11px sans-serif";
        ctx.fillText(m.kind === "buy" ? "▲买" : "▼卖", cx - 10, my);
      });
    }
  });

  // 日期轴
  ctx.fillStyle = "#8b98ab"; ctx.font = "10.5px monospace";
  [0, Math.floor(bars.length / 2), bars.length - 1].forEach((idx) => {
    if (!bars[idx]) return;
    const cx = padL + idx * step;
    ctx.fillText(bars[idx].date.slice(5), Math.min(W - 46, Math.max(padL, cx)), H - 6);
  });

  // 十字光标 + 浮层
  if (hoverX !== null && hoverX >= padL && hoverX <= W - padR) {
    const idx = Math.max(0, Math.min(bars.length - 1, Math.floor((hoverX - padL) / step)));
    const b = bars[idx];
    const cx = padL + idx * step + step / 2;
    ctx.strokeStyle = "rgba(243,207,136,0.45)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(cx, padT); ctx.lineTo(cx, H - padB); ctx.stroke();
    ctx.setLineDash([]);
    const pStr = has(b.pct) ? `${b.pct > 0 ? "+" : ""}${Number(b.pct).toFixed(2)}%` : "—";
    tip.innerHTML = `<b>${esc(b.date)}</b> · 开 ${fmtNum(b.open)} 高 ${fmtNum(b.high)} 低 ${fmtNum(b.low)} 收 <b class="${b.close >= b.open ? "up" : "down"}">${fmtNum(b.close)}</b> (${pStr})`;
    tip.classList.remove("hidden");
  }
}

function drawIntraday(ctx, W, H, intra, meta, tip, hoverX) {
  if (!intra || !intra.available || !intra.points || !intra.points.length) {
    meta.textContent = (intra && intra.reason) || "暂无分时数据";
    return;
  }
  const pts = intra.points;
  const pc = intra.prev_close;
  const last = pts[pts.length - 1];
  const pct = pc ? (last.price - pc) / pc * 100 : null;
  const badge = intra.live ? "🟢 盘中实时 1分钟" : `⚪ ${intra.granularity || "快照"}`;
  meta.textContent = `${badge} · ${intra.day} ${last.t} 现价 ${fmtNum(last.price)}` +
    (pct !== null ? ` (${pct > 0 ? "+" : ""}${pct.toFixed(2)}%)` : "") +
    (intra.has_vwap ? " · 黄线=均价线VWAP" : "") + ` · ${intra.note || ""}`;

  const padL = 56, padR = 54, padT = 16, volH = 44, gap = 10, padB = 22;
  const priceH = H - padT - volH - gap - padB;
  const plotW = W - padL - padR;

  let lo = Infinity, hi = -Infinity, maxVol = 0;
  for (const p of pts) {
    if (p.price < lo) lo = p.price;
    if (p.price > hi) hi = p.price;
    if (p.avg_price) { lo = Math.min(lo, p.avg_price); hi = Math.max(hi, p.avg_price); }
    if (p.volume && p.volume > maxVol) maxVol = p.volume;
  }
  if (pc) {
    const dev = Math.max(Math.abs(hi - pc), Math.abs(lo - pc), pc * 0.015);
    lo = pc - dev * 1.1; hi = pc + dev * 1.1;
  } else if (hi <= lo) { lo -= 0.1; hi += 0.1; }

  const yOf = (v) => padT + priceH * (1 - (v - lo) / (hi - lo));
  const xOf = (i) => padL + (pts.length > 1 ? (plotW * i / (pts.length - 1)) : plotW / 2);

  // 网格 & 涨跌幅标尺
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.fillStyle = "#8b98ab"; ctx.font = "11px monospace";
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4;
    const y = yOf(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillText(v.toFixed(2), 6, y + 4);
    if (pc) {
      const rp = (v - pc) / pc * 100;
      ctx.fillStyle = rp > 0 ? UP_COLOR : rp < 0 ? DOWN_COLOR : "#8b98ab";
      ctx.fillText(`${rp > 0 ? "+" : ""}${rp.toFixed(1)}%`, W - padR + 5, y + 4);
      ctx.fillStyle = "#8b98ab";
    }
  }

  // 昨收虚线
  if (pc) {
    ctx.strokeStyle = "rgba(255,255,255,0.22)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(padL, yOf(pc)); ctx.lineTo(W - padR, yOf(pc)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // 成交量柱
  const volTop = padT + priceH + gap;
  if (maxVol > 0) {
    const bw = Math.max(1.5, Math.min(5, plotW / pts.length * 0.7));
    pts.forEach((p, i) => {
      if (!p.volume) return;
      const prevPx = i > 0 ? pts[i - 1].price : (pc || p.price);
      ctx.fillStyle = p.price >= prevPx ? UP_COLOR : DOWN_COLOR;
      ctx.globalAlpha = 0.5;
      const vh = (p.volume / maxVol) * volH;
      ctx.fillRect(xOf(i) - bw / 2, volTop + volH - vh, bw, vh);
      ctx.globalAlpha = 1;
    });
  }

  // 均价线 VWAP（黄色）
  if (intra.has_vwap) {
    ctx.strokeStyle = VWAP_COLOR; ctx.lineWidth = 1.4;
    ctx.beginPath();
    let started = false;
    pts.forEach((p, i) => {
      if (!p.avg_price) return;
      const x = xOf(i), y = yOf(p.avg_price);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  // 分时主线（红涨绿跌）
  const lineCol = pc ? (last.price >= pc ? UP_COLOR : DOWN_COLOR) : "#60a5fa";
  ctx.strokeStyle = lineCol; ctx.lineWidth = 1.8;
  ctx.beginPath();
  pts.forEach((p, i) => {
    const x = xOf(i), y = yOf(p.price);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // 时间轴
  ctx.fillStyle = "#8b98ab"; ctx.font = "10.5px monospace";
  [0, Math.floor(pts.length / 2), pts.length - 1].forEach((idx) => {
    if (!pts[idx]) return;
    ctx.fillText(pts[idx].t.slice(0, 5), Math.min(W - padR - 30, Math.max(padL, xOf(idx) - 12)), H - 6);
  });

  // 十字光标
  if (hoverX !== null && hoverX >= padL && hoverX <= W - padR) {
    const ratio = (hoverX - padL) / plotW;
    const idx = Math.max(0, Math.min(pts.length - 1, Math.round(ratio * (pts.length - 1))));
    const p = pts[idx];
    const cx = xOf(idx);
    ctx.strokeStyle = "rgba(243,207,136,0.45)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(cx, padT); ctx.lineTo(cx, H - padB); ctx.stroke();
    ctx.setLineDash([]);
    const rp = pc ? (p.price - pc) / pc * 100 : null;
    tip.innerHTML = `<b>${esc(p.t)}</b> · 价 <b class="${rp !== null ? cls(rp) : ""}">${fmtNum(p.price)}</b>` +
      (rp !== null ? ` (${rp > 0 ? "+" : ""}${rp.toFixed(2)}%)` : "") +
      (p.avg_price ? ` · 均价 ${fmtNum(p.avg_price)}` : "") +
      (p.volume ? ` · 量 ${fmtInt(Math.round(p.volume))}手` : "");
    tip.classList.remove("hidden");
  }
}

/* ------------------------------------------- Evolution & Backtest (lazy) */
function renderEvolution(d) {
  if (!d || !d.available) {
    $("#evoPathsBody").innerHTML = `<tr><td colspan="7" class="empty">进化引擎数据暂不可用</td></tr>`;
    return;
  }
  const sc = d.shadow_compare || {};
  const chips = [
    ["已追踪路径", fmtInt(d.n_paths)],
    ["因果结算样本", fmtInt(sc.n_trades)],
    ["静态基准均值", fmtPct(sc.baseline_avg_ret), cls(sc.baseline_avg_ret)],
    ["信息素影子均值", fmtPct(sc.shadow_avg_ret), cls(sc.shadow_avg_ret)],
    ["影子对照差异", has(sc.delta_pp) ? `${sc.delta_pp > 0 ? "+" : ""}${sc.delta_pp.toFixed(2)}pp` : "—", cls(sc.delta_pp)],
    ["每日挥发率 ρ", `${((d.params?.rho_evaporation || 0.1) * 100).toFixed(0)}%`],
  ];
  $("#evoSummary").innerHTML = chips.map(([k, v, c]) =>
    `<div class="summary-chip"><span>${esc(k)}</span><b class="${c || ""}">${esc(v)}</b></div>`
  ).join("");

  $("#evoAdvice").innerHTML = (d.advice || []).map((a) => `<div class="advice-item">${esc(a)}</div>`).join("");

  const paths = d.paths || [];
  if (!paths.length) {
    $("#evoPathsBody").innerHTML = `<tr><td colspan="7" class="empty">尚无已结算路径样本，产生真实结算后自动演化。</td></tr>`;
  } else {
    $("#evoPathsBody").innerHTML = paths.map((p) => {
      const pct = Math.max(0, Math.min(100, ((p.pheromone - 0.25) / (1.50 - 0.25)) * 100));
      const barCol = p.pheromone >= 1.05 ? UP_COLOR : p.pheromone <= 0.90 ? DOWN_COLOR : "#d9a84e";
      const tCls = p.trend === "强化" ? "grade-a" : p.trend === "衰减" ? "grade-reject" : "grade-b";
      return `<tr>
        <td><b>${esc(p.path)}</b></td>
        <td class="num"><span class="tau-bar"><span class="tau-track"><span class="tau-fill" style="width:${pct.toFixed(0)}%;background:${barCol}"></span></span><b>${fmtNum(p.pheromone, 2)}</b></span></td>
        <td class="num">×${fmtNum(p.size_mult, 2)}</td>
        <td class="num">${fmtInt(p.samples)}</td>
        <td class="num">${fmtPct(p.win_rate, 1)}</td>
        <td class="num ${p.nuclear_n > 0 ? "down" : "muted"}">${fmtInt(p.nuclear_n)}</td>
        <td><span class="tag ${tCls}">${esc(p.trend)}</span></td>
      </tr>`;
    }).join("");
  }

  const logs = d.feedback_log || [];
  if (!logs.length) {
    $("#evoLogBody").innerHTML = `<tr><td colspan="7" class="empty">暂无奖惩反馈日志</td></tr>`;
  } else {
    $("#evoLogBody").innerHTML = logs.map((r) => `<tr>
      <td>${esc(fmtDay(r.day))}</td>
      <td><b>${esc(r.name || "")}</b> <span class="code">${esc(r.ts_code)}</span></td>
      <td class="code">${esc(r.path)}</td>
      <td class="num ${cls(r.ret)}">${fmtPct(r.ret)}</td>
      <td class="num ${cls(r.reward)}">${fmtPct(r.reward)}</td>
      <td class="num">${fmtNum(r.tau_before, 2)} → <b>${fmtNum(r.tau_after, 2)}</b></td>
      <td class="muted">${esc(r.label)}</td>
    </tr>`).join("");
  }
}

function renderBacktest(g) {
  const sim = g && g.sim;
  if (!sim || !sim.available) {
    $("#btMeta").textContent = "历史回放快照暂不可用";
    $("#btStats").innerHTML = `<tr><td colspan="6" class="empty">无历史回放数据</td></tr>`;
    return;
  }
  const s = sim.summary || {};
  $("#btMeta").textContent = `区间 ${sim.period || "—"} · ${sim.cost_label || "万0.84免五"} · 生成 ${sim.generated_at || "—"}`;
  const rows = [
    ["主口径（含交易成本）", s.n, s.win_rate, s.avg_net, s.median_net, s.compound_10pct_net],
    ["毛收益对照（不含成本）", s.n, s.win_rate_gross, s.avg_gross, s.median_gross, s.compound_10pct_gross],
  ];
  $("#btStats").innerHTML = rows.map(([name, n, wr, avg, med, comp]) => `<tr>
    <td>${esc(name)}</td><td class="num">${fmtInt(n)}</td><td class="num">${fmtPct(wr, 1)}</td>
    <td class="num ${cls(avg)}">${fmtPct(avg)}</td><td class="num ${cls(med)}">${fmtPct(med)}</td>
    <td class="num ${cls(comp)}">${fmtPct(comp)}</td>
  </tr>`).join("");

  const latest = (sim.latest || []).slice(0, 20);
  $("#btLatest").innerHTML = latest.length ? latest.map((x) => `<tr>
    <td>${esc(x.entry_date || "—")}</td>
    <td><div class="name">${esc(x.name || "—")}</div><div class="code">${esc(x.ts_code || "")}</div></td>
    <td>${esc(x.theme || x.industry || "—")}</td>
    <td class="num">${fmtInt(x.entry_streak)}</td>
    <td class="num">${fmtInt(x.hold_days)}天</td>
    <td class="num">${fmtInt(x.max_streak)}</td>
    <td class="num ${cls(x.net_return)}">${fmtPct(x.net_return)}</td>
  </tr>`).join("") : `<tr><td colspan="7" class="empty">无近期信号</td></tr>`;

  $("#btCaveats").innerHTML = (sim.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("");
}

function renderExitAlternatives(d) {
  if (!d || !d.available) {
    $("#exitAltMeta").textContent = (d && d.reason) || "卖法对照暂不可用";
    $("#exitAltBody").innerHTML = `<tr><td colspan="6" class="empty">暂无对照样本</td></tr>`;
    return;
  }
  const s = d.summary || {};
  $("#exitAltMeta").textContent =
    `n=${fmtInt(s.n)} · 现行均值 ${fmtPct(s.avg_actual)} vs 固定止损均值 ${fmtPct(s.avg_fixed)} · 改善 ${has(s.delta_pp) ? `${s.delta_pp > 0 ? "+" : ""}${Number(s.delta_pp).toFixed(2)}pp` : "—"}`;
  const rows = d.trades || [];
  $("#exitAltBody").innerHTML = rows.length ? rows.map((t) => {
    const diff = has(t.diff_pp) ? `${t.diff_pp > 0 ? "+" : ""}${Number(t.diff_pp).toFixed(2)}pp` : "—";
    return `<tr>
      <td><div class="name">${esc(t.name || "—")}</div><div class="code">${esc(t.ts_code || "")}</div></td>
      <td class="num ${cls(t.actual_gross)}">${fmtPct(t.actual_gross)}</td>
      <td class="num ${cls(t.fixed_gross)}">${fmtPct(t.fixed_gross)}</td>
      <td class="num ${cls(t.diff_pp)}">${diff}</td>
      <td class="code">${esc(t.env_summary || "—")}</td>
      <td class="muted">${esc(t.fixed_reason || "—")}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">尚无已结算交易</td></tr>`;

  const groups = d.by_env || {};
  $("#exitEnvGroups").innerHTML = Object.entries(groups).map(([title, items]) => `
    <div class="env-group">
      <h4>${esc(title)}</h4>
      <table><thead><tr><th>分组</th><th class="num">n</th><th class="num">现行</th><th class="num">止损</th><th class="num">差</th></tr></thead>
      <tbody>${(items || []).map((x) => `<tr>
        <td>${esc(x.bucket)}</td><td class="num">${fmtInt(x.n)}</td>
        <td class="num ${cls(x.avg_actual)}">${fmtPct(x.avg_actual)}</td>
        <td class="num ${cls(x.avg_fixed)}">${fmtPct(x.avg_fixed)}</td>
        <td class="num ${cls(x.delta_pp)}">${has(x.delta_pp) ? `${x.delta_pp > 0 ? "+" : ""}${Number(x.delta_pp).toFixed(1)}pp` : "—"}</td>
      </tr>`).join("")}</tbody></table>
    </div>`).join("");
  $("#exitAltCaveats").innerHTML = (d.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("");
}

/* ------------------------------------------- decoupled loaders & SSE */
async function loadHealth() {
  if (state.inFlight.health) return;
  state.inFlight.health = true;
  try {
    const h = await getJSON("/api/health", 20000);
    renderHealth(h);
  } catch (e) {
    setPill("#healthPill", "健康检查失败", "bad");
    setNotice("#healthNotice", `无法读取 /api/health：${e.message || e}`);
  } finally {
    state.inFlight.health = false;
  }
}

async function loadLiveSim() {
  if (state.inFlight.live) return;
  state.inFlight.live = true;
  try {
    const d = await getJSON("/api/live-sim", 45000);
    renderLiveSim(d);
  } catch (e) {
    $("#heroTitle").textContent = "实时账本加载失败";
    $("#heroReason").textContent = String(e.message || e);
  } finally {
    state.inFlight.live = false;
  }
}

async function loadEvolution() {
  if (state.inFlight.evo) return;
  state.inFlight.evo = true;
  try {
    const d = await getJSON("/api/evolution", 30000);
    renderEvolution(d);
    state.loaded.evo = true;
  } catch (e) {
    $("#evoPathsBody").innerHTML = `<tr><td colspan="7" class="empty">加载失败：${esc(e.message || e)}</td></tr>`;
  } finally {
    state.inFlight.evo = false;
  }
}

async function loadBacktest() {
  if (state.inFlight.bt) return;
  state.inFlight.bt = true;
  try {
    const g = await getJSON("/api/guitu", 45000);
    renderBacktest(g);
    state.loaded.bt = true;
  } catch (e) {
    $("#btMeta").textContent = `历史回放加载失败：${e.message || e}`;
  } finally {
    state.inFlight.bt = false;
  }
}

async function loadExitAlternatives() {
  if (state.inFlight.exitAlt) return;
  state.inFlight.exitAlt = true;
  $("#exitAltMeta").textContent = "正在计算卖法对照（首次需扫面板，请稍候）…";
  try {
    const d = await getJSON("/api/exit-alternatives", 60000);
    renderExitAlternatives(d);
    state.loaded.exitAlt = true;
  } catch (e) {
    $("#exitAltMeta").textContent = `卖法对照加载失败：${e.message || e}`;
  } finally {
    state.inFlight.exitAlt = false;
  }
}

function connectSSE() {
  if (typeof EventSource === "undefined") {
    setPill("#ssePill", "定时轮询", "warn");
    return;
  }
  try {
    const es = new EventSource("/api/stream");
    state.sse = es;
    es.onopen = () => setPill("#ssePill", "实时推送", "ok");
    es.onmessage = (ev) => {
      if (document.hidden) return;
      try {
        const brief = JSON.parse(ev.data);
        if (brief.error) return;
        state.marketTrading = Boolean(brief.trading);
        setPill("#ssePill", brief.trading ? `盘中实时 ${fmtTs(brief.server_time)}` : `在线 ${fmtTs(brief.server_time)}`, "ok");
        if (brief.generated_at && brief.generated_at !== state.lastGeneratedAt) {
          loadLiveSim();
        }
      } catch (_) {}
    };
    es.onerror = () => setPill("#ssePill", "重连中/轮询", "warn");
  } catch (_) {
    setPill("#ssePill", "定时轮询", "warn");
  }
}

/* -------------------------------------------------------------- init */
function bindEvents() {
  $$(".tab-btn").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  $$("#posFilters .filter-btn").forEach((b) => {
    b.addEventListener("click", () => {
      state.posFilter = b.dataset.filter;
      $$("#posFilters .filter-btn").forEach((x) => x.classList.toggle("active", x === b));
      if (state.lastLiveData) renderPositions(state.lastLiveData);
    });
  });

  const onRowClick = (e) => {
    const tr = e.target.closest("tr.chart-row");
    if (!tr) return;
    toggleInlineChart(tr, tr.dataset.code, tr.dataset.name);
  };
  $("#todayBody").addEventListener("click", onRowClick);
  $("#posBody").addEventListener("click", onRowClick);

  $("#exitAltDetails").addEventListener("toggle", (e) => {
    if (e.target.open && !state.loaded.exitAlt) loadExitAlternatives();
  });

  $("#refreshBtn").addEventListener("click", async () => {
    const btn = $("#refreshBtn");
    btn.disabled = true; btn.textContent = "刷新中…";
    try {
      await fetch("/api/refresh", { method: "POST" });
      state.loaded = { evo: false, bt: false, exitAlt: false };
      await Promise.all([loadHealth(), loadLiveSim()]);
      if (state.activeTab === "evo") { loadEvolution(); loadBacktest(); }
      if ($("#exitAltDetails").open) loadExitAlternatives();
    } finally {
      btn.disabled = false; btn.textContent = "刷新";
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { loadHealth(); loadLiveSim(); }
  });
}

bindEvents();
// 首屏只并发两个轻快核心接口，互不阻塞，毫秒级呈现
loadHealth();
loadLiveSim();
connectSSE();
// 兜底轮询（仅前台激活时执行；SSE 正常时作为冗余保护）
setInterval(() => {
  if (document.hidden) return;
  loadHealth();
  loadLiveSim();
}, 45000);
