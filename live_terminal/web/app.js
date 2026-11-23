/* 悟空真经 · 二板排板终端 —— 原生 JS，无构建步骤。
 * 所有数字来自 /api/*，前端不参与任何策略判定。 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const has = (v) => v !== null && v !== undefined && v !== "" && !Number.isNaN(v);

const fmtPct = (v, digits = 2) => {
  if (!has(v)) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${n > 0 ? "+" : ""}${(n * 100).toFixed(digits)}%`;
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

async function getJSON(url, timeout = 60000) {
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

function setText(id, text, tone) {
  const n = $(id);
  if (!n) return;
  n.textContent = text;
  if (tone) n.className = `pill dot ${tone}`;
}

function notice(msg) {
  const n = $("#notice");
  if (!msg) { n.classList.add("hidden"); n.textContent = ""; return; }
  n.classList.remove("hidden");
  n.textContent = msg;
}

/* ---------------------------------------------------------------- health */
function renderHealth(h) {
  const ok = h && h.ok;
  const last = h?.panel?.last_trade_date || "—";
  const live = h?.live_archive?.latest || "—";
  setText("#healthPill", ok ? "数据正常" : "数据异常", ok ? "ok" : "bad");
  $("#healthPill").title = `panel_last=${last} · live_latest=${live} · recorder=${
    h?.recorder?.running ? "running" : "stopped"}`;
  const market = h?.market?.reason || "";
  setText("#marketPill", market ? `A股 ${market}` : "A股 —");
  $("#dayPill").textContent = fmtDay(h?.day);
}

/* --------------------------------------------------------------- live sim */
function renderLiveSim(d) {
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

  const title = $("#heroTitle");
  const reason = $("#heroReason");
  if (d.no_orders) {
    title.textContent = "今日不开仓";
    reason.textContent = d.no_orders_reason || "当日无二板委托。";
    notice(d.no_orders_reason || "");
  } else if (nToday > 0) {
    title.textContent = `今日 ${nToday} 张二板排板单`;
    reason.textContent = `已成交 ${nFilled} 张，排队未成 ${d.n_unfilled_today ?? 0} 张，` +
      `不可判定 ${d.n_unknown_today ?? 0} 张。成交由 Q037 规则判定，不默认排到就成交。`;
    notice("");
  } else {
    title.textContent = "今日暂无委托";
    reason.textContent = "涨停池留档中暂未形成二板龟兔组合。";
    notice("");
  }

  const rules = $("#rules");
  rules.innerHTML = [
    ["买点", d.entry_rule],
    ["卖点", d.exit_rule],
    ["核心", d.core_definition],
  ].filter(([, v]) => v).map(([k, v]) => `<span>${esc(k)}：${esc(v)}</span>`).join("");

  $("#todayMeta").textContent =
    `${fmtDay(day)} · 涨停池帧 ${fmtInt(d.frames)} · 今日委托 ${fmtInt(nToday)} · 已成交 ${fmtInt(nFilled)}`;
  renderTodayOrders(d.today_orders || [], day);
  renderPairs(d.pairs, day);
  renderPositions(d);
  renderCost(d);
  $("#footerMeta").textContent =
    `账本生成 ${d.generated_at || "—"} · ${d.strategy || ""} · 快照源 ${d.source || "sync"}`;
}

function renderTodayOrders(rows, day) {
  const body = $("#todayBody");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty">${esc(fmtDay(day))} 无排板委托 —— 当日策略未开仓，不回退展示历史单。</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((o) => {
    const g = o.guitu || {};
    const reason = o.fill_reason || o.state_note || (o.queue_sim && o.queue_sim.detail) || "—";
    return `<tr>
      <td><div class="name">${esc(o.name || "—")}</div><div class="code">${esc(o.ts_code || "")}</div></td>
      <td>${esc(o.industry || g.industry || "—")}</td>
      <td>${roleTag(g.role, g.role_cn)}</td>
      <td>${esc(fmtTs(o.entry_frame_ts))}</td>
      <td class="num">${fmtNum(o.entry_price)}</td>
      <td class="num">${fmtInt(o.qty)}</td>
      <td>${statusTag(o.status)}</td>
      <td class="muted">${esc(reason)}</td>
    </tr>`;
  }).join("");
}

function renderPairs(pairs, day) {
  const wrap = $("#pairsWrap");
  wrap.innerHTML = "";
  const all = [...(pairs?.pairs || []), ...(pairs?.unpaired || [])].filter((p) => p.day === day);
  if (!all.length) {
    wrap.appendChild(el("div", "pair-card", `<h4>今日无龟兔组合</h4><div class="empty-card">B1 只收二板（streak==2）；组内没有同时出现一字兔子与换手乌龟时不硬凑。</div>`));
    return;
  }
  for (const p of all) {
    const card = el("div", "pair-card");
    const members = [...(p.rabbits || []), ...(p.turtles || [])];
    card.innerHTML = `<h4>${esc(p.industry || "同题材")} <span class="tag ${p.is_pair ? "turtle" : "unknown"}">${
      p.is_pair ? "龟兔配对" : "未配对"}</span></h4>
      <div class="meta">${esc(fmtDay(p.day))} · ${members.length} 只二板 · ${esc(p.note || "")}</div>
      ${members.map((m) => `<div class="member">
        <span>${roleTag(m.role, m.role_cn)}</span>
        <span class="who">${esc(m.name || "")} <span class="code">${esc(m.ts_code || "")}</span></span>
        <span class="why">${esc(m.reason || m.status || "")}</span>
      </div>`).join("")}`;
    wrap.appendChild(card);
  }
}

