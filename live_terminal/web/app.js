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
    cancelled: ["unknown", "主动撤单"],
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
  catTab: "us",
  lastLiveData: null,
  lastMacroData: null,
  lastGeneratedAt: null,
  serverBuild: null,
  marketTrading: false,
  inFlight: { health: false, live: false, macro: false, evo: false, bt: false, exitAlt: false },
  loaded: { evo: false, bt: false, exitAlt: false },
  sse: null,
};

/* --------------------------------------------------------------- tabs & Apple motion */
function positionTabIndicator() {
  const ind = $("#tabIndicator");
  const active = $(".tab-btn.active");
  if (!ind || !active) return;
  const x = active.offsetLeft;
  const w = active.offsetWidth;
  if (w > 0) {
    ind.style.width = `${w}px`;
    ind.style.transform = `translate3d(${x}px, 0, 0)`;
    ind.style.opacity = "1";
  }
}

let _revealObs = null;
function triggerReveal(root = document) {
  const nodes = $$(".reveal", root);
  if (!nodes.length) return;
  const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || typeof IntersectionObserver === "undefined") {
    nodes.forEach((n) => n.classList.add("in-view"));
    return;
  }
  if (!_revealObs) {
    _revealObs = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) {
          en.target.classList.add("in-view");
          _revealObs.unobserve(en.target);
        }
      });
    }, { threshold: 0.06, rootMargin: "0px 0px -24px 0px" });
  }
  nodes.forEach((n) => {
    n.classList.remove("in-view");
    _revealObs.observe(n);
  });
}

function animateKpiInt(sel, target) {
  const el = $(sel);
  if (!el) return;
  const to = Number(target ?? 0);
  if (!Number.isFinite(to)) { el.textContent = "—"; return; }
  const prev = Number(el.dataset.val);
  el.dataset.val = String(to);
  const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || !Number.isFinite(prev) || prev === to || typeof requestAnimationFrame === "undefined") {
    el.textContent = fmtInt(to);
    return;
  }
  const start = performance.now();
  const dur = 480;
  const tick = (now) => {
    const t = Math.min(1, (now - start) / dur);
    const eased = 1 - Math.pow(2, -10 * t);
    const cur = Math.round(prev + (to - prev) * eased);
    el.textContent = fmtInt(cur);
    if (t < 1) requestAnimationFrame(tick);
    else el.textContent = fmtInt(to);
  };
  requestAnimationFrame(tick);
}

function initAppleMotion() {
  // 1) 顶部玻璃导航随滚动加深层次
  const bar = $(".topbar");
  const onScroll = () => {
    if (bar) bar.classList.toggle("scrolled", window.scrollY > 10);
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // 2) 鼠标跟随高光（Apple Bento 卡片镜面质感）
  document.addEventListener("pointermove", (ev) => {
    const card = ev.target && ev.target.closest ? ev.target.closest(".spotlight") : null;
    if (!card) return;
    const rect = card.getBoundingClientRect();
    card.style.setProperty("--mx", `${(ev.clientX - rect.left).toFixed(1)}px`);
    card.style.setProperty("--my", `${(ev.clientY - rect.top).toFixed(1)}px`);
  }, { passive: true });

  // 3) 滑动胶囊自适应窗口缩放
  window.addEventListener("resize", positionTabIndicator, { passive: true });
  requestAnimationFrame(() => {
    positionTabIndicator();
    triggerReveal($("#pane-today") || document);
  });
}

function switchTab(tab) {
  state.activeTab = tab;
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${tab}`));
  positionTabIndicator();
  const pane = $(`#pane-${tab}`);
  if (pane) requestAnimationFrame(() => triggerReveal(pane));
  if (tab === "evo") {
    if (!state.loaded.evo) loadEvolution();
    if (!state.loaded.bt) loadBacktest();
  }
}

/* ------------------------------------------------------------- health */
function renderHealth(h) {
  const ok = h && h.ok;
  const p = h?.panel || {};
  const last = p.effective_last_trade_date || p.last_trade_date || "—";
  const live = h?.live_archive?.latest || "—";
  const ac = h?.archive_check || {};
  const tc = ac.themes || {};
  const archiveBad = ac.status === "missing" || ac.status === "stale";
  const themeBad = tc.status === "missing" || tc.status === "stale";
  const panelBroken = Boolean(p.continuity_broken || p.panel_ok === false);
  const panelStale = Boolean(p.stale_panel);
  const procStale = Boolean(h?.process?.stale);

  setPill("#healthPill",
    !ok ? "数据异常" : procStale ? "旧进程停写" : panelBroken ? "面板断档" : panelStale ? "日线滞后" : archiveBad ? "留档缺失" : themeBad ? "题材断更" : "数据正常",
    (!ok || procStale || panelBroken) ? "bad" : (panelStale || archiveBad || themeBad) ? "warn" : "ok");
  $("#healthPill").title = [
    p.message || "",
    ac.message || "",
    tc.message ? `题材池：${tc.message}` : "",
    procStale ? "后端运行旧版源码进程（已自动停写，请重启服务）" : "",
    `panel=${last} · live=${live} · recorder=${h?.recorder?.running ? "running" : "stopped"}`,
  ].filter(Boolean).join("\n");

  const market = h?.market?.reason || "";
  state.marketTrading = Boolean(h?.market?.trading);
  setPill("#marketPill", market ? `A股 ${market}` : "A股 —");
  $("#dayPill").textContent = fmtDay(h?.day);

  const gapNote = (p.gap_days && p.gap_days.length) ? ` <span class="down">(缺 ${esc(p.gap_days.join(","))})</span>` : "";
  $("#freshPanel").innerHTML = `日线面板：<b>${esc(last)}</b>${gapNote}`;
  $("#freshLive").innerHTML = `留档最新：<b>${esc(fmtDay(live))}</b> (${h?.recorder?.running ? "采集中" : "未采集"})`;

  const warns = [];
  if (!ok) warns.push("后端健康检查失败，请确认数据目录与服务状态。");
  if (procStale) warns.push("后端运行旧版源码进程（已自动启用版本锁停写防脏盘，请重启服务生效新代码）。");
  if (panelBroken) warns.push(`日线连续性熔断（已阻断自动挂单）：${p.message || "存在跨日数据空洞"}`);
  else if (panelStale) warns.push(`日线滞后：${p.message || `最新 ${last}`}`);
  if (archiveBad) warns.push(`留档自检：${ac.message}`);
  if (themeBad) warns.push(`题材池：${tc.message}`);
  const hn = $("#healthNotice");
  if (hn) hn.classList.toggle("bad", Boolean(!ok || procStale || panelBroken));
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
  animateKpiInt("#kToday", nToday);
  animateKpiInt("#kFilled", nFilled);
  animateKpiInt("#kHolding", nHolding);
  animateKpiInt("#kPairs", nPairs);

  const f = d.freshness || {};
  $("#freshSync").innerHTML = `账本同步：<b>${esc(fmtTs(d.generated_at))}</b>${f.market_trading ? " (盘中实时)" : ""}`;
  if (f.price_source) $("#freshSource").textContent = `数据源：${f.price_source}`;

  const title = $("#heroTitle");
  const reason = $("#heroReason");
  if (d.no_orders) {
    title.textContent = "今日不开仓";
    const notes = Array.isArray(d.notes) ? d.notes : [];
    const primary = notes.find((x) => x.includes("今日实时涨停池") || x.includes("情绪闸门拦截")) || d.no_orders_reason || "当日无二板委托。";
    reason.textContent = primary;
    const extra = notes.filter((x) => x !== primary).join(" ｜ ");
    setNotice("#strategyNotice", extra);
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
  renderRabbitStates(d.rabbit_states || {});
  renderPairs(d.today_pairs || { pairs: [] }, day, d.pre_seal_radar || []);
  renderT1RelayRadar(d.t1_relay_radar || {});
  renderSkippedCandidates(d.skipped_candidates || []);
  renderFillCategories(d.by_fill_category || {});
  renderPositions(d);
  renderAccount(d);
  renderCost(d);
  renderEmotion(d.ice, d.emotion, d.overseas, d.sizing);

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
    const orderTs = o.entry_frame_ts || o.ordered_at;
    const orderPx = has(o.entry_price) ? o.entry_price : o.limit_price;
    const budgetTip = [o.budget_reason, has(o.evo_size_mult) ? `信息素系数 ×${fmtNum(o.evo_size_mult, 2)}` : ""].filter(Boolean).join(" · ");
    return `<tr class="chart-row" data-code="${esc(o.ts_code)}" data-name="${esc(o.name)}" title="点击展开高清 K 线 / 1分钟分时">
      <td><div class="name">${esc(o.name || "—")}</div><div class="code">${esc(o.ts_code || "")}</div></td>
      <td>${esc(o.guitu_theme || o.industry || "—")}</td>
      <td>${roleTag(g.role, g.role_cn)}</td>
      ${buyQualityCell(o)}
      <td>${esc(fmtTs(orderTs))}</td>
      <td class="num">${fmtNum(orderPx)}</td>
      <td class="num">${has(o.qty) ? `${fmtInt(o.qty)}股` : "—"}</td>
      <td class="num" title="${esc(budgetTip)}">${has(o.size_budget_yuan) ? fmtInt(o.size_budget_yuan) : "—"}</td>
      ${valueCell(o)}
      <td>${statusTag(o.status)}</td>
      <td class="muted">${esc(reason)}</td>
    </tr>`;
  }).join("");
}

