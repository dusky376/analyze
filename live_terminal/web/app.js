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
  const ac = h?.archive_check || {};
  const tc = ac.themes || {};
  // 留档自检：missing / stale 时降级为预警，避免把"数据没到"误读为"策略没信号"。
  const archiveBad = ac.status === "missing" || ac.status === "stale";
  const themeBad = tc.status === "missing" || tc.status === "stale";
  setText("#healthPill",
    !ok ? "数据异常" : archiveBad ? "留档缺失" : themeBad ? "题材池断更" : "数据正常",
    !ok ? "bad" : (archiveBad || themeBad) ? "warn" : "ok");
  $("#healthPill").title = [
    ac.message || "",
    tc.message ? `题材池：${tc.message}` : "",
    `panel_last=${last} · live_latest=${live} · recorder=${
      h?.recorder?.running ? "running" : "stopped"}`,
  ].filter(Boolean).join("\n");
  const market = h?.market?.reason || "";
  setText("#marketPill", market ? `A股 ${market}` : "A股 —");
  $("#dayPill").textContent = fmtDay(h?.day);
  // 顶部 notice：留档缺失优先；两者都正常时清空。
  if (archiveBad) notice(`留档自检：${ac.message}`);
  else if (themeBad) notice(`题材池：${tc.message}`);
  else notice("");
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
    // 具体原因（如「当日无题材池标签」）优先；没有才退回通用说明，
    // 避免把「数据没到」笼统显示成「策略没开仓」。
    const why = (Array.isArray(d.notes) && d.notes.length)
      ? d.notes.join(" ") : "";
    reason.textContent = why || d.no_orders_reason || "当日无二板委托。";
    if (why) reason.title = d.no_orders_reason || "";
    notice(why || d.no_orders_reason || "");
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
  // core_definition 是对象（{core, board, rabbit, ...}），取「核心」本意的
  // core 字段；直接渲染整个对象会变成 "[object Object]"（2026-11-24 用户实测）。
  const coreText = typeof d.core_definition === "object" && d.core_definition
    ? d.core_definition.core
    : d.core_definition;
  rules.innerHTML = [
    ["买点", d.entry_rule],
    ["卖点", d.exit_rule],
    ["核心", coreText],
  ].filter(([, v]) => v).map(([k, v]) => `<span>${esc(k)}：${esc(v)}</span>`).join("");

  $("#todayMeta").textContent =
    `${fmtDay(day)} · 涨停池帧 ${fmtInt(d.frames)} · 今日委托 ${fmtInt(nToday)} · 已成交 ${fmtInt(nFilled)}`;
  renderTodayOrders(d.today_orders || [], day);
  renderPairs(d.pairs, day);
  renderPositions(d);
  renderAccount(d);
  renderCost(d);
  renderEmotion(d.ice, d.emotion);
  $("#footerMeta").textContent =
    `账本生成 ${d.generated_at || "—"} · ${d.strategy || ""} · 快照源 ${d.source || "sync"}`;
}

// 排板成交率诊断：从 queue_sim 提取"封单消耗比"连续值（只读展示，不参与判定）。
// ≥RATIO_FILL(后端≈1) 表示排到；数值越高越接近成交。取各轮最大值 + 全天净消耗比。
function fillRatio(o) {
  const q = o && o.queue_sim;
  if (!q) return null;
  const rounds = q.rounds || [];
  let best = null;
  for (const r of rounds) {
    if (has(r.ratio) && Number.isFinite(Number(r.ratio))) {
      const v = Number(r.ratio);
      best = best === null ? v : Math.max(best, v);
    }
  }
  // 全天未开板但有净消耗：seal_consumed / seal_fund_at_entry
  if (best === null && has(q.seal_consumed_yi) && has(q.seal_fund_at_entry_yi)
      && Number(q.seal_fund_at_entry_yi) > 0) {
    best = Number(q.seal_consumed_yi) / Number(q.seal_fund_at_entry_yi);
  }
  return best;
}
function ratioCell(o) {
  const r = fillRatio(o);
  if (r === null) return `<td class="num muted">—</td>`;
  const cls2 = r >= 1 ? "up" : r >= 0.5 ? "flat" : "down";
  return `<td class="num ${cls2}" title="封单消耗比 ${r.toFixed(2)}；≥1 排到">${r.toFixed(2)}</td>`;
}