function renderPositions(d) {
  const body = $("#posBody");
  const rows = [...(d.positions || [])].sort((a, b) =>
    String(b.entry_date || "").localeCompare(String(a.entry_date || "")));
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty">暂无持仓或已结算记录。</td></tr>`;
    return;
  }
  body.innerHTML = rows.slice(0, 80).map((p) => `<tr>
    <td><div class="name">${esc(p.name || "—")}</div><div class="code">${esc(p.ts_code || "")}</div></td>
    <td>${esc(fmtDay(p.entry_date))}</td>
    <td>${statusTag(p.status)}</td>
    <td class="num">${fmtNum(p.entry_price)}</td>
    <td class="num">${esc(fmtDay(p.exit_date))}</td>
    <td class="num">${fmtNum(p.exit_price)}</td>
    <td class="num ${cls(p.gross_return)}">${fmtPct(p.gross_return)}</td>
    <td class="num ${cls(p.r_net)}">${fmtPct(p.r_net)}</td>
  </tr>`).join("");
}

function renderCost(d) {
  const summary = d.summary || {};
  const by = summary.by_cost || {};
  const costLabel = d.cost_label || "万0.84免五";
  // 单一口径：后端只返回一个以 cost_label 为 key 的聚合行。
  $("#costBody").innerHTML = Object.keys(by).length
    ? Object.entries(by).map(([k, v]) => `<tr>
        <td>${esc(k)} <span class="code">${esc(d.cost_rate != null ? fmtPct(d.cost_rate) : "")}</span></td>
        <td class="num">${fmtInt(v.n)}</td>
        <td class="num">${fmtPct(v.win_rate)}</td>
        <td class="num ${cls(v.gross_mean)}">${fmtPct(v.gross_mean)}</td>
        <td class="num ${cls(v.gross_median)}">${fmtPct(v.gross_median)}</td>
      </tr>`).join("")
    : `<tr><td colspan="5" class="empty">暂无已结算样本。</td></tr>`;

  const gross = summary.gross || {};
  const chips = [
    ["委托总数", fmtInt(summary.n_orders_all)],
    ["已成", fmtInt(summary.n_filled_all)],
    ["已结算", fmtInt(summary.n_trades_closed)],
    ["毛收益胜率", fmtPct(gross.win_rate)],
    ["毛收益中位", fmtPct(gross.gross_median)],
  ];
  $("#costSummary").innerHTML = chips.map(([k, v]) =>
    `<div class="summary-chip">${esc(k)}<b>${esc(v)}</b></div>`).join("") +
    `<div class="summary-chip">${esc(d.cost_status || "")}</div>`;
}

/* ------------------------------------------------------------------ guitu */
function renderGuitu(g) {
  const sim = g?.sim || {};
  $("#btMeta").textContent = sim.available
    ? `面板 ${esc(sim.panel_last || "—")} · ${fmtInt(sim.panel_rows)} 行真实日线 · 数据源 feeds.build_panel`
    : "尚未生成历史回放（python -m backend.guitu_backtest）";

  const stats = sim.stats || {};
  $("#btStats").innerHTML = Object.keys(stats).length
    ? Object.entries(stats).map(([k, v]) => `<tr>
        <td>${esc(k)}</td>
        <td class="num">${fmtInt(v.n)}</td>
        <td class="num">${fmtPct(v.win)}</td>
        <td class="num ${cls(v.mean_net)}">${fmtPct(v.mean_net)}</td>
        <td class="num ${cls(v.median_net)}">${fmtPct(v.median_net)}</td>
        <td class="num ${cls(v.cum_10pct)}">${fmtPct(v.cum_10pct)}</td>
      </tr>`).join("")
    : `<tr><td colspan="6" class="empty">暂无回放统计。</td></tr>`;

  const latest = sim.latest || [];
  $("#btLatest").innerHTML = latest.length
    ? latest.map((x) => `<tr>
        <td>${esc(fmtDay(x.signal_date))}</td>
        <td><div class="name">${esc(x.name || "—")}</div><div class="code">${esc(x.ts_code || "")}</div></td>
        <td>${esc(x.industry || "—")}</td>
        <td class="num">${fmtInt(x.height ?? 2)}</td>
        <td class="num">${fmtInt(x.hold)}</td>
        <td class="num">${fmtInt(x.max_streak)}</td>
        <td class="num ${cls(x.a)}">${fmtPct(x.a)}</td>
      </tr>`).join("")
    : `<tr><td colspan="7" class="empty">暂无二板事件。</td></tr>`;

  const cav = sim.caveats || [];
  $("#btCaveats").innerHTML = cav.map((x) => `<li>${esc(x)}</li>`).join("") ||
    "<li>暂无边界声明。</li>";
}

/* ------------------------------------------------------------------- load */
async function loadAll(manual = false) {
  const btn = $("#refreshBtn");
  btn.disabled = true;
  try {
    if (manual) {
      await fetch("/api/refresh", { method: "POST" });
    }
    const [health, live, guitu] = await Promise.all([
      getJSON("/api/health", 30000).catch(() => null),
      getJSON("/api/live-sim", 90000),
      getJSON("/api/guitu", 60000).catch(() => null),
    ]);
    renderHealth(health);
    renderLiveSim(live);
    if (guitu) renderGuitu(guitu);
    $("#footerMeta").textContent =
      `${live.strategy || "悟空真经"} · 账本生成 ${live.generated_at || "—"} · 页面刷新 ${new Date().toLocaleTimeString("zh-CN")}`;
  } catch (err) {
    $("#heroTitle").textContent = "载入失败";
    $("#heroReason").textContent = String(err && err.message ? err.message : err);
    setText("#healthPill", "服务异常", "bad");
  } finally {
    btn.disabled = false;
  }
}

$("#refreshBtn").addEventListener("click", () => loadAll(true));
loadAll(false);
setInterval(() => loadAll(false), 60000);