function renderPairs(pairs, day, radar = []) {
  const wrap = $("#pairsWrap");
  const list = (pairs && pairs.pairs) || [];
  const cards = [];
  if (!list.length) {
    cards.push(`<div class="pair-card"><h4>今日龟兔组合</h4><div class="empty-card">${esc(fmtDay(day))} 暂无同题材同身位龟兔配对（不回退展示历史日配对）。</div></div>`);
  } else {
    list.forEach((p) => {
      const rabbits = Array.isArray(p.rabbits) ? p.rabbits : [];
      const turtles = Array.isArray(p.turtles) ? p.turtles : [];
      const members = Array.isArray(p.members) ? p.members : [...rabbits, ...turtles];
      const nRabbit = has(p.n_rabbit) ? p.n_rabbit : rabbits.length;
      const nTurtle = has(p.n_turtle) ? p.n_turtle : turtles.length;
      const themeName = p.theme || p.industry || "—";
      const memberHtml = members.map((m) => `
        <div class="member">
          ${roleTag(m.role, m.role_cn)}
          <span class="who"><b>${esc(m.name || "")}</b> <span class="code">${esc(m.ts_code || "")}</span></span>
          <span class="why">${esc(m.reason || m.role_reason || "")}</span>
        </div>`).join("");
      cards.push(`<div class="pair-card">
        <h4>${esc(themeName)} · ${esc(p.streak || 2)} 板</h4>
        <div class="meta">${esc(fmtDay(p.day || day))} · 兔子 ${fmtInt(nRabbit)} · 乌龟 ${fmtInt(nTurtle)}</div>
        ${memberHtml || '<div class="empty-card">暂无明细</div>'}
      </div>`);
    });
  }
  if (Array.isArray(radar) && radar.length) {
    const rHtml = radar.slice(0, 6).map((c) => `
      <div class="member">
        <span class="tag grade-b">+${fmtNum(c.pct, 2)}%</span>
        <span class="who"><b>${esc(c.name || "")}</b> <span class="code">${esc(c.ts_code || "")} · ${esc(c.theme || "")}</span></span>
        <span class="why">距涨停 ${fmtNum(c.gap_to_limit, 2)}%</span>
      </div>`).join("");
    cards.push(`<div class="pair-card">
      <h4>临封扫板雷达（同题材有兔）</h4>
      <div class="meta">实时行情观察 · 满足薄卖一放量硬条件且有槽位时自动触发滑点扫板 · 命中 ${fmtInt(radar.length)} 只</div>
      ${rHtml}
    </div>`);
  }
  wrap.innerHTML = cards.join("");
}

function renderRabbitStates(rs) {
  const wrap = $("#rabbitStatesWrap");
  if (!wrap) return;
  const entries = Object.entries(rs || {});
  if (!entries.length) {
    wrap.innerHTML = "";
    return;
  }
  const clsMap = { anchored: "grade-a", decaying: "unfilled", diverging: "grade-b", failed: "grade-reject" };
  wrap.innerHTML = entries.map(([th, info]) => {
    const st = info.state || "failed";
    const rList = (info.rabbits || []).map((r) => `
      <div class="member">
        <span class="tag ${clsMap[r.state] || "grade-c"}">${esc(r.state_cn || r.state)}</span>
        <span class="who"><b>${esc(r.name || r.ts_code)}</b> <span class="code">${esc(r.ts_code)} · ${fmtInt(r.streak)}板 · 强度 ${fmtNum(r.strength_score, 0)}</span></span>
        <span class="why">${fmtNum(r.peak_seal_yi, 2)}→${fmtNum(r.latest_seal_yi, 2)}亿 (${esc(r.trend || "平稳")})</span>
      </div>`).join("");
    return `<div class="pair-card">
      <h4>一字兔健康灯 · ${esc(th)} <span class="tag ${clsMap[st] || "grade-c"}">${esc(info.state_cn || st)} (${fmtNum(info.strength_score, 0)}分)</span></h4>
      <div class="meta">${esc(info.note || "")}</div>
      ${rList}
    </div>`;
  }).join("");
}

function renderSkippedCandidates(skipped) {
  const el = $("#skippedWrap");
  if (!el) return;
  if (!Array.isArray(skipped) || !skipped.length) {
    el.innerHTML = "";
    return;
  }
  const rows = skipped.map((s) => {
    const bq = s.buy_quality || {};
    const scTxt = has(bq.raw_score) ? `${bq.raw_score}分` : (has(bq.score) ? `${bq.score}分` : "—");
    return `<tr class="chart-row" data-code="${esc(s.ts_code)}" data-name="${esc(s.name)}">
      <td><div class="name">${esc(s.name || "—")}</div><div class="code">${esc(s.ts_code || "")}</div></td>
      <td>${esc(s.theme || "—")}</td>
      <td class="num">${esc(scTxt)}</td>
      <td class="muted">${esc(s.reason || "")}</td>
    </tr>`;
  }).join("");
  el.innerHTML = `<div class="sub-panel-box">
    <h4>被过滤 / 槽位顺延候选 (${fmtInt(skipped.length)} 只) <span class="muted">解释为何前排未下单或顺延（点击行可看 K 线）</span></h4>
    <div class="table-wrap"><table>
      <thead><tr><th>代码 / 名称</th><th>题材</th><th class="num">买点评分</th><th>跳过 / 顺延原因</th></tr></thead>
      <tbody id="skippedBody">${rows}</tbody>
    </table></div>
  </div>`;
  const sb = $("#skippedBody");
  if (sb) sb.addEventListener("click", (e) => {
    const tr = e.target.closest("tr.chart-row");
    if (tr) toggleInlineChart(tr, tr.dataset.code, tr.dataset.name);
  });
}

function renderT1RelayRadar(radar) {
  const el = $("#t1RelayWrap");
  if (!el) return;
  const w2s = radar?.weak_to_strong_auction || [];
  const rs = radar?.reseal_confirm || [];
  const wl = radar?.watchlist || [];
  if (!w2s.length && !rs.length && !wl.length) {
    el.innerHTML = "";
    return;
  }
  const w2sHtml = w2s.map((x) => `<span class="stock-pill positive" data-code="${esc(x.ts_code)}" data-name="${esc(x.name)}" title="${esc(x.note || "")}">
    ${esc(x.name || x.ts_code)} <small>+${fmtNum(x.open_gap, 1)}%${x.vol_unverified ? "·缺量核验" : ""}</small>
  </span>`).join("");
  const rsHtml = rs.map((x) => `<span class="stock-pill" data-code="${esc(x.ts_code)}" data-name="${esc(x.name)}" title="${esc(x.note || "")}">
    ${esc(x.name || x.ts_code)} <small>回封 ${fmtNum(x.seal_fund_yi, 2)}亿</small>
  </span>`).join("");
  const wlHtml = (!w2s.length && !rs.length) ? wl.slice(0, 8).map((x) => stockPillHtml({
    ts_code: x.ts_code, name: x.name, high_days: x.theme, reason: x.reason,
  })).join("") : "";
  el.innerHTML = `<div class="sub-panel-box" id="t1RelayBox">
    <h4>T+1 二进三接力观察 <span class="muted">来源日 ${esc(fmtDay(radar.source_day || ""))} · 弱转强 ${fmtInt(w2s.length)} / 分歧回封 ${fmtInt(rs.length)} / 顺延池 ${fmtInt(wl.length)}</span></h4>
    <div class="stock-pills">${w2sHtml}${rsHtml}${wlHtml}</div>
  </div>`;
  const box = $("#t1RelayBox");
  if (box) box.addEventListener("click", (e) => {
    const btn = e.target.closest("button.stock-pill, span.stock-pill");
    if (btn && btn.dataset.code) openMacroStockChart(btn.dataset.code, btn.dataset.name);
  });
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
  const capital = has(a.capital_yuan) ? Number(a.capital_yuan) : (has(d.capital_yuan) ? Number(d.capital_yuan) : 1000000);
  const equity = has(a.total_amount) ? Number(a.total_amount) : (has(a.equity_yuan) ? Number(a.equity_yuan) : null);
  const cash = has(a.available) ? Number(a.available) : (has(a.cash_yuan) ? Number(a.cash_yuan) : null);
  const posVal = has(a.position_amount) ? Number(a.position_amount) : (has(a.position_value_yuan) ? Number(a.position_value_yuan) : null);
  const pnl = a.total_pnl;
  const ret = has(a.total_return) ? Number(a.total_return) : (has(pnl) && capital > 0 ? Number(pnl) / capital : null);
  const todayPnl = a.today_pnl;
  const chips = [
    ["初始本金", fmtInt(capital)],
    ["总资产", fmtInt(equity)],
    ["可用现金", fmtInt(cash)],
    ["持仓市值", fmtInt(posVal)],
    ["今日盈亏", has(todayPnl) ? `${todayPnl > 0 ? "+" : ""}${fmtInt(todayPnl)}` : "—", cls(todayPnl)],
    ["累计盈亏", has(pnl) ? `${pnl > 0 ? "+" : ""}${fmtInt(pnl)} (${fmtPct(ret)})` : "—", cls(pnl)],
  ];
  el.innerHTML = chips.map(([k, v, c]) =>
    `<div class="summary-chip"><span>${esc(k)}</span><b class="${c || ""}">${esc(v)}</b></div>`
  ).join("");

  const denom = (equity && equity > 0) ? equity : capital;
  if (denom > 0 && has(posVal)) {
    const pct = has(a.position_pct) ? Number(a.position_pct) : Math.min(100, (posVal / denom) * 100);
    capWrap.classList.remove("hidden");
    $("#capBarFill").style.width = `${Math.min(100, Math.max(0, pct)).toFixed(1)}%`;
    $("#capBarText").textContent = `${pct.toFixed(1)}% (${fmtInt(posVal)} / 总仓上限 ${fmtInt(Math.round(denom * 0.8))})`;
  } else {
    capWrap.classList.add("hidden");
  }
}