function renderTodayOrders(rows, day) {
  const body = $("#todayBody");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="9" class="empty">${esc(fmtDay(day))} 无排板委托 —— 当日策略未开仓，不回退展示历史单。</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((o) => {
    const g = o.guitu || {};
    const reason = o.fill_reason || o.state_note || (o.queue_sim && o.queue_sim.detail) || "—";
    return `<tr>
      <td><div class="name">${esc(o.name || "—")}</div><div class="code">${esc(o.ts_code || "")}</div></td>
      <td>${esc(o.guitu_theme || o.industry || g.industry || "—")}${
        o.guitu_theme && o.industry ? `<div class="code">${esc(o.industry)}</div>` : ""}</td>
      <td>${roleTag(g.role, g.role_cn)}</td>
      <td>${esc(fmtTs(o.entry_frame_ts))}</td>
      <td class="num">${fmtNum(o.entry_price)}</td>
      <td class="num">${fmtInt(o.qty)}</td>
      ${ratioCell(o)}
      <td class="num muted" title="${esc(o.budget_reason || "")}">${
        has(o.size_budget_yuan) ? fmtInt(o.size_budget_yuan) : (has(o.notional) ? fmtInt(o.notional) : "—")}</td>
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

// 交易软件式账户面板（用户 2026-11-24 要求）：总金额/可用/持仓/当日盈亏。
function renderAccount(d) {
  const a = d.account;
  const box = $("#accountSummary");
  if (!box) return;
  if (!a || a.total_amount === undefined) {
    box.innerHTML = `<div class="summary-chip">账户数据不可用</div>`;
    return;
  }
  const money = (v) => fmtInt(v);
  const pnlChip = (label, v, n) => {
    const tone = v > 0 ? "up" : v < 0 ? "down" : "flat";
    return `<div class="summary-chip">${label}` +
      `<b class="${tone}">${Number(v) > 0 ? "+" : ""}${money(v)}</b>` +
      (has(n) ? `<small>（${fmtInt(n)} 笔）</small>` : "") +
      `</div>`;
  };
  box.innerHTML = [
    `<div class="summary-chip">总金额<b>${money(a.total_amount)}</b></div>`,
    `<div class="summary-chip">可用金额<b>${money(a.available)}</b></div>`,
    `<div class="summary-chip">持仓金额<b>${money(a.position_amount)}</b>` +
      `<small>（${fmtInt(a.n_holding)} 只 · ${esc(a.position_pct ?? "—")}%）</small></div>`,
    pnlChip("当日盈亏", a.today_pnl, a.today_pnl_n),
    pnlChip("累计盈亏", a.total_pnl, a.total_pnl_n),
    `<div class="summary-chip muted" style="font-size:11px">${esc(a.note || "")}</div>`,
  ].join("");
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

/* ------------------------------------------------------- exit alternatives */
function renderExitAlternatives(d) {
  if (!d || !d.available) {
    $("#exitAltMeta").textContent = (d && d.reason) || "卖法对照暂不可用。";
    return;
  }
  const s = d.summary || {};
  const cur = s.current || {}, sl = s.stoploss || {};
  const delta = s.delta_mean_pp;
  $("#exitAltMeta").textContent =
    `n=${esc(d.n)} · 现行均值 ${fmtPct(cur.mean)} / 中位 ${fmtPct(cur.median)}` +
    ` · 固定止损模拟均值 ${fmtPct(sl.mean)} / 中位 ${fmtPct(sl.median)}` +
    (has(delta) ? ` · 均值差 ${Number(delta) >= 0 ? "+" : ""}${Number(delta).toFixed(2)}pp（假设，非结论）` : "");

  $("#exitAltBody").innerHTML = (d.trades || []).map((t) => {
    const ctx = t.context || {};
    const gate = ctx.gate_blocked === true ? "闸门拦截" : ctx.gate_blocked === false ? "闸门通过" : "闸门未知";
    // high_avg_ret 后端是**百分数**（-0.37 表示 -0.37%），fmtPct 期望小数 → 必须 /100。
    // 2026-11-23 修复：此前漏除，页面显示成 -37.00%（放大 100 倍）。
    const high = has(ctx.high_avg_ret)
      ? `高标均涨 ${fmtPct(Number(ctx.high_avg_ret) / 100)}` : "高标 —";
    const ld = has(ctx.high_limit_down_share) ? `跌停占比 ${(Number(ctx.high_limit_down_share) * 100).toFixed(0)}%` : "";
    const brk = ctx.break_is_limit_down === true ? "断板跌停" : ctx.break_is_limit_down === false ? "断板未跌停" : "断板 —";
    const envText = [gate, high, ld, brk].filter(Boolean).join(" · ");
    return `<tr>
      <td><div class="name">${esc(t.name || "—")}</div><div class="code">${esc(t.code || "")}</div></td>
      <td class="num ${cls(t.current_return)}">${fmtPct(t.current_return)}</td>
      <td class="num ${cls(t.stoploss_return)}">${fmtPct(t.stoploss_return)}</td>
      <td class="num ${cls(t.delta_pp)}">${has(t.delta_pp) ? `${Number(t.delta_pp) >= 0 ? "+" : ""}${Number(t.delta_pp).toFixed(1)}pp` : "—"}</td>
      <td class="muted">${esc(envText)}</td>
      <td class="muted">${esc(t.stoploss_note || "—")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="empty">暂无已结算样本。</td></tr>`;

  renderEnvGroups(d.by_env || [], d.n);

  $("#exitAltCaveats").innerHTML = (d.caveats || []).map((x) => `<li>${esc(x)}</li>`).join("") ||
    "<li>暂无边界声明。</li>";
}

// 按环境分组汇总：让"改善来自哪类情况"一眼可见。
const ENV_DIM_LABEL = {
  break_limit_down: "断板是否跌停",
  gate: "情绪闸门",
  high_avg_ret: "昨日高标均涨",
  high_limit_down_share: "高标跌停占比",
};

function renderEnvGroups(groups, totalN) {
  const box = $("#exitEnvGroups");
  if (!box) return;
  if (!groups.length) {
    box.innerHTML = `<div class="empty-card">暂无环境分组数据。</div>`;
    return;
  }
  // 按维度归并，每个维度一张小表
  const byDim = {};
  for (const g of groups) (byDim[g.dimension] ||= []).push(g);
  box.innerHTML = Object.entries(byDim).map(([dim, rows]) => {
    const label = ENV_DIM_LABEL[dim] || dim;
    return `<div class="env-group">
      <h4>${esc(label)}</h4>
      <table>
        <thead><tr>
          <th>分组</th><th class="num">笔数</th><th class="num">现行均值</th>
          <th class="num">止损模拟</th><th class="num">改善</th>
        </tr></thead>
        <tbody>${rows.map((g) => `<tr>
          <td>${esc(g.bucket)}${g.small_sample ? ' <span class="tag unknown">样本少</span>' : ""}</td>
          <td class="num">${fmtInt(g.n)}</td>
          <td class="num ${cls(g.current && g.current.mean)}">${fmtPct(g.current && g.current.mean)}</td>
          <td class="num ${cls(g.stoploss && g.stoploss.mean)}">${fmtPct(g.stoploss && g.stoploss.mean)}</td>
          <td class="num ${cls(g.delta_mean_pp)}">${has(g.delta_mean_pp) ? `${Number(g.delta_mean_pp) >= 0 ? "+" : ""}${Number(g.delta_mean_pp).toFixed(1)}pp` : "—"}</td>
        </tr>`).join("")}</tbody>
      </table>
      ${rows.some((g) => g.small_sample) ? '<p class="muted">含「样本少」组（n&lt;5），其改善幅度不可当结论。</p>' : ""}
    </div>`;
  }).join("") + `<p class="muted">共 ${fmtInt(totalN)} 笔；分组为描述性切分，未做显著性检验，分组越多偶然越大。</p>`;
}

/* ------------------------------------------------------- emotion / ice */
function renderEmotion(ice, emo) {
  // 市场级冰点：决定仓位，不决定是否空仓（用户裁定：小仓试错）
  const iceBox = $("#iceBox");
  if (iceBox) {
    if (!ice || !ice.available) {
      iceBox.innerHTML = `<h4>市场冰点 · 仓位档</h4>
        <div class="empty-card">${esc((ice && ice.reason) || "冰点数据不可用")}</div>`;
    } else {
      const lv = ice.level || "—";
      const factor = has(ice.size_factor) ? Number(ice.size_factor) : null;
      const tone = ice.is_ice ? "down" : "up";
      const m = ice.metrics || {};
      const dims = ice.dim_levels || {};
      const dimText = Object.entries(dims)
        .map(([k, v]) => `${esc(ICE_DIM_CN[k] || k)}=${esc(v || "—")}`).join(" · ");
      const iceTag = ice.intraday
        ? `<span class="tag unknown">盘中 ${esc((ice.as_of || "").slice(11, 16) || "实时")}</span>`
        : "";
      iceBox.innerHTML = `<h4>市场冰点 · 仓位档</h4>
        <div class="status">
          <b class="${tone}">${esc(ice.verdict || "—")}</b>
          <span class="tag ${ice.is_ice ? "rabbit" : "turtle"}">${esc(lv)}</span>
          ${factor !== null ? `<span class="tag unknown">仓位 ×${factor.toFixed(2)}</span>` : ""}
          ${iceTag}
        </div>
        <p class="muted">跌停 ${esc(fmtInt(m.limit_down_n))} · 涨停 ${esc(fmtInt(m.limit_up_n))}
          · 下跌占比 ${esc(has(m.down_share) ? (Number(m.down_share) * 100).toFixed(1) + "%" : "—")}
          · 高标破板 ${esc(fmtInt(m.broken_hi_n))}</p>
        <p class="muted">${dimText}</p>
        ${(ice.reasons || []).length ? `<p class="muted">判据：${(ice.reasons || []).map(esc).join("；")}</p>` : ""}
        <p class="muted" style="opacity:.75">${esc(ice.caveat || "")}</p>`;
    }
  }

  // 个股级情绪：决定能否打板
  const emoBox = $("#emotionBox");
  if (emoBox) {
    if (!emo || !emo.available) {
      emoBox.innerHTML = `<h4>个股情绪 · 打板闸门</h4>
        <div class="empty-card">${esc((emo && emo.reason) || "情绪数据不可用")}</div>`;
    } else {
      const blocked = emo.blocked === true;
      const emoTag = emo.intraday
        ? `<span class="tag unknown">盘中 ${esc((emo.as_of || "").slice(11, 16) || "实时")}</span>`
        : "";
      emoBox.innerHTML = `<h4>个股情绪 · 打板闸门</h4>
        <div class="status">
          <b class="${blocked ? "down" : "up"}">${esc(emo.verdict || "—")}</b>
          ${emoTag}
        </div>
        <p class="muted">昨日高标 ${esc(fmtInt(emo.n_high))} 只 · 今日均涨 ${fmtPct(
          has(emo.avg_ret) ? Number(emo.avg_ret) / 100 : null)}
          · 跌停占比 ${esc(has(emo.limit_down_share)
            ? (Number(emo.limit_down_share) * 100).toFixed(0) + "%" : "—")}</p>
        <p class="muted">条件C 昨日涨停溢价 ${fmtPct(
          has(emo.premium) ? Number(emo.premium) / 100 : null)}
          ${has(emo.n_premium) ? `（${esc(fmtInt(emo.n_premium))} 只样本）` : ""}</p>
        ${(emo.reasons || []).length ? `<p class="muted">触发：${(emo.reasons || []).map(esc).join("；")}</p>` : ""}`;
    }
  }
}

const ICE_DIM_CN = {
  down_share: "下跌占比",
  limit_down_n: "跌停数",
  dt_ratio: "跌停/涨停",
  broken_hi_n: "高标破板",
};

/* ------------------------------------------------------------------- load */
async function loadAll(manual = false) {
  const btn = $("#refreshBtn");
  btn.disabled = true;
  try {
    if (manual) {
      await fetch("/api/refresh", { method: "POST" });
    }
    const [health, live, guitu, exitAlt] = await Promise.all([
      getJSON("/api/health", 30000).catch(() => null),
      getJSON("/api/live-sim", 90000),
      getJSON("/api/guitu", 60000).catch(() => null),
      getJSON("/api/exit-alternatives", 90000).catch(() => null),
    ]);
    renderHealth(health);
    renderLiveSim(live);
    if (guitu) renderGuitu(guitu);
    if (exitAlt) renderExitAlternatives(exitAlt);
    $("#footerMeta").textContent =
      `${live.strategy || "悟空真经"} · 账本生成 ${live.generated_at || "—"} · 页面刷新 ${new Date().toLocaleTimeString("zh-CN")}`;
  } catch (err) {
    // 2026-11-23 审计 P1-11：live-sim 失败时其余区块会保留上一轮旧 DOM，
    // 演示时会变成"顶部报错、下面还是旧数据"的误导状态。这里显式标灰。
    $("#heroTitle").textContent = "载入失败";
    $("#heroReason").textContent = String(err && err.message ? err.message : err);
    setText("#healthPill", "服务异常", "bad");
    clearStalePanels();
  } finally {
    btn.disabled = false;
  }
}

// 请求失败时把各区块置为"数据不可用"，避免旧数字冒充新数字。
function clearStalePanels() {
  const emptyRows = [
    ["#todayBody", 9], ["#posBody", 8], ["#costBody", 5],
    ["#exitAltBody", 6], ["#btStats", 6], ["#btLatest", 7],
  ];
  for (const [sel, cols] of emptyRows) {
    const n = $(sel);
    if (n) n.innerHTML = `<tr><td colspan="${cols}" class="empty">数据不可用（本次请求失败）</td></tr>`;
  }
  for (const sel of ["#iceBox", "#emotionBox", "#exitEnvGroups", "#pairsWrap"]) {
    const n = $(sel);
    if (n) n.innerHTML = `<div class="empty-card">数据不可用（本次请求失败）</div>`;
  }
  for (const id of ["#kToday", "#kFilled", "#kHolding", "#kPairs"]) {
    const n = $(id);
    if (n) n.textContent = "—";
  }
}

$("#refreshBtn").addEventListener("click", () => loadAll(true));
loadAll(false);
setInterval(() => loadAll(false), 60000);