function renderCost(d) {
  const s = d.summary || {};
  const gross = s.gross || {};
  const byCost = s.by_cost || {};
  const netStat = (d.cost_label && byCost[d.cost_label]) || Object.values(byCost)[0] || {};
  const nClosed = has(s.n_trades_closed) ? s.n_trades_closed : (has(s.n_closed) ? s.n_closed : (gross.n ?? 0));
  const nHolding = has(d.n_holding) ? d.n_holding : (d.account?.n_holding ?? s.n_holding ?? 0);
  const wrGross = has(gross.win_rate) ? gross.win_rate : s.win_rate_gross;
  const avgGross = has(gross.gross_mean) ? gross.gross_mean : s.avg_gross;
  const avgNet = has(netStat.gross_mean) ? netStat.gross_mean : s.avg_net;
  const medNet = has(netStat.gross_median) ? netStat.gross_median : s.median_net;
  const wrNet = has(netStat.win_rate) ? netStat.win_rate : s.win_rate_net;

  const chips = [
    ["已结算", fmtInt(nClosed)],
    ["持有中", fmtInt(nHolding)],
    ["胜率(毛)", fmtPct(wrGross, 1)],
    ["均值毛", fmtPct(avgGross), cls(avgGross)],
    ["均值净(万0.84免五)", fmtPct(avgNet), cls(avgNet)],
    ["单笔上限", `${fmtInt(d.position_size_yuan || 100000)} 元`],
  ];
  $("#costSummary").innerHTML = chips.map(([k, v, c]) =>
    `<div class="summary-chip"><span>${esc(k)}</span><b class="${c || ""}">${esc(v)}</b></div>`
  ).join("");

  const body = $("#costBody");
  if (!nClosed) {
    body.innerHTML = `<tr><td colspan="5" class="empty">尚无已结算交易，结算后自动汇总。</td></tr>`;
    return;
  }
  body.innerHTML = `<tr>
    <td>${esc(d.cost_label || "万0.84免五")}</td>
    <td class="num">${fmtInt(netStat.n ?? nClosed)}</td>
    <td class="num">${fmtPct(wrNet, 1)}</td>
    <td class="num ${cls(avgNet)}">${fmtPct(avgNet)}</td>
    <td class="num ${cls(medNet)}">${fmtPct(medNet)}</td>
  </tr>`;
}

function renderEmotion(ice, emo, ovs, sizing) {
  const iceEl = $("#iceBox");
  const emoEl = $("#emotionBox");
  const ovsEl = $("#overseasBox");

  if (!ice || !ice.available) {
    iceEl.innerHTML = `<h4>市场冰点 · 仓位档位</h4><div class="empty-card">${esc((ice && ice.reason) || "市场冰点数据暂不可用")}</div>`;
  } else {
    const isIce = Boolean(ice.is_ice);
    const lv = ice.level || ice.severity_level || "—";
    const sf = has(sizing?.effective_size_factor) ? Number(sizing.effective_size_factor)
      : (has(ice.size_factor) ? Number(ice.size_factor) : 1);
    const tagCls = isIce ? (lv === "L5" ? "grade-reject" : "unfilled") : "grade-a";
    const isSingleDimHigh = !isIce && (lv === "L4" || lv === "L5");
    const tagTxt = isIce
      ? `${lv} 冰点 · 生效仓位 ×${sf.toFixed(2)}`
      : (isSingleDimHigh
        ? `非冰点（单维${lv}） · 生效仓位 ×${sf.toFixed(2)}`
        : `${lv} 常态 · 生效仓位 ×${sf.toFixed(2)}`);
    const m = ice.metrics || {};
    const dl = ice.dim_levels || {};
    const refDay = m.day || ice.day || "—";
    const refSrc = ice.source || (ice.intraday ? `盘中实时行情${ice.as_of ? ` (${ice.as_of})` : ""}` : "真实日线面板");
    const rows = [
      ["下跌占比", has(m.down_share) ? `${(m.down_share * 100).toFixed(1)}%` : "—", dl.down_share || "—"],
      ["跌停家数", has(m.limit_down_n) ? `${fmtInt(m.limit_down_n)} 家` : "—", dl.limit_down_n || "—"],
      ["跌停/涨停", has(m.dt_ratio) ? fmtNum(m.dt_ratio, 2) : "—", dl.dt_ratio || "—"],
      ["昨日≥3板断板", has(m.broken_hi_n) ? `${fmtInt(m.broken_hi_n)} 家` : "—", dl.broken_hi_n || "—"],
    ];
    if (has(m.vol_ratio_5d)) {
      const vp = ice.decline_regime?.percentiles?.vol_ratio_5d;
      rows.push(["全市场5日量比", `${fmtNum(m.vol_ratio_5d, 2)}×`, has(vp) ? `P${Math.round(vp * 100)}` : "—"]);
    }
    const dr = ice.decline_regime;
    let drHtml = "";
    if (dr && dr.active) {
      const regCls = dr.regime === "panic_collapse" ? "grade-reject"
        : dr.regime === "narrow_siphon" ? "grade-b" : "unfilled";
      const secBadge = dr.secondary_cn ? `<span class="tag grade-c">兼 ${esc(dr.secondary_cn)}</span>` : "";
      const st250 = dr.rolling_250d_stats || {};
      const th250 = dr.adaptive_thresholds || {};
      const rebTxt = st250.n > 0 && has(st250.rebound_rate)
        ? `近${st250.window_days || 250}日同类 ${st250.n} 次 · 次日修复率 ${(st250.rebound_rate * 100).toFixed(0)}%`
        : `滚动样本窗 ${th250.window_days || 0} 日`;
      const thParts = [];
      if (has(th250.down_P80)) thParts.push(`普跌P80=${(th250.down_P80 * 100).toFixed(0)}%`);
      if (has(th250.ld_P80)) thParts.push(`跌停P80=${Math.round(th250.ld_P80)}家`);
      if (has(th250.vol_P25)) thParts.push(`缩量P25=${Number(th250.vol_P25).toFixed(2)}×`);
      drHtml = `<div class="regime-box">
        <div class="regime-head">
          <span class="tag ${regCls}">${esc(dr.regime_cn || "普跌细分")}</span>
          ${secBadge}
          <span class="regime-mult">动态系数 <b>×${fmtNum(dr.adapted_mult, 2)}</b></span>
        </div>
        <div class="regime-hint">${esc(dr.action_hint || "")}</div>
        <div class="regime-meta">${esc(rebTxt)}${thParts.length ? ` · 动态标尺 ${esc(thParts.join(" / "))}` : ""}</div>
        <div class="regime-meta muted">${esc(dr.adaptation_note || "")}</div>
      </div>`;
    }
    const formulaLine = sizing?.formula ? `<p class="regime-meta">统一口径：<b>${esc(sizing.formula)}</b></p>` : "";
    iceEl.innerHTML = `<h4>市场冰点 · 仓位档位</h4>
      <div class="status"><span class="tag ${tagCls}">${esc(tagTxt)}</span><span>${esc(ice.verdict || "")}</span></div>
      <table><thead><tr><th>维度</th><th class="num">实测</th><th class="num">分位档</th></tr></thead>
      <tbody>${rows.map(([k, v, l]) => `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td><td class="num">${esc(l)}</td></tr>`).join("")}</tbody></table>
      ${drHtml}${formulaLine}
      <p class="muted">基准日 ${esc(refDay)} · 数据源 ${esc(refSrc)}</p>`;
  }

  if (!emo || !emo.available) {
    emoEl.innerHTML = `<h4>个股情绪 · 打板闸门</h4><div class="empty-card">${esc((emo && emo.reason) || "个股情绪数据暂不可用")}</div>`;
  } else {
    const blocked = Boolean(emo.blocked);
    const tagCls = blocked ? "grade-reject" : "grade-a";
    const tagTxt = blocked ? "闸门拦截 · 今日不开仓" : "闸门通过 · 可做二板";
    const avgPct = has(emo.avg_ret) ? Number(emo.avg_ret) : (has(emo.avg_pct) ? Number(emo.avg_pct) : null);
    const prem = has(emo.premium) ? Number(emo.premium) : null;
    const rows = [
      ["昨日≥2板高标数", has(emo.n_high) ? `${fmtInt(emo.n_high)} 只` : "—", ""],
      ["高标今日均涨", avgPct !== null ? `${avgPct > 0 ? "+" : ""}${avgPct.toFixed(2)}%` : "—", cls(avgPct)],
      ["高标跌停占比", has(emo.limit_down_share) ? `${(emo.limit_down_share * 100).toFixed(1)}%` : "—", ""],
      ["昨日涨停溢价", prem !== null ? `${prem > 0 ? "+" : ""}${prem.toFixed(2)}%${has(emo.n_premium) ? ` (${emo.n_premium}只)` : ""}` : "—", cls(prem)],
    ];
    const hb = emo.high_board_pulse;
    let hbHtml = "";
    if (hb && hb.available) {
      const hbCls = hb.state === "drag" ? "grade-reject"
        : hb.state === "warn" ? "unfilled"
        : hb.state === "lead" ? "grade-a" : "grade-c";
      const lPills = (hb.leaders || []).map((ld) => {
        const pTxt = has(ld.pct) ? `${ld.pct > 0 ? "+" : ""}${Number(ld.pct).toFixed(1)}%` : "—";
        const pCls = ld.status === "跌停" ? "danger" : (ld.status === "晋级" ? "positive" : "");
        return `<span class="leader-pill ${pCls}" title="${esc((ld.themes || []).join("·"))}">${esc(ld.name)} <small>${ld.prev_streak}板</small> <b class="${cls(ld.pct)}">${pTxt}</b></span>`;
      }).join("");
      hbHtml = `<div class="regime-box">
        <div class="regime-head">
          <span class="tag ${hbCls}">${esc(hb.state_cn || "高标梯队")}</span>
          <span class="regime-mult">梯队系数 <b>×${fmtNum(hb.size_mult, 2)}</b></span>
        </div>
        <div class="regime-meta">${esc(hb.note || "")}</div>
        ${lPills ? `<div class="leader-pills">${lPills}</div>` : ""}
      </div>`;
    }
    emoEl.innerHTML = `<h4>个股情绪 · 打板闸门</h4>
      <div class="status"><span class="tag ${tagCls}">${esc(tagTxt)}</span><span>${esc(emo.verdict || emo.reason || "")}</span></div>
      <table><thead><tr><th>指标</th><th class="num">数值</th></tr></thead>
      <tbody>${rows.map(([k, v, c]) => `<tr><td>${esc(k)}</td><td class="num ${c}">${esc(v)}</td></tr>`).join("")}</tbody></table>
      ${hbHtml}`;
  }

  if (ovsEl && ovs && ovs.available) {
    renderOverseasBox(ovs);
  }
}

function renderOverseasBox(ovs) {
  const ovsEl = $("#overseasBox");
  if (!ovsEl) return;
  if (!ovs || !ovs.available) {
    ovsEl.innerHTML = `<h4>外盘风偏 · 日韩美股 / A50</h4><div class="empty-card">外盘报价暂不可用</div>`;
    return;
  }
  const s = ovs.sentiment || {};
  const lv = s.risk_level || "neutral";
  const tagCls = lv === "risk_off" ? "grade-reject" : lv === "caution" ? "unfilled" : "grade-a";
  const mult = has(s.size_mult) ? Number(s.size_mult) : 1.0;
  const scStr = has(s.score) ? `${Number(s.score) > 0 ? "+" : ""}${Number(s.score).toFixed(1)}` : "—";
  const pickKeys = new Set(["KOSPI", "N225", "A50", "IXIC", "SOX", "HSI", "KWEB"]);
  const rows = (ovs.indices || []).filter((x) => pickKeys.has(x.key)).map((x) => {
    const isSilent = Boolean(x.stale || x.holiday_silent);
    const p = isSilent ? "休市静默" : (has(x.pct) ? `${Number(x.pct) > 0 ? "+" : ""}${Number(x.pct).toFixed(2)}%` : "—");
    const clsVal = isSilent ? "muted" : cls(x.pct);
    return `<tr><td>${esc(x.name)} <span class="code">${esc(x.region)}${isSilent && x.stale_hours ? `·${Math.round(x.stale_hours)}h前` : ""}</span></td><td class="num ${clsVal}"><b>${esc(p)}</b></td></tr>`;
  }).join("");
  ovsEl.innerHTML = `<h4>外盘风偏 · 日韩美股 / A50</h4>
    <div class="status"><span class="tag ${tagCls}">${esc(s.risk_label || "外盘中性")} ×${mult.toFixed(2)}</span><span>情绪分 <b>${esc(scStr)}</b></span></div>
    <table><thead><tr><th>全球核心指数</th><th class="num">涨跌幅</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="muted">${esc((s.reasons || []).join(" · ") || ovs.source || "")}</p>`;
}

function stockPillHtml(s, extraCls = "") {
  const code = s.ts_code || s.code || "";
  const name = s.name || code;
  const badge = s.high_days ? `<small>${esc(s.high_days)}</small>` : "";
  const tip = s.reason ? `${name} (${code}) · ${s.reason}` : `${name} (${code}) · 点击查看K线/分时`;
  return `<button type="button" class="stock-pill ${extraCls}" data-code="${esc(code)}" data-name="${esc(name)}" title="${esc(tip)}">${esc(name)} <span class="code">${esc(code)}</span>${badge}</button>`;
}

function renderCatalystBox(d) {
  const catEl = $("#catalystBox");
  if (!catEl || !d) return;
  const ovs = d.overseas || {};
  const ns = d.news || {};
  const usCats = (ovs.catalysts || []).filter((c) => c.leaders && c.leaders.length);
  const cnCats = (ns.theme_catalysts || []).slice(0, 7);
  const isUs = state.catTab !== "cn";

  const bodyHtml = isUs
    ? `<table><thead><tr><th>映射 A 股板块 / 美股龙头 / 对应 A 股</th><th class="num">均涨</th><th>方向</th></tr></thead>
      <tbody>${usCats.length ? usCats.map((c) => {
        const dirCls = c.direction === "bullish" ? "grade-a" : c.direction === "bearish" ? "grade-reject" : "grade-c";
        const dirTxt = c.direction === "bullish" ? "美股催化" : c.direction === "bearish" ? "美股承压" : "平稳";
        const pills = (c.leaders || []).slice(0, 4).map((l) =>
          `<span class="leader-pill ${cls(l.pct)}">${esc(l.name)} ${l.pct > 0 ? "+" : ""}${Number(l.pct).toFixed(1)}%</span>`
        ).join("");
        const aStocks = (c.mapped_stocks || []).slice(0, 4).map((s) => stockPillHtml(s)).join("");
        return `<tr>
          <td>
            <b>${esc(c.theme)}</b>
            <div class="leader-pills">${pills}</div>
            ${aStocks ? `<div class="stock-pills">${aStocks}</div>` : ""}
          </td>
          <td class="num ${cls(c.avg_pct)}"><b>${c.avg_pct > 0 ? "+" : ""}${Number(c.avg_pct).toFixed(2)}%</b></td>
          <td><span class="tag ${dirCls}">${dirTxt}</span></td>
        </tr>`;
      }).join("") : '<tr><td colspan="3" class="empty">暂无美股龙头报价</td></tr>'}</tbody></table>`
    : `<table><thead><tr><th>A 股题材轴 / 政策要点 / 对应 A 股</th><th class="num">计数</th><th>极性</th></tr></thead>
      <tbody>${cnCats.length ? cnCats.map((c) => {
        const dirCls = c.direction === "bullish" ? "grade-a" : (c.direction === "danger" || c.direction === "bearish") ? "grade-reject" : "grade-c";
        const dirTxt = c.direction === "bullish" ? `利好 +${c.score}` : c.direction === "danger" ? `雷区 ${c.score}` : c.direction === "bearish" ? `承压 ${c.score}` : `中性`;
        const topH = (c.headlines && c.headlines[0]) ? c.headlines[0].title : "";
        const aStocks = (c.mapped_stocks || []).slice(0, 4).map((s) => stockPillHtml(s)).join("");
        return `<tr>
          <td>
            <b>${esc(c.theme)}</b>
            <div class="code">${esc(topH.slice(0, 42))}</div>
            ${aStocks ? `<div class="stock-pills">${aStocks}</div>` : ""}
          </td>
          <td class="num">好${c.pos_n}/空${c.neg_n}</td>
          <td><span class="tag ${dirCls}">${dirTxt}</span></td>
        </tr>`;
      }).join("") : '<tr><td colspan="3" class="empty">暂未命中特定题材快讯</td></tr>'}</tbody></table>`;

  catEl.innerHTML = `<div class="card-head-row">
      <h4>板块催化与对应 A 股</h4>
      <div class="sub-seg">
        <button type="button" class="sub-seg-btn ${isUs ? "active" : ""}" data-cat="us">美股映射 (${usCats.length})</button>
        <button type="button" class="sub-seg-btn ${!isUs ? "active" : ""}" data-cat="cn">国内政策 (${cnCats.length})</button>
      </div>
    </div>${bodyHtml}`;
}

function renderMacroAlertBanner(ovs, ns) {
  const el = $("#macroAlertBanner");
  if (!el) return;
  const mp = (ns && ns.macro_policy_alert) || {};
  const mr = (ovs && ovs.market_reaction) || {};
  const showPolicy = Boolean(mp.active);
  const showSurge = Boolean(mr.sudden_surge);
  if (!showPolicy && !showSurge) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  const heads = (mp.headlines || []).slice(0, 3).map((h) => `<span class="tag grade-a">${esc(h.t || "")} ${esc(h.title || "")}</span>`).join(" ");
  const parts = [];
  if (showPolicy) parts.push(`<b>${esc(mp.note || "重磅稳市政策触发")}</b>`);
  if (showSurge) parts.push(`<span>⚡ ${esc(mr.note || "宽基/期货突拉共振")}</span>`);
  el.innerHTML = `<div class="banner-head">${parts.join(" ｜ ")}</div>${heads ? `<div>${heads}</div>` : ""}`;
  el.classList.remove("hidden");
}

function renderLhbBox(lhb) {
  const el = $("#lhbBox");
  if (!el) return;
  if (!lhb || !lhb.available) {
    el.innerHTML = `<h4>昨日龙虎榜 · 游资主攻方向</h4><div class="empty-card">暂无龙虎榜留档数据</div>`;
    return;
  }
  const ms = lhb.market_sentiment || {};
  const yp = lhb.youzi_premium || {};
  const focus = lhb.focus || {};
  const topThemes = (focus.top_themes || []).slice(0, 5).map((t) =>
    `<span class="leader-pill ${t.net_yi > 0 ? "positive" : ""}">${esc(t.theme)} (${fmtInt(t.n_stocks)}只 / ${t.net_yi > 0 ? "+" : ""}${fmtNum(t.net_yi, 2)}亿)</span>`
  ).join("");
  const topStocks = (focus.top_stocks || []).slice(0, 8).map((s) =>
    stockPillHtml({
      ts_code: s.ts_code,
      name: s.name,
      high_days: `${s.net_amt_yi > 0 ? "+" : ""}${fmtNum(s.net_amt_yi, 2)}亿`,
      reason: s.tag || "",
    }, s.net_amt_yi > 0 ? "positive" : "")
  ).join("");
  const ypTxt = yp.available && has(yp.win_rate)
    ? ` · 昨日游资今日溢价胜率 <b>${fmtPct(yp.win_rate, 1)}</b> (${fmtInt(yp.n)}只)`
    : "";
  el.innerHTML = `<h4>昨日龙虎榜 · 游资主攻 (${esc(lhb.latest_day || "—")})</h4>
    <div class="status"><span class="tag ${Number(ms.total_net_yi || 0) >= 0 ? "grade-a" : "grade-reject"}">净买 ${Number(ms.total_net_yi || 0) >= 0 ? "+" : ""}${fmtNum(ms.total_net_yi, 2)}亿</span><span>${esc(ms.note || "")}${ypTxt}</span></div>
    ${topThemes ? `<div class="leader-pills" style="margin-bottom:8px">${topThemes}</div>` : ""}
    ${topStocks ? `<div class="stock-pills">${topStocks}</div>` : '<div class="empty-card">暂无主攻个股明细</div>'}`;
}

function renderMacroIntel(d) {
  if (!d) return;
  state.lastMacroData = d;
  const ovs = d.overseas || {};
  const ns = d.news || {};
  const lhb = d.lhb || {};
  if (ovs.available) renderOverseasBox(ovs);
  renderMacroAlertBanner(ovs, ns);
  renderLhbBox(lhb);

  const meta = $("#macroMeta");
  if (meta) {
    meta.textContent = `外盘 ${ovs.n_indices || 0} 指数 · 快讯 ${ns.n_total || 0} 条（利好 ${ns.n_positive || 0} / 雷区 ${ns.n_danger || 0} / 直接命中个股 ${(ns.direct_stocks || []).length} 只）· 龙虎榜 ${lhb.n_profiles || 0} 只 · 点击任意 A 股胶囊看盘 · 更新 ${fmtTs(d.generated_at)}`;
  }

  // 左：美股映射 / 国内政策题材切换卡片（左右等高更整洁）
  renderCatalystBox(d);

  // 右：7×24 高信噪比快讯与直接命中个股/雷区预警
  const newsEl = $("#newsBox");
  if (newsEl) {
    const watch = d.watch_alerts || [];
    const watchBanner = watch.length ? watch.map((w) =>
      `<div class="status"><span class="tag ${w.risk_level === "danger" ? "grade-reject" : "grade-a"}">${esc(w.scope)} · ${esc(w.name)} (${esc(w.ts_code)})</span><span>${esc((w.headlines && w.headlines[0] && w.headlines[0].title) || "")}</span></div>`
    ).join("") : "";

    const direct = (ns.direct_stocks || []).slice(0, 10);
    const directBar = direct.length ? `<div class="stock-pills" style="margin-bottom:10px">${direct.map((s) => {
      const ec = s.risk_level === "danger" ? "danger" : s.risk_level === "positive" ? "positive" : "";
      const tag = s.risk_level === "danger" ? `⚠ ${(s.danger_tags || []).join("/")}` : s.risk_level === "positive" ? "利好" : "公告";
      return stockPillHtml({ ts_code: s.ts_code, name: s.name || s.ts_code, high_days: tag, reason: (s.headlines && s.headlines[0]?.title) || "" }, ec);
    }).join("")}</div>` : "";

    const items = (ns.highlights || []).slice(0, 14);
    const listHtml = items.length ? `<div class="news-list">${items.map((it) => {
      const sentCls = it.sentiment === "danger" ? "grade-reject" : it.sentiment === "positive" ? "grade-a" : it.sentiment === "negative" ? "unfilled" : "grade-c";
      const sentLbl = it.sentiment === "danger" ? "⚠ 雷区" : it.sentiment === "positive" ? "利好" : it.sentiment === "negative" ? "偏空" : "快讯";
      const thTags = (it.themes || []).map((t) => `<span class="tag grade-b">${esc(t)}</span>`).join("");
      const directSt = (it.stocks || []).slice(0, 3).map((s) => stockPillHtml(s, it.sentiment === "danger" ? "danger" : "")).join("");
      const themeSt = !directSt ? (it.theme_stocks || []).slice(0, 3).map((s) => stockPillHtml(s)).join("") : "";
      return `<div class="news-item ${it.sentiment === "danger" ? "danger" : ""}">
        <div class="news-top">
          <span class="tag ${sentCls}">${sentLbl}</span>
          <span class="code">${esc(fmtTs(it.published_at))} · ${esc(it.source || "")}</span>
          ${thTags}
        </div>
        <div class="news-title">${esc(it.title)}</div>
        ${(directSt || themeSt) ? `<div class="stock-pills">${directSt || themeSt}</div>` : ""}
      </div>`;
    }).join("")}</div>` : `<div class="empty-card">暂无实时快讯</div>`;
    newsEl.innerHTML = `<h4>7×24 快讯 · 命中个股与雷区预警</h4>${watchBanner}${directBar}${listHtml}`;
  }
}

function openMacroStockChart(code, name) {
  const host = $("#macroIntel");
  if (!host) return;
  closeInlineChart();
  const old = $("#macroChartBox");
  if (old && old.dataset.code === code) { old.remove(); return; }
  if (old) old.remove();

  const box = document.createElement("div");
  box.id = "macroChartBox";
  box.className = "macro-chart-box";
  box.dataset.code = code;
  box.innerHTML = `<div class="chart-inline">
    <div class="chart-inline-head">
      <b>${esc(name || code)} (${esc(code)}) · 映射个股实时看盘</b>
      <div class="chart-tabs">
        <button type="button" class="chart-tab active" data-tab="kline">日K线 + 买卖点</button>
        <button type="button" class="chart-tab" data-tab="intraday">1分钟分时 + VWAP</button>
      </div>
      <button type="button" class="chart-close">收起 ✕</button>
    </div>
    <div class="chart-inline-meta muted">单股实时快查中…</div>
    <div class="chart-stage">
      <canvas class="chart-canvas"></canvas>
      <div class="chart-tip hidden"></div>
    </div>
  </div>`;
  host.appendChild(box);
  inlineChart.code = code;
  inlineChart.name = name || code;
  inlineChart.tab = "kline";
  inlineChart.detailEl = box;

  box.querySelector(".chart-close").addEventListener("click", () => closeInlineChart());
  box.querySelectorAll(".chart-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      inlineChart.tab = btn.dataset.tab;
      box.querySelectorAll(".chart-tab").forEach((b) => b.classList.toggle("active", b === btn));
      drawInlineChart();
    });
  });
  fetchAndDrawChart();
  inlineChart.timer = setInterval(() => {
    if (!document.hidden) fetchAndDrawChart();
  }, state.marketTrading ? 12000 : 60000);
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ------------------------------------------- HiDPI Interactive Chart */
const inlineChart = { code: null, name: null, tab: "kline", data: null, timer: null, rowEl: null, detailEl: null, tableId: null };
const UP_COLOR = "#ff453a";
const DOWN_COLOR = "#30d158";
const VWAP_COLOR = "#ffd60a";

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

function _intradaySlot(t) {
  if (!t || typeof t !== "string") return 0;
  const parts = t.slice(0, 5).split(":");
  if (parts.length < 2) return 0;
  const mins = parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
  if (Number.isNaN(mins)) return 0;
  if (mins <= 9 * 60 + 30) return 0;
  if (mins <= 11 * 60 + 30) return mins - (9 * 60 + 30);          // 0 .. 120
  if (mins < 13 * 60) return 120;                                 // 午休定格在 11:30/13:00
  if (mins <= 15 * 60) return 120 + (mins - 13 * 60);             // 120 .. 240
  return 240;
}

function _boardLimitPct(code) {
  const c = String(code || "").padStart(6, "0");
  if (/^(43|83|87|92)/.test(c)) return 0.30;
  if (/^(300|301|302|688|689)/.test(c)) return 0.20;
  return 0.10;
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

  const padL = 56, padR = 56, padT = 16, volH = 46, gap = 10, padB = 22;
  const priceH = H - padT - volH - gap - padB;
  const plotW = W - padL - padR;

  let lo = Infinity, hi = -Infinity, maxVol = 0;
  for (const p of pts) {
    if (p.price < lo) lo = p.price;
    if (p.price > hi) hi = p.price;
    if (p.avg_price) { lo = Math.min(lo, p.avg_price); hi = Math.max(hi, p.avg_price); }
    const vbar = p.volume || p.amount || 0;
    if (vbar > maxVol) maxVol = vbar;
  }
  const limPct = _boardLimitPct(intra.code);
  if (pc) {
    const rawDev = Math.max(Math.abs(hi - pc), Math.abs(lo - pc), pc * 0.015);
    const limDev = pc * limPct;
    // 贴近涨跌停时严格锁定 ±10%/±20%/±30%（同花顺/东财标准），绝不虚扩到 11%
    let dev = rawDev >= limDev * 0.92 ? Math.max(limDev, rawDev) : Math.min(limDev, rawDev * 1.05);
    lo = pc - dev;
    hi = pc + dev;
  } else if (hi <= lo) { lo -= 0.1; hi += 0.1; }

  const yOf = (v) => padT + priceH * (1 - (v - lo) / (hi - lo));
  // 固定全天 240 分钟横轴（09:30–11:30 / 13:00–15:00），上午收盘占左半边
  const xOfSlot = (slot) => padL + plotW * (slot / 240);
  const xOfPt = (p) => xOfSlot(_intradaySlot(p.t));

  // 水平网格 & 涨跌幅标尺（5 档对称：+10% / +5% / 0% / -5% / -10%）
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.fillStyle = "#8b98ab"; ctx.font = "11px monospace";
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4;
    const y = yOf(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillText(v.toFixed(2), 6, y + 4);
    if (pc) {
      const rp = (v - pc) / pc * 100;
      ctx.fillStyle = rp > 0.05 ? UP_COLOR : rp < -0.05 ? DOWN_COLOR : "#8b98ab";
      ctx.fillText(`${rp > 0.05 ? "+" : ""}${rp.toFixed(1)}%`, W - padR + 5, y + 4);
      ctx.fillStyle = "#8b98ab";
    }
  }

  // 垂直时间参考线（10:30 / 11:30午休中线 / 14:00）
  [60, 120, 180].forEach((slot) => {
    const vx = xOfSlot(slot);
    ctx.strokeStyle = slot === 120 ? "rgba(255,255,255,0.14)" : "rgba(255,255,255,0.05)";
    ctx.setLineDash(slot === 120 ? [3, 3] : []);
    ctx.beginPath(); ctx.moveTo(vx, padT); ctx.lineTo(vx, H - padB); ctx.stroke();
    ctx.setLineDash([]);
  });

  // 昨收虚线
  if (pc) {
    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(padL, yOf(pc)); ctx.lineTo(W - padR, yOf(pc)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // 成交量/额柱（底部）
  const volTop = padT + priceH + gap;
  if (maxVol > 0) {
    const bw = Math.max(1.2, Math.min(3.2, plotW / 240 * 0.72));
    pts.forEach((p, i) => {
      const vbar = p.volume || p.amount || 0;
      if (!vbar) return;
      const prevPx = i > 0 ? pts[i - 1].price : (pc || p.price);
      ctx.fillStyle = p.price >= prevPx ? UP_COLOR : DOWN_COLOR;
      ctx.globalAlpha = 0.55;
      const vh = Math.max(1, (vbar / maxVol) * volH);
      ctx.fillRect(xOfPt(p) - bw / 2, volTop + volH - vh, bw, vh);
      ctx.globalAlpha = 1;
    });
  }

  // 均价线 VWAP（黄色）
  if (intra.has_vwap) {
    ctx.strokeStyle = VWAP_COLOR; ctx.lineWidth = 1.4;
    ctx.beginPath();
    let started = false;
    pts.forEach((p) => {
      if (!p.avg_price) return;
      const x = xOfPt(p), y = yOf(p.avg_price);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  // 分时主线（红涨绿跌）
  const lineCol = pc ? (last.price >= pc ? UP_COLOR : DOWN_COLOR) : "#60a5fa";
  ctx.strokeStyle = lineCol; ctx.lineWidth = 1.8;
  ctx.beginPath();
  pts.forEach((p, i) => {
    const x = xOfPt(p), y = yOf(p.price);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // 标准 A 股时间轴（09:30 / 10:30 / 11:30|13:00 / 14:00 / 15:00）
  ctx.fillStyle = "#8b98ab"; ctx.font = "10.5px monospace";
  [
    [0, "09:30"],
    [60, "10:30"],
    [120, "11:30/13:00"],
    [180, "14:00"],
    [240, "15:00"],
  ].forEach(([slot, lbl]) => {
    const x = xOfSlot(slot);
    const tx = slot === 0 ? padL : (slot === 240 ? W - padR - 32 : x - lbl.length * 3);
    ctx.fillText(lbl, tx, H - 6);
  });

  // 十字光标（匹配最近时间点）
  if (hoverX !== null && hoverX >= padL && hoverX <= W - padR) {
    let bestIdx = 0, bestDist = Infinity;
    for (let i = 0; i < pts.length; i++) {
      const dist = Math.abs(xOfPt(pts[i]) - hoverX);
      if (dist < bestDist) { bestDist = dist; bestIdx = i; }
    }
    const p = pts[bestIdx];
    const cx = xOfPt(p);
    ctx.strokeStyle = "rgba(243,207,136,0.45)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(cx, padT); ctx.lineTo(cx, H - padB); ctx.stroke();
    ctx.setLineDash([]);
    const rp = pc ? (p.price - pc) / pc * 100 : null;
    tip.innerHTML = `<b>${esc(p.t)}</b> · 价 <b class="${rp !== null ? cls(rp) : ""}">${fmtNum(p.price)}</b>` +
      (rp !== null ? ` (${rp > 0 ? "+" : ""}${rp.toFixed(2)}%)` : "") +
      (p.avg_price ? ` · 均价 ${fmtNum(p.avg_price)}` : "") +
      (p.volume ? ` · 量 ${fmtInt(Math.round(p.volume))}手` : "") +
      (p.amount ? ` · 额 ${(p.amount / 1e4).toFixed(0)}万` : "");
    tip.classList.remove("hidden");
  }
}

function renderFillCategories(byCat) {
  const el = $("#fillCatWrap");
  if (!el) return;
  const keys = ["queue_head", "reopen_reseal", "reopen_broken", "sweep_shadow"];
  const rows = keys.map((k) => {
    const it = (byCat && byCat[k]) || {};
    if (!it.label && !has(it.n_orders)) return "";
    return `<tr>
      <td><b>${esc(it.label || k)}</b></td>
      <td class="num">${fmtInt(it.n_orders || 0)}</td>
      <td class="num">${fmtInt(it.n_closed || 0)}</td>
      <td class="num">${fmtPct(it.win_rate, 1)}</td>
      <td class="num ${cls(it.mean_ret)}">${fmtPct(it.mean_ret)}</td>
    </tr>`;
  }).filter(Boolean).join("");
  if (!rows) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = `<h3 class="sub-head">四分类成交账本（队首 / 回封 / 烂板 / 扫板影子）</h3>
    <div class="table-wrap"><table>
      <thead><tr><th>成交分类</th><th class="num">委托数</th><th class="num">已结算</th><th class="num">胜率</th><th class="num">均值净收益</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

function renderCancelCounterfactual(cf) {
  const el = $("#cancelCfWrap");
  if (!el) return;
  if (!cf || !cf.available) {
    el.innerHTML = "";
    return;
  }
  const byKind = (cf.by_kind || []).map((k) => `<tr>
    <td><b>${esc(k.kind_cn || k.kind)}</b></td>
    <td class="num">${fmtInt(k.n_total)} / ${fmtInt(k.n_verified)}</td>
    <td class="num up">${fmtInt(k.n_saved)}</td>
    <td class="num down">${fmtInt(k.n_shaken_out)}</td>
    <td class="num ${cls(k.avg_next_ret)}">${fmtPct(k.avg_next_ret)}</td>
    <td class="num">${has(k.threshold_adj) ? `${k.threshold_adj > 0 ? "+" : ""}${(k.threshold_adj * 100).toFixed(1)}%` : "—"}</td>
    <td class="muted">${esc(k.suggestion || "")}</td>
  </tr>`).join("");
  const cases = (cf.cases || []).slice(0, 12).map((c) => `<tr>
    <td>${esc(fmtDay(c.day))}</td>
    <td><b>${esc(c.name || "")}</b> <span class="code">${esc(c.ts_code)}</span></td>
    <td>${esc(c.cancel_kind_cn || c.cancel_kind)}</td>
    <td>${esc(fmtTs(c.cancel_ts))}</td>
    <td class="num ${cls(c.next_ret)}">${fmtPct(c.next_ret)}</td>
    <td><span class="tag ${c.verdict?.includes("躲坑") ? "grade-a" : c.verdict?.includes("洗飞") ? "grade-reject" : "grade-c"}">${esc(c.verdict || "")}</span></td>
  </tr>`).join("");
  el.innerHTML = `<h3 class="sub-head">主动撤单反事实审计（避险躲坑 vs 主力假撤单洗飞）</h3>
    <p class="muted" style="margin:-4px 0 10px">${esc(cf.note || "")}</p>
    ${byKind ? `<div class="table-wrap" style="margin-bottom:10px"><table>
      <thead><tr><th>撤单类型</th><th class="num">总数/已验</th><th class="num">躲坑</th><th class="num">洗飞</th><th class="num">次日均值</th><th class="num">阈值自调</th><th>进化建议</th></tr></thead>
      <tbody>${byKind}</tbody>
    </table></div>` : ""}
    ${cases ? `<div class="table-wrap"><table>
      <thead><tr><th>日期</th><th>股票</th><th>撤单类型</th><th>撤单时刻</th><th class="num">次日收益</th><th>审计判定</th></tr></thead>
      <tbody>${cases}</tbody>
    </table></div>` : ""}`;
}

/* ------------------------------------------- Evolution & Backtest (lazy) */
function renderEvolution(d) {
  if (!d || !d.available) {
    $("#evoPathsBody").innerHTML = `<tr><td colspan="7" class="empty">进化引擎数据暂不可用</td></tr>`;
    return;
  }
  const rhoVal = has(d.params?.rho_evaporation) ? Number(d.params.rho_evaporation) : 0.03;
  const evoMetaEl = $("#evoMeta");
  if (evoMetaEl) {
    evoMetaEl.textContent = `严格因果更新 · 每日自然挥发 ${(rhoVal * 100).toFixed(0)}% · 跌停核按钮 2× 重罚`;
  }
  const sc = d.shadow_compare || {};
  const chips = [
    ["已追踪路径", fmtInt(d.n_paths)],
    ["因果结算样本", fmtInt(sc.n_trades)],
    ["静态基准均值", fmtPct(sc.baseline_avg_ret), cls(sc.baseline_avg_ret)],
    ["信息素影子均值", fmtPct(sc.shadow_avg_ret), cls(sc.shadow_avg_ret)],
    ["影子对照差异", has(sc.delta_pp) ? `${sc.delta_pp > 0 ? "+" : ""}${sc.delta_pp.toFixed(2)}pp` : "—", cls(sc.delta_pp)],
    ["每日挥发率 ρ", `${(rhoVal * 100).toFixed(0)}%`],
  ];
  $("#evoSummary").innerHTML = chips.map(([k, v, c]) =>
    `<div class="summary-chip"><span>${esc(k)}</span><b class="${c || ""}">${esc(v)}</b></div>`
  ).join("");

  $("#evoAdvice").innerHTML = (d.advice || []).map((a) => `<div class="advice-item">${esc(a)}</div>`).join("");
  if (state.lastLiveData && state.lastLiveData.by_fill_category) {
    renderFillCategories(state.lastLiveData.by_fill_category);
  }
  renderCancelCounterfactual(d.cancel_counterfactual);

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

  const facHead = $("#evoFactorsHead");
  if (facHead && has(d.n_factor_dims)) {
    facHead.textContent = `${fmtInt(d.n_factor_dims)} 因子独立信息素画像（竞价 / 上板 / 换手 / 封板质量 / 股性基因 / 60日突破 / 题材锚定 / 板块梯队 / 高标存亡 / 昨日溢价 / 龙虎榜 / 外盘催化 · 小样本收缩）`;
  }
  const facEl = $("#evoFactorsBody");
  if (facEl) {
    const frows = (d.factor_pheromones && d.factor_pheromones.rows) || [];
    if (!frows.length) {
      facEl.innerHTML = `<tr><td colspan="8" class="empty">暂无多因子结算样本</td></tr>`;
    } else {
      facEl.innerHTML = frows.map((r) => {
        const tCls = r.trend === "强化" ? "grade-a" : r.trend === "衰减" ? "grade-reject" : "grade-b";
        return `<tr>
          <td><b>${esc(r.dim_label)}</b></td>
          <td>${esc(r.bucket)}</td>
          <td class="num">${fmtNum(r.pheromone, 2)}</td>
          <td class="num"><b>×${fmtNum(r.eff_mult, 2)}</b></td>
          <td class="num">${fmtInt(r.samples)}</td>
          <td class="num">${fmtPct(r.win_rate, 1)}</td>
          <td class="num ${cls(r.avg_ret)}">${fmtPct(r.avg_ret)}</td>
          <td><span class="tag ${tCls}">${esc(r.trend)}</span></td>
        </tr>`;
      }).join("");
    }
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

  renderMissedReflection(d.missed_reflection);
  renderB2Challenger(d.b2_challenger);
}

function renderB2Challenger(b2) {
  const metaEl = $("#b2Meta");
  const sumEl = $("#b2Summary");
  const noteEl = $("#b2GateNote");
  const pathsBody = $("#b2PathsBody");
  const tradesBody = $("#b2TradesBody");
  if (!pathsBody || !tradesBody) return;
  if (!b2 || !b2.available) {
    if (metaEl) metaEl.textContent = (b2 && b2.reason) || "B2 影子挑战者暂不可用";
    pathsBody.innerHTML = `<tr><td colspan="7" class="empty">暂无数据</td></tr>`;
    tradesBody.innerHTML = `<tr><td colspan="7" class="empty">暂无数据</td></tr>`;
    return;
  }
  const s = b2.summary || {};
  if (metaEl) metaEl.textContent = b2.note || `区间 ${b2.period || "—"}`;
  if (sumEl) {
    const chips = [
      ["影子样本", fmtInt(s.n_trades)],
      ["整体胜率", fmtPct(s.win_rate, 1)],
      ["断板净均", fmtPct(s.avg_net_ret), cls(s.avg_net_ret)],
      ["核按钮数", fmtInt(s.nuclear_n), s.nuclear_n > 0 ? "down" : ""],
      ["达标解锁路径", `${fmtInt(s.n_graduated)} 条`, s.n_graduated > 0 ? "up" : ""],
      ["潜力观察路径", `${fmtInt(s.n_promising)} 条`],
    ];
    sumEl.innerHTML = chips.map(([k, v, c]) =>
      `<div class="summary-chip"><span>${esc(k)}</span><b class="${c || ""}">${esc(v)}</b></div>`
    ).join("");
  }
  if (noteEl) {
    noteEl.innerHTML = `<div class="advice-item">硬性毕业晋升门禁：样本 ≥ 10 笔 · 胜率 ≥ 55% · 断板净均 ≥ +3.0% · 核按钮率 ≤ 10% · 信息素 τ ≥ 1.10 → 达标才解锁 0.3× 卫星试错仓，否则留在影子账本继续学习。</div>`;
  }
  const ps = b2.paths || [];
  pathsBody.innerHTML = ps.length ? ps.map((p) => {
    const stCls = p.status === "graduated" ? "grade-a" : p.status === "promising" ? "grade-b" : "grade-c";
    return `<tr>
      <td><b>${esc(p.path)}</b></td>
      <td class="num"><b>${fmtNum(p.pheromone, 2)}</b></td>
      <td class="num">${fmtInt(p.samples)}</td>
      <td class="num">${fmtPct(p.win_rate, 1)}</td>
      <td class="num ${cls(p.avg_net_ret)}">${fmtPct(p.avg_net_ret)}</td>
      <td class="num ${p.nuclear_n > 0 ? "down" : "muted"}">${fmtInt(p.nuclear_n)}</td>
      <td><span class="tag ${stCls}">${esc(p.status_cn)}</span> <span class="code">${esc(p.gap_to_graduate || "")}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" class="empty">暂无纯换手子路径样本</td></tr>`;

  const ts = b2.recent_trades || [];
  tradesBody.innerHTML = ts.length ? ts.map((t) => `<tr class="chart-row" data-code="${esc(t.ts_code)}" data-name="${esc(t.name)}" title="点击展开该股 K 线与分时">
      <td>${esc(t.entry_day)}</td>
      <td><div class="name">${esc(t.name)}</div><div class="code">${esc(t.ts_code)}</div></td>
      <td>${esc(t.theme)} <span class="tag grade-b">${esc(t.alt_anchor)}</span>${t.ice_crossing ? ` <span class="tag grade-a">冰点穿越</span>` : ""}</td>
      <td class="num">${has(t.turnover) ? `${fmtNum(t.turnover, 1)}%` : "—"}</td>
      <td class="num up">${fmtInt(t.max_streak)} 板</td>
      <td>${esc(t.exit_day)}</td>
      <td class="num ${cls(t.net_ret)}"><b>${fmtPct(t.net_ret)}</b>${t.exit_blocked ? ` <span class="code down">跌停顺延</span>` : ""}</td>
    </tr>`).join("") : `<tr><td colspan="7" class="empty">暂无影子结算记录</td></tr>`;
}

function renderMissedReflection(m) {
  const metaEl = $("#missMeta");
  const sumEl = $("#missSummary");
  const heatEl = $("#missHeatList");
  const propBody = $("#missPropBody");
  const winBody = $("#missWinBody");
  if (!propBody || !winBody) return;
  if (!m || !m.available) {
    if (metaEl) metaEl.textContent = (m && m.reason) || "反事实反思暂不可用";
    propBody.innerHTML = `<tr><td colspan="8" class="empty">暂无候选因子验证数据</td></tr>`;
    winBody.innerHTML = `<tr><td colspan="8" class="empty">暂无漏网强势股数据</td></tr>`;
    return;
  }
  if (metaEl) metaEl.textContent = m.note || `扫描区间 ${m.period || "—"}`;
  const rb = m.reason_breakdown || {};
  const topReason = Object.values(rb)[0];
  const heatEntries = Object.values(m.theme_leader_heat || {});
  const chips = [
    ["扫描区间", `${fmtInt(m.lookback_days)} 交易日`],
    ["全市场二板", fmtInt(m.n_two_board_total)],
    ["漏网强势股", fmtInt(m.n_missed_winners)],
    ["其中可买入", fmtInt(m.n_buyable_winners)],
    ["主因分布", topReason ? `${topReason.label} (${topReason.count})` : "—"],
    ["热度反哺题材", `${fmtInt(heatEntries.length)} 个`],
  ];
  if (sumEl) {
    sumEl.innerHTML = chips.map(([k, v]) =>
      `<div class="summary-chip"><span>${esc(k)}</span><b>${esc(v)}</b></div>`
    ).join("");
  }
  if (heatEl) {
    const items = [];
    const autoL = m.theme_auto_learned;
    if (autoL && autoL.n > 0) {
      const sampleTxt = (autoL.top || []).slice(0, 6).map((x) => `${x.tag}→${x.name}`).join("、");
      items.push(`<div class="advice-item">题材自学习（同票共现自动归轴，无需手改配置）：已自动掌握 <b>${fmtInt(autoL.n)}</b> 个新兴标签（如 ${esc(sampleTxt)}）</div>`);
    }
    if (heatEntries.length) {
      const heatRhoPct = Math.round(Number(m.rho_evaporation ?? 0.03) * 100);
      heatEntries.slice(0, 5).forEach((h) => {
        items.push(`<div class="advice-item">主线热度反哺 · <b>${esc(h.theme)}</b>（${esc((h.leaders || []).join("、"))}）→ 催化分 <b>+${fmtNum(h.catalyst_bonus, 2)}</b> · 仓位微调 <b>+${fmtNum(h.heat_bonus, 2)}</b>（次日按 ${heatRhoPct}% 自然挥发）</div>`);
      });
    } else if (!items.length) {
      items.push(`<div class="advice-item">近期暂未检测到需要反哺的漏网主线题材热度。</div>`);
    }
    heatEl.innerHTML = items.join("");
  }

  const props = m.factor_proposals || [];
  propBody.innerHTML = props.length ? props.map((p) => {
    const vCls = p.verdict_code === "promote" ? "grade-a"
      : p.verdict_code === "watch" ? "grade-b"
      : p.verdict_code === "reject" ? "grade-reject" : "grade-c";
    return `<tr>
      <td><b>${esc(p.title)}</b></td>
      <td class="muted">${esc(p.desc)}</td>
      <td class="num">${fmtInt(p.n_triggered)}</td>
      <td class="num up">${fmtInt(p.n_winners_caught)}</td>
      <td class="num down">${fmtInt(p.n_losers_dragged)}</td>
      <td class="num">${fmtPct(p.win_rate, 1)}</td>
      <td class="num ${cls(p.avg_break_ret)}">${fmtPct(p.avg_break_ret)}</td>
      <td><span class="tag ${vCls}">${esc(p.verdict)}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="8" class="empty">暂无候选因子验证</td></tr>`;

  const wins = m.missed_winners || [];
  winBody.innerHTML = wins.length ? wins.map((w) => {
    const rCls = w.miss_reason === "oneword_unbuyable" ? "grade-c"
      : w.miss_reason === "theme_unmapped" ? "unfilled" : "grade-b";
    return `<tr class="chart-row" data-code="${esc(w.ts_code)}" data-name="${esc(w.name)}" title="点击展开该股 K 线与分时">
      <td>${esc(w.day)}</td>
      <td><div class="name">${esc(w.name)}</div><div class="code">${esc(w.ts_code)}</div></td>
      <td>${esc(w.theme)}${w.ice_crossing ? ` <span class="tag grade-a">冰点穿越</span>` : ""}</td>
      <td class="num">${has(w.turnover) ? `${fmtNum(w.turnover, 1)}%` : "—"}</td>
      <td class="num ${cls(w.open_gap)}">${has(w.open_gap) ? `${w.open_gap > 0 ? "+" : ""}${fmtNum(w.open_gap, 1)}%` : "—"}</td>
      <td class="num up"><b>${fmtInt(w.max_streak)} 板</b></td>
      <td class="num up"><b>+${(Number(w.peak_ret_5d || 0) * 100).toFixed(1)}%</b></td>
      <td><span class="tag ${rCls}">${esc(w.miss_reason_cn)}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="8" class="empty">近期无未命中的强势二板</td></tr>`;
}

function renderBacktest(g) {
  const sim = g && g.sim;
  if (!sim || !sim.available) {
    $("#btMeta").textContent = "历史回放快照暂不可用";
    $("#btStats").innerHTML = `<tr><td colspan="6" class="empty">无历史回放数据</td></tr>`;
    return;
  }
  const period = sim.period || (sim.panel_last ? `截至 ${sim.panel_last}` : "—");
  const costMain = has(sim.cost_main) ? Number(sim.cost_main) : 0.003688;
  $("#btMeta").textContent = `区间 ${period} · ${sim.cost_label || "万0.84免五"} · 生成 ${sim.generated_at || "—"}`;

  const statsMap = sim.stats || {};
  const entries = Object.entries(statsMap)
    .filter(([, st]) => st && typeof st === "object")
    .sort(([a], [b]) => {
      const rank = (k) => k.includes("同源实盘") ? 0
        : k.includes("同源日线") ? 1
        : (k.includes("全市场宽口径") || k.includes("主口径")) ? 2
        : k.includes("毛收益") ? 3 : 4;
      return rank(a) - rank(b);
    });
  if (entries.length) {
    $("#btStats").innerHTML = entries.map(([name, st]) => `<tr>
      <td>${esc(name)}</td>
      <td class="num">${fmtInt(st.n)}</td>
      <td class="num">${fmtPct(st.win, 1)}</td>
      <td class="num ${cls(st.mean_net)}">${fmtPct(st.mean_net)}</td>
      <td class="num ${cls(st.median_net)}">${fmtPct(st.median_net)}</td>
      <td class="num ${cls(st.cum_10pct)}">${fmtPct(st.cum_10pct)}</td>
    </tr>`).join("");
  } else {
    $("#btStats").innerHTML = `<tr><td colspan="6" class="empty">无回放统计项</td></tr>`;
  }

  const latest = (sim.latest || []).slice(0, 20);
  $("#btLatest").innerHTML = latest.length ? latest.map((x) => {
    const dt = x.signal_date || x.entry_date || "—";
    const hold = has(x.hold) ? x.hold : x.hold_days;
    const netRet = has(x.net_return) ? Number(x.net_return) : (has(x.a) ? Number(x.a) - costMain : null);
    const themeLabel = x.theme || x.industry || (x.has_rabbit ? "同日有兔" : "同日无兔");
    return `<tr>
      <td>${esc(dt)}</td>
      <td><div class="name">${esc(x.name || "—")}</div><div class="code">${esc(x.ts_code || "")}</div></td>
      <td>${esc(themeLabel)}</td>
      <td class="num">${fmtInt(has(x.entry_streak) ? x.entry_streak : 2)}</td>
      <td class="num">${has(hold) ? `${fmtInt(hold)}天` : "—"}</td>
      <td class="num">${fmtInt(x.max_streak)}</td>
      <td class="num ${cls(netRet)}">${fmtPct(netRet)}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" class="empty">无近期信号</td></tr>`;

  $("#btCaveats").innerHTML = (sim.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("");
}

function renderExitAlternatives(d) {
  if (!d || !d.available) {
    $("#exitAltMeta").textContent = (d && d.reason) || "卖法对照暂不可用";
    $("#exitAltBody").innerHTML = `<tr><td colspan="6" class="empty">暂无对照样本</td></tr>`;
    return;
  }
  const s = d.summary || {};
  const curM = s.current ? s.current.mean : s.avg_actual;
  const slM = s.stoploss ? s.stoploss.mean : s.avg_fixed;
  const deltaPp = has(s.delta_mean_pp) ? s.delta_mean_pp : s.delta_pp;
  const nTotal = has(d.n) ? d.n : (s.current?.n ?? s.n ?? 0);
  $("#exitAltMeta").textContent =
    `n=${fmtInt(nTotal)} · 现行均值 ${fmtPct(curM)} vs 固定止损均值 ${fmtPct(slM)} · 差异 ${has(deltaPp) ? `${deltaPp > 0 ? "+" : ""}${Number(deltaPp).toFixed(2)}pp` : "—"}`;

  const rows = d.trades || [];
  $("#exitAltBody").innerHTML = rows.length ? rows.map((t) => {
    const code = t.code || t.ts_code || "";
    const curR = has(t.current_return) ? t.current_return : t.actual_gross;
    const slR = has(t.stoploss_return) ? t.stoploss_return : t.fixed_gross;
    const diffVal = has(t.delta_pp) ? t.delta_pp : t.diff_pp;
    const diff = has(diffVal) ? `${diffVal > 0 ? "+" : ""}${Number(diffVal).toFixed(2)}pp` : "—";
    const ctx = t.context || {};
    const envParts = [];
    if (ctx.gate_blocked === true) envParts.push("闸门拦截");
    else if (ctx.gate_blocked === false) envParts.push("闸门通过");
    if (has(ctx.high_avg_ret)) envParts.push(`高标${ctx.high_avg_ret > 0 ? "+" : ""}${Number(ctx.high_avg_ret).toFixed(2)}%`);
    if (ctx.break_is_limit_down === true) envParts.push("断板跌停");
    else if (ctx.break_is_limit_down === false) envParts.push("断板未跌停");
    const envSummary = t.env_summary || envParts.join(" · ") || "—";
    const note = t.stoploss_note || t.fixed_reason || "—";
    const noteFull = t.stoploss_exit_date ? `${t.stoploss_exit_date} ${note}` : note;
    return `<tr>
      <td><div class="name">${esc(t.name || "—")}</div><div class="code">${esc(code)}</div></td>
      <td class="num ${cls(curR)}">${fmtPct(curR)}</td>
      <td class="num ${cls(slR)}">${fmtPct(slR)}</td>
      <td class="num ${cls(diffVal)}">${diff}</td>
      <td class="code">${esc(envSummary)}</td>
      <td class="muted">${esc(noteFull)}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">尚无已结算交易</td></tr>`;

  const dimLabel = {
    break_limit_down: "按断板是否跌停",
    gate: "按买入日情绪闸门",
    high_avg_ret: "按买入日高标均涨",
    high_limit_down_share: "按买入日高标跌停占比",
  };
  const grouped = {};
  if (Array.isArray(d.by_env)) {
    d.by_env.forEach((item) => {
      const k = dimLabel[item.dimension] || item.dimension || "环境分组";
      if (!grouped[k]) grouped[k] = [];
      grouped[k].push({
        bucket: item.bucket,
        n: item.n,
        avg_actual: item.current?.mean,
        avg_fixed: item.stoploss?.mean,
        delta_pp: item.delta_mean_pp,
      });
    });
  } else if (d.by_env && typeof d.by_env === "object") {
    Object.entries(d.by_env).forEach(([k, v]) => {
      if (Array.isArray(v)) grouped[k] = v;
    });
  }
  $("#exitEnvGroups").innerHTML = Object.entries(grouped).map(([title, items]) => `
    <div class="env-group">
      <h4>${esc(title)}</h4>
      <table><thead><tr><th>分组</th><th class="num">n</th><th class="num">现行</th><th class="num">止损</th><th class="num">差</th></tr></thead>
      <tbody>${items.map((x) => `<tr>
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

async function loadMacroIntel() {
  if (state.inFlight.macro) return;
  state.inFlight.macro = true;
  try {
    const d = await getJSON("/api/macro-intel", 25000);
    renderMacroIntel(d);
  } catch (e) {
    const meta = $("#macroMeta");
    if (meta) meta.textContent = `外盘/快讯加载失败：${e.message || e}`;
  } finally {
    state.inFlight.macro = false;
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
        if (brief.build) {
          if (state.serverBuild === null) state.serverBuild = brief.build;
          else if (state.serverBuild !== brief.build) { window.location.reload(); return; }
        }
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
  const missWinBody = $("#missWinBody");
  if (missWinBody) missWinBody.addEventListener("click", onRowClick);
  const b2TradesBody = $("#b2TradesBody");
  if (b2TradesBody) b2TradesBody.addEventListener("click", onRowClick);
  const macroEl = $("#macroIntel");
  if (macroEl) {
    macroEl.addEventListener("click", (e) => {
      const seg = e.target.closest("button.sub-seg-btn");
      if (seg && seg.dataset.cat) {
        state.catTab = seg.dataset.cat;
        if (state.lastMacroData) renderCatalystBox(state.lastMacroData);
        return;
      }
      const btn = e.target.closest("button.stock-pill");
      if (!btn || !btn.dataset.code) return;
      openMacroStockChart(btn.dataset.code, btn.dataset.name);
    });
  }

  $("#exitAltDetails").addEventListener("toggle", (e) => {
    if (e.target.open && !state.loaded.exitAlt) loadExitAlternatives();
  });

  $("#refreshBtn").addEventListener("click", async () => {
    const btn = $("#refreshBtn");
    btn.disabled = true; btn.textContent = "刷新中…";
    try {
      await fetch("/api/refresh", { method: "POST" });
      state.loaded = { evo: false, bt: false, exitAlt: false };
      await Promise.all([loadHealth(), loadLiveSim(), loadMacroIntel()]);
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
initAppleMotion();
// 首屏并发核心轻快接口，互不阻塞
loadHealth();
loadLiveSim();
loadMacroIntel();
connectSSE();
// 兜底轮询（仅前台激活时执行；SSE 正常时作为冗余保护）
setInterval(() => {
  if (document.hidden) return;
  loadHealth();
  loadLiveSim();
  loadMacroIntel();
}, 45000);
