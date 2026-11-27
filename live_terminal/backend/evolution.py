"""蚁群信息素（ACO）+ 非对称奖惩自进化引擎（影子对照，零虚构数据）。

核心设计原则
------------
1. **严格因果（无未来函数）**：第 T 日的路径权重与仓位系数，严格只由 T-1 及之前的真实结算/侦察结果更新而来。
2. **自然挥发（随市场风格切换自动进化）**：
       tau(t+1) = clip((1 - rho) * tau(t) + rho * tau_0 + alpha * mean_reward, tau_min, tau_max)
   当市场主线切换时，旧路径的信息素会按 RHO=0.03 自动向中性值 1.0 挥发衰减，不会死守过去模式。
3. **非对称奖惩（重罚跌停核按钮）**：
   - 盈利/连板晋级：正奖励（大涨 ≥9.5% 额外奖励 +0.03）
   - 常规亏损：1.2× 惩罚
   - 断板跌停封死顺延（流动性核按钮）：2.0× 重罚
   - 选对但排板未成（次日大涨 ≥5%）：计入「执行遗憾（Regret）」并释放侦察蚁正信息素
4. **安全护栏**：单日信息素最大变动钳位 ±0.15，建议仓位系数锁死在 [0.30, 1.25]，
   先以影子账本（Shadow Mode）与静态基准同屏对照，不直接篡改生产账本。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import paths

RHO = 0.03          # 每日自然挥发率（半衰期 ~23 交易日，过滤 1 周均值回归噪声、跟随月度风格切换）
ALPHA = 1.60        # 奖惩沉积系数
TAU_INIT = 1.00     # 中性初始信息素
TAU_MIN = 0.25      # 最低信息素下限（对应 0.30x 仓位）
TAU_MAX = 1.50      # 最高信息素上限（对应 1.25x 仓位）
MAX_DAILY_STEP = 0.15


SCOUT_WEIGHT = 0.50  # 市场自选侦察蚁（因无题材分类错过的换手二板）信息素沉积权重


def _anchor_bucket(record: dict[str, Any]) -> str:
    """板块无关的共振/锚定结构分桶（绝不绑定具体板块/行业名称，避免轮动误伤）。"""
    explicit = record.get("anchor_bucket")
    if explicit:
        return str(explicit).strip()
    rabbits = record.get("guitu_rabbits")
    if isinstance(rabbits, list) and len(rabbits) >= 2:
        return "多兔共振"
    if isinstance(rabbits, list) and len(rabbits) == 1:
        return "单兔锚定"
    if record.get("b2_anchor_type"):
        return str(record["b2_anchor_type"]).strip()
    if record.get("guitu_theme"):
        return "题材锚定"
    return "无题材锚定"


def _seal_bucket(first_seal: Any) -> str:
    s = str(first_seal or "").replace(":", "").strip()
    if len(s) == 5 and s.isdigit():
        s = "0" + s
    if len(s) >= 4 and s[:4].isdigit():
        hh, mm = int(s[:2]), int(s[2:4])
        m = hh * 60 + mm
        if m <= 9 * 60 + 40:
            return "早盘(≤09:40)"
        if m <= 11 * 60 + 30:
            return "上午"
        return "午后"
    return "时段未知"


def _turn_bucket(turn: Any) -> str:
    try:
        v = float(turn)
    except (TypeError, ValueError):
        return "换手未知"
    if v < 10.0:
        return "低换手(<10%)"
    if v <= 20.0:
        return "中换手(10-20%)"
    return "高换手(>20%)"


def _broken_bucket(broken: Any) -> str:
    try:
        b = int(broken)
    except (TypeError, ValueError):
        return "开板未知"
    if b == 0:
        return "0炸"
    if b <= 2:
        return "1-2炸"
    return "烂板(≥3炸)"


def make_path_key(record: dict[str, Any]) -> str:
    """将一笔委托或成交映射为**板块无关**的「共振结构 × 形态」寻优路径键。

    设计原则：具体板块由当日市场实时选出，进化引擎绝不对静态板块/行业名记仇或追高，
    只学习跨板块通用的结构性因子（共振锚定 × 首封时段 × 换手分桶 × 开板状态）。
    """
    if record.get("path_override"):
        return str(record["path_override"])
    ev = (record.get("guitu") or {}).get("evidence") or {}
    ab = _anchor_bucket(record)
    first_seal = (record.get("first_seal")
                  if record.get("first_seal") is not None
                  else record.get("first_seal_hhmmss"))
    if first_seal is None:
        first_seal = ev.get("first_seal")
    sb = _seal_bucket(first_seal)
    turn = record.get("turn_at_seal")
    if turn is None:
        turn = record.get("turnover")
    if turn is None:
        turn = ev.get("turnover")
    tb = _turn_bucket(turn)
    # 严格事前优先：挂单瞬间锁定的 broken_at_entry 优先于收盘后事后 broken_max，消除训练-推理特征错位
    broken = record.get("broken_at_entry")
    if broken is None:
        broken = record.get("broken_max")
    if broken is None:
        broken = record.get("broken")
    if broken is None:
        broken = ev.get("broken")
    bb = _broken_bucket(broken)
    return f"{ab} | {sb} | {tb} | {bb}"


def pheromone_to_size_mult(tau: float) -> float:
    """将信息素浓度 [0.25, 1.50] 平滑映射到安全仓位系数 [0.30, 1.25]。"""
    t = max(TAU_MIN, min(TAU_MAX, float(tau)))
    if t <= TAU_INIT:
        # [0.25, 1.00] -> [0.30, 1.00]
        frac = (t - TAU_MIN) / (TAU_INIT - TAU_MIN)
        return round(0.30 + 0.70 * frac, 3)
    # (1.00, 1.50] -> (1.00, 1.25]
    frac = (t - TAU_INIT) / (TAU_MAX - TAU_INIT)
    return round(1.00 + 0.25 * frac, 3)


def compute_reward(ret: float | None, *,
                   exit_blocked: bool = False,
                   filled: bool = True,
                   missed_next_ret: float | None = None) -> dict[str, Any]:
    """非对称奖惩函数（重罚跌停核按钮，记录排板错失遗憾）。"""
    if not filled:
        if missed_next_ret is not None and missed_next_ret >= 0.05:
            r = round(0.5 * float(missed_next_ret), 4)
            return {
                "reward": r,
                "kind": "scout_regret",
                "regret": round(float(missed_next_ret), 4),
                "label": f"侦察蚁正反馈（未成交但次日 +{missed_next_ret:.1%}，建议关注临封扫板）",
            }
        if missed_next_ret is not None and missed_next_ret <= -0.05:
            r = round(0.5 * float(missed_next_ret), 4)
            return {
                "reward": r,
                "kind": "scout_dodge",
                "regret": 0.0,
                "label": f"侦察蚁负反馈（未成交且次日 {missed_next_ret:.1%}，防虚强假板单边虚高）",
            }
        return {"reward": 0.0, "kind": "neutral", "regret": 0.0, "label": "未成交无显著反馈"}

    if ret is None:
        return {"reward": 0.0, "kind": "pending", "regret": 0.0, "label": "持仓未结算"}

    r = float(ret)
    if r > 0:
        bonus = 0.03 if r >= 0.095 else 0.0
        val = round(r + bonus, 4)
        return {
            "reward": val,
            "kind": "reward_big" if bonus > 0 else "reward",
            "regret": 0.0,
            "label": f"正向奖励 +{val:.2%}" + ("（晋级/大肉加成）" if bonus > 0 else ""),
        }
    if exit_blocked or r <= -0.15:
        val = round(2.0 * r, 4)
        return {
            "reward": val,
            "kind": "penalty_nuclear",
            "regret": 0.0,
            "label": f"核按钮重罚 {val:.2%}（2× 跌停流动性惩罚）",
        }
    val = round(1.2 * r, 4)
    return {
        "reward": val,
        "kind": "penalty",
        "regret": 0.0,
        "label": f"亏损惩罚 {val:.2%}（1.2×）",
    }


def _norm_day(d: Any) -> str:
    s = str(d or "").replace("-", "").replace("/", "")[:8]
    return s if len(s) == 8 and s.isdigit() else ""


def _as_list(records: Any) -> list[dict[str, Any]]:
    """兼容 sim_state.json 的 dict 形态与测试的 list 形态。"""
    if isinstance(records, dict):
        return [v for v in records.values() if isinstance(v, dict)]
    if isinstance(records, list):
        return [v for v in records if isinstance(v, dict)]
    return []


def enrich_unfilled_regret(orders: Any,
                           panel: Any = None) -> list[dict[str, Any]]:
    """为未成交委托补齐真实次日涨幅（侦察蚁执行遗憾，只读日线，绝不伪造）。"""
    order_list = [dict(o) for o in _as_list(orders)]
    need: list[dict[str, Any]] = [
        o for o in order_list
        if o.get("status") != "filled"
        and o.get("missed_next_ret") is None
        and o.get("entry_price")
    ]
    if not need:
        return order_list
    try:
        import polars as pl
        from backend import feeds
        p = panel if panel is not None else feeds.build_panel()
        if p is None or p.height == 0:
            return order_list
        codes = sorted({str(o.get("ts_code") or "").zfill(6) for o in need if o.get("ts_code")})
        if not codes:
            return order_list
        sub = (p.filter(pl.col("ts_code").is_in(codes))
               .select(["ts_code", pl.col("trade_date").cast(pl.Utf8).alias("d"), "close"])
               .sort(["ts_code", "d"]))
        by_code: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for r in sub.iter_rows(named=True):
            d8 = _norm_day(r["d"])
            px = r.get("close")
            if d8 and px and float(px) > 0:
                by_code[str(r["ts_code"]).zfill(6)].append((d8, float(px)))
        for o in need:
            c = str(o.get("ts_code") or "").zfill(6)
            d0 = _norm_day(o.get("day") or o.get("entry_date"))
            ep = float(o.get("entry_price") or 0.0)
            if not d0 or ep <= 0:
                continue
            nxt = next(((d1, px1) for d1, px1 in by_code.get(c, []) if d1 > d0), None)
            if nxt:
                o["missed_next_day"] = nxt[0]
                o["missed_next_ret"] = round(nxt[1] / ep - 1.0, 6)
    except Exception:  # noqa: BLE001
        return order_list
    return order_list


def evolve_from_records(orders: list[dict[str, Any]] | dict[str, dict[str, Any]],
                        trades: list[dict[str, Any]] | dict[str, dict[str, Any]],
                        *,
                        rho: float = RHO,
                        alpha: float = ALPHA,
                        all_days: list[str] | None = None,
                        market_scouts: list[dict[str, Any]] | None = None,
                        panel: Any = None) -> dict[str, Any]:
    """按交易日严格因果推进蚁群信息素更新（联合路径 + 10 因子独立信息素），并输出影子账本对照。

    Args:
        orders: 委托集合（支持 list 或 sim_state.json 的 dict 形态）
        trades: 已结算成交列表（含 entry_date, exit_date, gross_return/net_return, exit_blocked）
        all_days: 可选的交易日序列（用于在空仓日也执行自然挥发）
        market_scouts: 可选，因「无题材分类」错过但市场走出的可买二板侦察蚁样本（正负全量，严格按断板结算日因果汇入）
        panel: 可选日线面板；传入时自动补齐集合竞价/20日涨停基因/60日突破/高标存亡/昨日涨停溢价/同板块梯队等 10 维事前因子
    """
    from backend import position_sizing as _ps
    raw_orders = _as_list(orders)
    order_list = [o for o in raw_orders if not _ps.is_invalid_record(o)]
    trade_list = _ps.clean_trades(_as_list(trades), raw_orders)

    order_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for o in order_list:
        d = _norm_day(o.get("day") or o.get("entry_date"))
        c = str(o.get("ts_code") or "").zfill(6)
        if d and c:
            order_by_key[(d, c)] = o

    # 一次性从面板批量预计算 10 维事前因子上下文（严格事前，零未来函数）
    factor_pairs: set[tuple[str, str]] = set()
    for t in trade_list:
        ed = _norm_day(t.get("entry_date"))
        c = str(t.get("ts_code") or "").zfill(6)
        if ed and c:
            factor_pairs.add((ed, c))
    for sc in (market_scouts or []):
        ed = _norm_day(sc.get("entry_day") or sc.get("day"))
        c = str(sc.get("ts_code") or "").zfill(6)
        if ed and c:
            factor_pairs.add((ed, c))
    panel_ctx_map = _precompute_panel_factor_ctx(factor_pairs, panel=panel) if (panel is not None and factor_pairs) else {}

    # 按结算日聚合反馈（确保第 T 日结算的结果只影响 T+1 及之后的权重）
    events_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trade_list:
        ed = _norm_day(t.get("entry_date"))
        xd = _norm_day(t.get("exit_date")) or ed
        c = str(t.get("ts_code") or "").zfill(6)
        if not ed or not c:
            continue
        merged = {**order_by_key.get((ed, c), {}), **t}
        path_key = make_path_key(merged)
        ret = t.get("net_return")
        if ret is None:
            ret = t.get("r_net")
        if ret is None:
            ret = t.get("gross_return")
        if ret is None:
            continue
        rw = compute_reward(float(ret), exit_blocked=bool(t.get("exit_blocked")), filled=True)
        facs = extract_factors(merged, panel_ctx=panel_ctx_map.get((ed, c)))
        events_by_day[xd].append({
            "day": xd,
            "entry_day": ed,
            "ts_code": c,
            "name": str(merged.get("name") or ""),
            "path": path_key,
            "ret": float(ret),
            "filled": True,
            "source": "live",
            "factors": facs,
            **rw,
        })

    # 侦察蚁 1：未成交单若带 missed_next_ret 字段也纳入信息素与遗憾统计
    # 归属到次日（missed_next_day）以保证严格因果、无未来函数
    for o in order_list:
        if o.get("status") == "filled":
            continue
        d = _norm_day(o.get("day") or o.get("entry_date"))
        c = str(o.get("ts_code") or "").zfill(6)
        mnr = o.get("missed_next_ret")
        if d and c and mnr is not None:
            rw = compute_reward(None, filled=False, missed_next_ret=float(mnr))
            if rw["reward"] != 0.0:
                evt_day = _norm_day(o.get("missed_next_day")) or d
                events_by_day[evt_day].append({
                    "day": evt_day,
                    "entry_day": d,
                    "ts_code": c,
                    "name": str(o.get("name") or ""),
                    "path": make_path_key(o),
                    "ret": float(mnr),
                    "filled": False,
                    "source": "unfilled_regret",
                    "factors": extract_factors(o, panel_ctx=panel_ctx_map.get((d, c))),
                    **rw,
                })

    # 侦察蚁 2：因无题材分类错过但市场选出的可买换手二板（正负样本全量纳入，按断板结算日严格因果）
    for sc in (market_scouts or []):
        ed = _norm_day(sc.get("entry_day") or sc.get("day"))
        xd = _norm_day(sc.get("exit_day"))
        c = str(sc.get("ts_code") or "").zfill(6)
        ret = sc.get("net_ret")
        if not ed or not xd or not c or ret is None:
            continue
        base_rw = compute_reward(float(ret), exit_blocked=bool(sc.get("exit_blocked")), filled=True)
        w_rw = round(float(base_rw["reward"]) * SCOUT_WEIGHT, 4)
        pk = str(sc.get("path") or make_path_key(sc))
        facs = extract_factors(sc, panel_ctx=panel_ctx_map.get((ed, c)))
        events_by_day[xd].append({
            "day": xd,
            "entry_day": ed,
            "ts_code": c,
            "name": str(sc.get("name") or ""),
            "path": pk,
            "ret": float(ret),
            "filled": False,
            "source": "unthemed_market_scout",
            "factors": facs,
            "reward": w_rw,
            "kind": f"scout_{base_rw['kind']}",
            "regret": max(0.0, float(ret)),
            "label": f"无题材市场自选侦察（{SCOUT_WEIGHT:.1f}×）：{base_rw['label']}",
        })

    tau: dict[str, float] = {}
    stats: dict[str, dict[str, Any]] = {}
    feedback_log: list[dict[str, Any]] = []

    # 按日推进记录每日结算后的 tau 快照（含空仓日自然挥发）
    evt_days = sorted(events_by_day.keys())
    timeline = list(evt_days)
    if evt_days and all_days:
        first_d = evt_days[0]
        extra = [_norm_day(x) for x in all_days]
        timeline = sorted(set(evt_days) | {x for x in extra if x and x >= first_d})

    tau_history: list[tuple[str, dict[str, float]]] = []
    for day in timeline:
        # 1) 全路径自然挥发（向中性值 TAU_INIT 衰减）
        for pk in list(tau.keys()):
            tau[pk] = round((1.0 - rho) * tau[pk] + rho * TAU_INIT, 4)

        # 2) 按路径聚合当日奖惩并沉积信息素
        day_evts = events_by_day.get(day) or []
        by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ev in day_evts:
            by_path[ev["path"]].append(ev)

        for pk, evs in by_path.items():
            before = tau.get(pk, TAU_INIT)
            mean_r = sum(e["reward"] for e in evs) / len(evs)
            step = max(-MAX_DAILY_STEP, min(MAX_DAILY_STEP, alpha * mean_r))
            after = round(max(TAU_MIN, min(TAU_MAX, before + step)), 4)
            tau[pk] = after

            st = stats.setdefault(pk, {"samples": 0, "wins": 0, "sum_reward": 0.0,
                                       "nuclear_n": 0, "regret_n": 0, "scout_n": 0,
                                       "scout_wins": 0, "last_day": day})
            for e in evs:
                if e["filled"]:
                    st["samples"] += 1
                    if e["ret"] > 0:
                        st["wins"] += 1
                    if e["kind"] == "penalty_nuclear":
                        st["nuclear_n"] += 1
                elif e.get("source") == "unthemed_market_scout":
                    st["scout_n"] += 1
                    if e["ret"] > 0:
                        st["scout_wins"] += 1
                    if "nuclear" in str(e.get("kind") or ""):
                        st["nuclear_n"] += 1
                else:
                    st["regret_n"] += 1
                st["sum_reward"] += e["reward"]
                st["last_day"] = day
                feedback_log.append({
                    "day": day,
                    "entry_day": e["entry_day"],
                    "ts_code": e["ts_code"],
                    "name": e["name"],
                    "path": pk,
                    "ret": round(e["ret"], 4),
                    "reward": e["reward"],
                    "kind": e["kind"],
                    "label": e["label"],
                    "tau_before": round(before, 3),
                    "tau_after": round(after, 3),
                    "size_mult_after": pheromone_to_size_mult(after),
                })
        tau_history.append((day, dict(tau)))

    # 计算影子账本对照（严格使用 entry_day 之前的历史信息素）
    def _tau_before(entry_day: str, pk: str) -> float:
        got = TAU_INIT
        for d, snap in tau_history:
            if d < entry_day and pk in snap:
                got = snap[pk]
            elif d >= entry_day:
                break
        return got

    base_rets: list[float] = []
    shadow_rets: list[float] = []
    for day in sorted(events_by_day.keys()):
        for e in events_by_day[day]:
            if not e["filled"]:
                continue
            mult = pheromone_to_size_mult(_tau_before(e["entry_day"], e["path"]))
            base_rets.append(e["ret"])
            shadow_rets.append(e["ret"] * mult)

    base_avg = round(sum(base_rets) / len(base_rets), 4) if base_rets else None
    shadow_avg = round(sum(shadow_rets) / len(shadow_rets), 4) if shadow_rets else None
    delta_pp = (round((shadow_avg - base_avg) * 100.0, 2)
                if (base_avg is not None and shadow_avg is not None) else None)

    path_rows: list[dict[str, Any]] = []
    for pk, t_val in tau.items():
        st = stats.get(pk, {})
        n = int(st.get("samples") or 0)
        wins = int(st.get("wins") or 0)
        sn = int(st.get("scout_n") or 0)
        swins = int(st.get("scout_wins") or 0)
        rn = int(st.get("regret_n") or 0)
        eff_n = n if n > 0 else sn
        eff_wins = wins if n > 0 else swins
        avg_rw = round(st["sum_reward"] / max(1, n + sn + rn), 4)
        trend = "强化" if t_val > 1.05 else ("衰减" if t_val < 0.95 else "中性")
        src_tag = ("mixed" if (n > 0 and sn > 0) else
                   "market_scout" if (n == 0 and sn > 0) else "live")
        path_rows.append({
            "path": pk,
            "pheromone": round(t_val, 3),
            "size_mult": pheromone_to_size_mult(t_val),
            "samples": eff_n,
            "live_samples": n,
            "scout_n": sn,
            "win_rate": round(eff_wins / eff_n, 3) if eff_n > 0 else None,
            "avg_reward": avg_rw,
            "nuclear_n": int(st.get("nuclear_n") or 0),
            "regret_n": rn,
            "source": src_tag,
            "trend": trend,
            "last_day": st.get("last_day") or "",
        })
    path_rows.sort(key=lambda x: (-x["pheromone"], -x["samples"]))

    # 生成可执行进化建议
    advice: list[str] = []
    if not path_rows:
        advice.append("当前尚无已结算样本，所有路径保持中性信息素（1.00 / 1.00× 仓位），待真实成交后自动进化。")
    else:
        top = path_rows[0]
        bottom = path_rows[-1]
        if top["pheromone"] > 1.05:
            advice.append(f"优势路径：「{top['path']}」信息素 {top['pheromone']:.2f} → 建议仓位 ×{top['size_mult']:.2f}。")
        if bottom["pheromone"] < 0.90:
            advice.append(f"惩罚路径：「{bottom['path']}」信息素降至 {bottom['pheromone']:.2f}"
                          + (f"（含 {bottom['nuclear_n']} 次核按钮重罚）" if bottom["nuclear_n"] else "")
                          + f" → 建议降仓至 ×{bottom['size_mult']:.2f}。")
        if delta_pp is not None:
            direction = "改善" if delta_pp >= 0 else "落后"
            advice.append(f"影子对照（严格样本外因果）：信息素加权较静态基准均值 {direction} {delta_pp:+.2f}pp（n={len(base_rets)}）。")

    factor_evo = evolve_factors(events_by_day, timeline, rho=rho, alpha=alpha)

    return {
        "available": True,
        "mode": "causal_feedback",
        "params": {
            "rho_evaporation": rho,
            "alpha_deposit": alpha,
            "tau_range": [TAU_MIN, TAU_MAX],
            "size_mult_range": [0.30, 1.25],
            "max_daily_step": MAX_DAILY_STEP,
            "factor_prior_m": FACTOR_PRIOR_M,
        },
        "n_paths": len(path_rows),
        "n_trades": len(base_rets),
        "n_factor_dims": len(FACTOR_META),
        "paths": path_rows,
        "factor_pheromones": factor_evo,
        "feedback_log": list(reversed(feedback_log[-40:])),
        "shadow_compare": {
            "n_trades": len(base_rets),
            "baseline_avg_ret": base_avg,
            "shadow_avg_ret": shadow_avg,
            "delta_pp": delta_pp,
        },
        "advice": advice,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def causal_multipliers_before(day: str,
                              orders: Any,
                              trades: Any,
                              *,
                              all_days: list[str] | None = None) -> dict[str, dict[str, float]]:
    """严格因果计算「day 之前」各路径的信息素与建议仓位系数（供 live_sim.plan_orders 实时接入）。"""
    d_cut = _norm_day(day)
    if not d_cut:
        return {}
    past_trades = [
        t for t in _as_list(trades)
        if (_norm_day(t.get("exit_date")) or _norm_day(t.get("entry_date"))) < d_cut
    ]
    past_orders = [
        o for o in _as_list(orders)
        if _norm_day(o.get("day") or o.get("entry_date")) < d_cut
    ]
    past_days = [x for x in (all_days or []) if _norm_day(x) < d_cut] if all_days else None
    if not past_trades and not any(o.get("missed_next_ret") is not None for o in past_orders):
        return {}
    res = evolve_from_records(past_orders, past_trades, all_days=past_days)
    return {
        p["path"]: {"tau": float(p["pheromone"]), "size_mult": float(p["size_mult"])}
        for p in (res.get("paths") or [])
    }


# ---------------------------------------------------------------------------
# 踏空反事实反思引擎（2026-11-26：策略未命中但市场走出来的强势股 → 归因 + 因子验证 + 因果反哺）
# ---------------------------------------------------------------------------

MISS_REASON_CN: dict[str, str] = {
    "oneword_unbuyable": "本身一字二板（Q037物理买不进）",
    "no_oneword_rabbit": "纯换手二板（同题材无一字兔锚点）",
    "theme_unmapped": "题材标签盲区（未映射到题材轴）",
    "gate_blocked": "被个股情绪闸门拦截（逆势穿越龙）",
    "filtered_or_other": "聚焦上限/评分过滤或盘中炸板",
}


def _load_name_map() -> dict[str, str]:
    """只读加载 A 股 6 位代码→简称映射（复用 news_feed 本地缓存，绝不发外网阻塞）。"""
    try:
        from backend import news_feed
        c2n, _ = news_feed._stock_name_maps()
        if c2n:
            return c2n
    except Exception:  # noqa: BLE001
        pass
    return {}


def _load_theme_map_for_day(day8: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """读取某日题材池并映射到题材轴，返回 (code->raw_tags, code->all_mapped_themes)。

    保留每只票的**全部**命中板块（多标签展开，不丢弃第 2/3 个板块），
    避免类似「闽发铝业同时属新能源/算力/机器人」只保留第一个板块而误判无同题材兔子。
    """
    try:
        from backend import live_sim, theme_match
        raw_tags = live_sim._theme_tags(day8)
        if not raw_tags:
            return {}, {}
        code_axes: dict[str, list[str]] = {}
        items = [{"code": c, "name": "", "tags": tlist, "industry": None, "streak": 2}
                 for c, tlist in raw_tags.items()]
        grouped = theme_match.group_by_theme(items)
        for g in grouped.get("groups") or []:
            if g.get("level") in ("axis", "tag"):
                th = str(g.get("theme") or "")
                for c in g.get("codes") or []:
                    c6 = str(c).zfill(6)
                    bag = code_axes.setdefault(c6, [])
                    if th and th not in bag:
                        bag.append(th)
        return {str(c).zfill(6): list(v) for c, v in raw_tags.items()}, code_axes
    except Exception:  # noqa: BLE001
        return {}, {}


def analyze_missed_winners(
    orders: Any,
    *,
    panel: Any = None,
    lookback_days: int = 20,
    min_max_streak: int = 4,
    min_peak_ret: float = 0.20,
    as_of_day: str | None = None,
) -> dict[str, Any]:
    """反事实扫描：找出近期市场走出来的二板牛股中「策略没命中」的标的，归因并检验候选因子。

    设计原则（防过拟合 / 防幸存者偏差）：
    1. **严格因果**：只使用 `as_of_day` 之前已收盘的真实日线；某只牛股只有在 T+3 走出来后才被确认。
    2. **正负样本同时验证**：不只看漏网牛股，而是把同窗口内**全部未命中且可买入的二板**都拉出来，
       对每条候选放宽因子同时计算「能多抓几只牛股 vs 会多带进来几只亏钱票 → 真实断板均值净收益」，
       只有正期望且胜率达标才建议升格，否则明确标注「宁可踏空·维持过滤」。
    3. **因果反哺现行因子**：对近期走出大牛股的题材计算衰减热度 `theme_leader_heat`，
       在次日合法龟兔信号出现时，以有界正权重反哺买点催化分与仓位系数（不破坏核心入场纪律）。
    """
    try:
        import polars as pl
        from backend import feature_engine, feeds, ice_gate
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"依赖加载失败: {exc}", "missed_winners": [], "factor_proposals": [], "theme_leader_heat": {}}

    p = panel if panel is not None else feeds.build_panel()
    if p is None or p.height == 0:
        return {"available": False, "reason": "日线面板不可用", "missed_winners": [], "factor_proposals": [], "theme_leader_heat": {}}

    cut8 = _norm_day(as_of_day) if as_of_day else ""
    all_days = sorted({str(d) for d in p["trade_date"].unique().to_list()})
    if cut8:
        all_days = [d for d in all_days if _norm_day(d) < cut8]
    if len(all_days) < 6:
        return {"available": False, "reason": "有效历史交易日不足 6 天", "missed_winners": [], "factor_proposals": [], "theme_leader_heat": {}}

    # 窗口：扫描倒数 lookback_days+2 到 倒数第 2 天（留出至少 1 天验证后续走势；再往前多取 10 天算连板）
    win_end_idx = len(all_days) - 2
    win_start_idx = max(0, win_end_idx - lookback_days + 1)
    scan_days = set(all_days[win_start_idx:win_end_idx + 1])
    slice_start = all_days[max(0, win_start_idx - 10)]
    day_pos = {d: i for i, d in enumerate(all_days)}
    last_day = all_days[-1]

    # 已命中委托集合 (day8, code6)
    ordered_keys: set[tuple[str, str]] = set()
    for o in _as_list(orders):
        d8 = _norm_day(o.get("day") or o.get("entry_date"))
        c6 = str(o.get("ts_code") or "").zfill(6)
        if d8 and c6:
            ordered_keys.add((d8, c6))

    # 向量化计算窗口内涨停/连板/一字形态
    sel = ["ts_code", "trade_date", "open", "high", "low", "close"]
    for extra_col in ("turnover", "amount"):
        if extra_col in p.columns:
            sel.append(extra_col)
    sub = (
        p.filter(pl.col("trade_date").cast(pl.Utf8) >= slice_start)
        .filter(pl.col("trade_date").cast(pl.Utf8) <= last_day)
        .select(sel)
        .sort(["ts_code", "trade_date"])
        .with_columns(pl.col("close").shift(1).over("ts_code").alias("prev_close"))
    )
    codes = sub["ts_code"].unique().to_list()
    lim_map = {str(c): float(feature_engine.limit_pct(str(c))) * 100.0 for c in codes}
    sub = sub.with_columns(
        pl.col("ts_code").cast(pl.Utf8).replace_strict(lim_map, return_dtype=pl.Float64).alias("lim_pct")
    ).with_columns([
        ((pl.col("close") / pl.col("prev_close") - 1.0) * 100.0).alias("pct"),
        ((pl.col("open") / pl.col("prev_close") - 1.0) * 100.0).alias("open_gap"),
    ]).with_columns([
        (pl.col("pct") >= pl.col("lim_pct") - 0.3).fill_null(False).alias("is_zt"),
        ((pl.col("high") <= pl.col("low") * 1.002) & (pl.col("open") >= pl.col("close") * 0.998)).fill_null(False).alias("is_oneword_bar"),
    ]).with_columns([
        pl.col("is_zt").cast(pl.Int32).alias("_zt_i"),
        (~pl.col("is_zt")).cast(pl.Int32).cum_sum().over("ts_code").alias("_off_run"),
    ]).with_columns(
        pl.when(pl.col("is_zt"))
        .then(pl.col("_zt_i").cum_sum().over(["ts_code", "_off_run"]))
        .otherwise(0).cast(pl.Int32).alias("streak")
    )

    # 仅取出窗口内出现过 streak==2 的股票做后续路径统计（极快，通常 <250 只股）
    two_codes = (
        sub.filter((pl.col("streak") == 2) & pl.col("trade_date").cast(pl.Utf8).is_in(list(scan_days)))
        ["ts_code"].unique().to_list()
    )
    if not two_codes:
        return {
            "available": True,
            "lookback_days": len(scan_days),
            "period": f"{min(scan_days)}..{max(scan_days)}",
            "n_two_board_total": 0,
            "n_missed_winners": 0,
            "missed_winners": [],
            "reason_breakdown": {},
            "factor_proposals": [],
            "theme_leader_heat": {},
            "rho_evaporation": RHO,
            "unmapped_themes": [],
        }

    # 市场逐日下跌占比（用于判断是否冰点/普跌穿越）
    m_all = ice_gate.metrics_all(p)
    down_share_by_day = {
        str(r["day"]): float(r["down_share"])
        for r in m_all.filter(pl.col("day").is_in(list(scan_days))).to_dicts()
        if r.get("down_share") is not None
    }

    name_map = _load_name_map()
    theme_cache: dict[str, tuple[dict[str, list[str]], dict[str, str]]] = {}

    def _get_day_themes(d8: str) -> tuple[dict[str, list[str]], dict[str, str]]:
        if d8 not in theme_cache:
            theme_cache[d8] = _load_theme_map_for_day(d8)
        return theme_cache[d8]

    # 记录每天哪些题材存在一字二板兔子（直接用真实日线 is_oneword_bar & streak==2 近似复核）
    sub_two = sub.filter(pl.col("ts_code").is_in(two_codes))
    oneword_2b_by_day: dict[str, set[str]] = defaultdict(set)
    for r in sub_two.filter((pl.col("streak") == 2) & pl.col("is_oneword_bar")).select(["trade_date", "ts_code"]).iter_rows(named=True):
        oneword_2b_by_day[_norm_day(r["trade_date"])].add(str(r["ts_code"]).zfill(6))

    all_missed_2b: list[dict[str, Any]] = []
    n_two_total = 0

    for (code_raw,), g in sub_two.group_by("ts_code", maintain_order=True):
        code = str(code_raw).zfill(6)
        nm = name_map.get(code, "")
        if feature_engine.is_st(nm):
            continue
        rows = g.to_dicts()
        n_rows = len(rows)
        for idx, r in enumerate(rows):
            d_str = str(r["trade_date"])
            if d_str not in scan_days or int(r.get("streak") or 0) != 2:
                continue
            n_two_total += 1
            d8 = _norm_day(d_str)
            if (d8, code) in ordered_keys:
                continue  # 策略已命中，不属于漏网

            c0 = float(r.get("close") or 0.0)
            if c0 <= 0:
                continue
            # 计算后续真实走势（严格在已收盘的 rows[idx+1:] 内）
            fut = rows[idx + 1:idx + 6]
            if not fut:
                continue
            # 最高连板高度
            max_streak = 2
            for fr in fut:
                if bool(fr.get("is_zt")):
                    max_streak = max(max_streak, int(fr.get("streak") or 0))
                else:
                    break
            # 5日最大涨幅（盘中最高价相对二板收盘）
            peak_hi = max(float(fr.get("high") or fr.get("close") or c0) for fr in fut)
            peak_ret = round(peak_hi / c0 - 1.0, 4)
            # 断板日真实收益（与 B1 卖点口径对齐：首个未涨停日收盘，若仍连板取末日收盘）
            break_row = next((fr for fr in fut if not bool(fr.get("is_zt"))), fut[-1])
            break_ret = round(float(break_row.get("close") or c0) / c0 - 1.0, 4)
            # 确认走出来的日期（达到>=4板或+20%那天，严格用于因果截止）
            confirm_day = _norm_day(fut[-1].get("trade_date"))
            for fr in fut:
                hi_r = float(fr.get("high") or fr.get("close") or c0) / c0 - 1.0
                if int(fr.get("streak") or 0) >= min_max_streak or hi_r >= min_peak_ret:
                    confirm_day = _norm_day(fr.get("trade_date"))
                    break

            turn_val = r.get("turnover")
            turn_f = round(float(turn_val), 2) if turn_val is not None and turn_val == turn_val else None
            gap_val = r.get("open_gap")
            gap_f = round(float(gap_val), 2) if gap_val is not None and gap_val == gap_val else None
            is_ow = bool(r.get("is_oneword_bar")) and (turn_f is None or turn_f < 5.0)

            raw_tags_map, axis_map = _get_day_themes(d8)
            theme_axes = axis_map.get(code) or []
            theme_axis = theme_axes[0] if theme_axes else None
            raw_tags = raw_tags_map.get(code) or []

            # 归因漏网原因（多标签全量核对：任一板块有同题材一字兔即算有兔）
            if is_ow:
                miss_reason = "oneword_unbuyable"
            elif raw_tags_map and not theme_axes:
                miss_reason = "theme_unmapped"
            else:
                my_set = set(theme_axes)
                rabbits_same_theme = [
                    rc for rc in oneword_2b_by_day.get(d8, set())
                    if rc != code and my_set and (my_set & set(axis_map.get(rc) or []))
                ]
                if not rabbits_same_theme:
                    miss_reason = "no_oneword_rabbit"
                else:
                    miss_reason = "filtered_or_other"

            ds = down_share_by_day.get(d_str)
            is_winner = bool(max_streak >= min_max_streak or peak_ret >= min_peak_ret)
            all_missed_2b.append({
                "day": d_str,
                "day8": d8,
                "confirm_day": confirm_day,
                "ts_code": code,
                "name": nm or code,
                "theme": theme_axis or (raw_tags[0] if raw_tags else "未归类"),
                "theme_mapped": bool(theme_axis),
                "raw_tags": raw_tags[:3],
                "turnover": turn_f,
                "open_gap": gap_f,
                "is_oneword": is_ow,
                "market_down_share": round(ds, 4) if ds is not None else None,
                "ice_crossing": bool(ds is not None and ds >= 0.75),
                "max_streak": max_streak,
                "peak_ret_5d": peak_ret,
                "break_ret": break_ret,
                "miss_reason": miss_reason,
                "miss_reason_cn": MISS_REASON_CN.get(miss_reason, miss_reason),
                "is_winner": is_winner,
            })

    winners = [x for x in all_missed_2b if x["is_winner"]]
    winners.sort(key=lambda x: (-x["max_streak"], -x["peak_ret_5d"], x["day"]))
    buyable_pool = [x for x in all_missed_2b if x["miss_reason"] != "oneword_unbuyable"]
    buyable_winners = [x for x in winners if x["miss_reason"] != "oneword_unbuyable"]

    # 漏网原因分布
    reason_counts: dict[str, int] = defaultdict(int)
    for w in winners:
        reason_counts[w["miss_reason"]] += 1
    reason_breakdown = {
        k: {"count": v, "label": MISS_REASON_CN.get(k, k)}
        for k, v in sorted(reason_counts.items(), key=lambda kv: -kv[1])
    }

    # --- 候选因子正负样本对撞检验（Precision / Recall，绝不盲目放宽！） ---
    def _eval_proposal(key: str, title: str, desc: str, predicate) -> dict[str, Any]:
        hits = [x for x in buyable_pool if predicate(x)]
        n_hit = len(hits)
        w_hit = sum(1 for x in hits if x["is_winner"])
        rets = [x["break_ret"] for x in hits]
        avg_r = round(sum(rets) / n_hit, 4) if n_hit else None
        wr = round(sum(1 for r in rets if r > 0) / n_hit, 4) if n_hit else None
        if n_hit >= 4 and avg_r is not None and avg_r >= 0.03 and wr is not None and wr >= 0.50:
            verdict = "建议升格试行"
            verdict_code = "promote"
        elif n_hit > 0 and avg_r is not None and avg_r > 0:
            verdict = "观察积累"
            verdict_code = "watch"
        elif n_hit == 0:
            verdict = "近期无触发"
            verdict_code = "none"
        else:
            verdict = "宁可踏空·维持过滤"
            verdict_code = "reject"
        return {
            "key": key,
            "title": title,
            "desc": desc,
            "n_triggered": n_hit,
            "n_winners_caught": w_hit,
            "n_losers_dragged": sum(1 for r in rets if r <= 0),
            "win_rate": wr,
            "avg_break_ret": avg_r,
            "verdict": verdict,
            "verdict_code": verdict_code,
        }

    factor_proposals = [
        _eval_proposal(
            "pure_turnover_dragon",
            "纯换手强龙头（无一字兔锚点）",
            "无同题材一字二板兔，但换手 ≤12% 且一进二高开 ≥3% 的换手二板",
            lambda x: (
                x["miss_reason"] == "no_oneword_rabbit"
                and x["turnover"] is not None and x["turnover"] <= 12.0
                and x["open_gap"] is not None and x["open_gap"] >= 3.0
            ),
        ),
        _eval_proposal(
            "ice_crossing_dragon",
            "普跌/冰点逆势穿越二板",
            "全市场下跌占比 ≥75% 当日仍能换手封住二板的逆势活口",
            lambda x: bool(x["ice_crossing"]),
        ),
        _eval_proposal(
            "weak_to_strong_gap",
            "弱转强高开缩量二板",
            "二板开盘溢价 ≥4% 且换手 ≤15%（高一致筹码锁定，不依赖一字兔）",
            lambda x: (
                x["open_gap"] is not None and x["open_gap"] >= 4.0
                and x["turnover"] is not None and x["turnover"] <= 15.0
            ),
        ),
        _eval_proposal(
            "theme_unmapped_gap",
            "题材轴盲区待补词",
            "有题材池原始标签但尚未归入题材轴的漏网二板（可安全补全词库）",
            lambda x: x["miss_reason"] == "theme_unmapped",
        ),
    ]

    # --- 因果反哺：主线龙头题材热度（自动衰减，供实盘买点催化与仓位微调使用） ---
    last_idx = day_pos.get(last_day, len(all_days) - 1)
    theme_heat_raw: dict[str, dict[str, Any]] = {}
    for w in buyable_winners:
        th = w["theme"]
        if not w["theme_mapped"] or not th or th == "未归类":
            continue
        w_idx = day_pos.get(w["day"], last_idx)
        age = max(0, last_idx - w_idx)
        decay = (1.0 - RHO) ** age
        gain_score = min(0.08, max(0.02, w["peak_ret_5d"] * 0.20)) * decay
        slot = theme_heat_raw.setdefault(th, {
            "theme": th, "heat_bonus": 0.0, "catalyst_bonus": 0.0,
            "n_leaders": 0, "leaders": [],
        })
        slot["heat_bonus"] += gain_score
        slot["n_leaders"] += 1
        if len(slot["leaders"]) < 3:
            slot["leaders"].append(f"{w['name']}({w['max_streak']}板/+{w['peak_ret_5d']*100:.0f}%)")

    theme_leader_heat: dict[str, dict[str, Any]] = {}
    for th, info in sorted(theme_heat_raw.items(), key=lambda kv: -kv[1]["heat_bonus"]):
        hb = round(min(0.15, info["heat_bonus"]), 3)
        cb = round(min(1.5, hb * 10.0), 2)
        theme_leader_heat[th] = {
            "theme": th,
            "heat_bonus": hb,           # 注入仓位系数微调 ∈ [0, +0.15]
            "catalyst_bonus": cb,       # 注入买点质量催化分 ∈ [0, +1.5]
            "n_leaders": info["n_leaders"],
            "leaders": info["leaders"],
        }

    # 待补全题材盲区清单 + 同票共现自动学到的新兴题材映射
    unmapped_themes = [
        {"day": w["day"], "ts_code": w["ts_code"], "name": w["name"],
         "raw_tags": w["raw_tags"], "max_streak": w["max_streak"], "peak_ret_5d": w["peak_ret_5d"]}
        for w in winners if w["miss_reason"] == "theme_unmapped"
    ][:10]
    learned_sample: list[dict[str, Any]] = []
    learned_n = 0
    try:
        from backend import theme_match
        lm = theme_match.learned_tag_mappings(before_day=cut8 or None)
        learned_n = len(lm)
        learned_sample = [
            {"tag": tag, **info}
            for tag, info in sorted(lm.items(), key=lambda kv: (-kv[1]["stocks"], -kv[1]["purity"]))[:12]
        ]
    except Exception:  # noqa: BLE001
        pass

    return {
        "available": True,
        "lookback_days": len(scan_days),
        "period": f"{min(scan_days)}..{max(scan_days)}",
        "n_two_board_total": n_two_total,
        "n_missed_buyable_pool": len(buyable_pool),
        "n_missed_winners": len(winners),
        "n_buyable_winners": len(buyable_winners),
        "missed_winners": winners[:25],
        "reason_breakdown": reason_breakdown,
        "factor_proposals": factor_proposals,
        "theme_leader_heat": theme_leader_heat,
        "rho_evaporation": RHO,
        "theme_auto_learned": {"n": learned_n, "top": learned_sample},
        "unmapped_themes": unmapped_themes,
        "note": (
            f"扫描近 {len(scan_days)} 个交易日全市场 {n_two_total} 只二板，"
            f"检出未命中强势股 {len(winners)} 只（可买入 {len(buyable_winners)} 只）；"
            "候选放宽因子均经全部可买二板正负样本对撞，避免幸存者偏差。"
        ),
    }


def causal_theme_leader_heat(day: str, orders: Any, *, panel: Any = None) -> dict[str, dict[str, Any]]:
    """严格因果获取 `day` 之前市场走出的漏网主线龙头题材热度（供 live_sim 实时因子反哺）。"""
    d8 = _norm_day(day)
    if not d8:
        return {}
    res = analyze_missed_winners(orders, panel=panel, lookback_days=15, as_of_day=d8)
    if not res.get("available"):
        return {}
    return res.get("theme_leader_heat") or {}


# ---------------------------------------------------------------------------
# B2 纯换手二板·影子挑战者（独立信息素学习 + 硬性毕业晋升门禁，不触碰 B1 主仓）
# ---------------------------------------------------------------------------

B2_GRADUATION_GATE: dict[str, float] = {
    "min_samples": 10,
    "min_win_rate": 0.55,
    "min_avg_net_ret": 0.03,
    "max_nuclear_rate": 0.10,
    "min_pheromone": 1.10,
}


def evaluate_b2_turnover_challenger(
    orders: Any,
    *,
    panel: Any = None,
    lookback_days: int = 25,
    as_of_day: str | None = None,
) -> dict[str, Any]:
    """B2 纯换手二板影子挑战者：专门追踪「无同题材一字二板兔」的纯换手二板，独立跑信息素学习与毕业晋升。

    核心聚焦四类广义锚点（直接回应「题材划分过细/跨产业链共振/高标或一字首板带动」）：
      1. 关联产业链有兔（上下游/共振大主线存在一字二板兔）
      2. 高标同题材带动（同题材存在 ≥3 板高标当日晋级涨停）
      3. 一字首板带动（同题材存在一字首板涨停）
      4. 独立换手龙头（纯换手，无上述广义锚点）
    """
    try:
        import polars as pl
        from backend import costs, feature_engine, feeds, ice_gate, theme_match
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"依赖加载失败: {exc}", "paths": [], "recent_trades": []}

    p = panel if panel is not None else feeds.build_panel()
    if p is None or p.height == 0:
        return {"available": False, "reason": "日线面板不可用", "paths": [], "recent_trades": []}

    cut8 = _norm_day(as_of_day) if as_of_day else ""
    all_days = sorted({str(d) for d in p["trade_date"].unique().to_list()})
    if cut8:
        all_days = [d for d in all_days if _norm_day(d) < cut8]
    if len(all_days) < 6:
        return {"available": False, "reason": "有效历史交易日不足 6 天", "paths": [], "recent_trades": []}

    win_end_idx = len(all_days) - 2
    win_start_idx = max(0, win_end_idx - lookback_days + 1)
    scan_days = set(all_days[win_start_idx:win_end_idx + 1])
    slice_start = all_days[max(0, win_start_idx - 10)]
    last_day = all_days[-1]
    rt_cost = float(costs.DEFAULT.round_trip_rate())

    ordered_keys: set[tuple[str, str]] = set()
    for o in _as_list(orders):
        d8 = _norm_day(o.get("day") or o.get("entry_date"))
        c6 = str(o.get("ts_code") or "").zfill(6)
        if d8 and c6:
            ordered_keys.add((d8, c6))

    sel = ["ts_code", "trade_date", "open", "high", "low", "close"]
    if "turnover" in p.columns:
        sel.append("turnover")
    sub = (
        p.filter(pl.col("trade_date").cast(pl.Utf8) >= slice_start)
        .filter(pl.col("trade_date").cast(pl.Utf8) <= last_day)
        .select(sel)
        .sort(["ts_code", "trade_date"])
        .with_columns(pl.col("close").shift(1).over("ts_code").alias("prev_close"))
    )
    codes = sub["ts_code"].unique().to_list()
    lim_map = {str(c): float(feature_engine.limit_pct(str(c))) * 100.0 for c in codes}
    sub = sub.with_columns(
        pl.col("ts_code").cast(pl.Utf8).replace_strict(lim_map, return_dtype=pl.Float64).alias("lim_pct")
    ).with_columns([
        ((pl.col("close") / pl.col("prev_close") - 1.0) * 100.0).alias("pct"),
        ((pl.col("open") / pl.col("prev_close") - 1.0) * 100.0).alias("open_gap"),
    ]).with_columns([
        (pl.col("pct") >= pl.col("lim_pct") - 0.3).fill_null(False).alias("is_zt"),
        (pl.col("pct") <= -(pl.col("lim_pct") - 0.3)).fill_null(False).alias("is_dt"),
        ((pl.col("high") <= pl.col("low") * 1.002) & (pl.col("open") >= pl.col("close") * 0.998)).fill_null(False).alias("is_oneword_bar"),
    ]).with_columns([
        pl.col("is_zt").cast(pl.Int32).alias("_zt_i"),
        (~pl.col("is_zt")).cast(pl.Int32).cum_sum().over("ts_code").alias("_off_run"),
    ]).with_columns(
        pl.when(pl.col("is_zt"))
        .then(pl.col("_zt_i").cum_sum().over(["ts_code", "_off_run"]))
        .otherwise(0).cast(pl.Int32).alias("streak")
    )

    # 当日市场各类广义锚点集合（一字二板 / 一字首板 / ≥3板晋级高标）
    scan_sub = sub.filter(pl.col("trade_date").cast(pl.Utf8).is_in(list(scan_days)))
    ow2_by_day: dict[str, set[str]] = defaultdict(set)
    ow1_by_day: dict[str, set[str]] = defaultdict(set)
    hi3_zt_by_day: dict[str, set[str]] = defaultdict(set)
    for r in scan_sub.filter(pl.col("is_zt")).select(["trade_date", "ts_code", "streak", "is_oneword_bar"]).iter_rows(named=True):
        d8 = _norm_day(r["trade_date"])
        c6 = str(r["ts_code"]).zfill(6)
        st_n = int(r["streak"] or 0)
        if st_n == 2 and bool(r["is_oneword_bar"]):
            ow2_by_day[d8].add(c6)
        elif st_n == 1 and bool(r["is_oneword_bar"]):
            ow1_by_day[d8].add(c6)
        elif st_n >= 3:
            hi3_zt_by_day[d8].add(c6)

    two_codes = scan_sub.filter(pl.col("streak") == 2)["ts_code"].unique().to_list()
    if not two_codes:
        return {"available": True, "period": f"{min(scan_days)}..{max(scan_days)}", "summary": {"n_trades": 0}, "paths": [], "recent_trades": [], "graduated_paths": []}

    m_all = ice_gate.metrics_all(p)
    down_share_by_day = {
        str(r["day"]): float(r["down_share"])
        for r in m_all.filter(pl.col("day").is_in(list(scan_days))).to_dicts()
        if r.get("down_share") is not None
    }
    name_map = _load_name_map()
    theme_cache: dict[str, tuple[dict[str, list[str]], dict[str, list[str]]]] = {}

    def _get_th(d8: str):
        if d8 not in theme_cache:
            theme_cache[d8] = _load_theme_map_for_day(d8)
        return theme_cache[d8]

    # 收集所有可买入的「无同题材一字二板兔」纯换手二板影子成交
    shadow_trades: list[dict[str, Any]] = []
    sub_two = sub.filter(pl.col("ts_code").is_in(two_codes))
    for (code_raw,), g in sub_two.group_by("ts_code", maintain_order=True):
        code = str(code_raw).zfill(6)
        nm = name_map.get(code, "")
        if feature_engine.is_st(nm):
            continue
        rows = g.to_dicts()
        for idx, r in enumerate(rows):
            d_str = str(r["trade_date"])
            if d_str not in scan_days or int(r.get("streak") or 0) != 2:
                continue
            d8 = _norm_day(d_str)
            if (d8, code) in ordered_keys:
                continue
            turn_val = r.get("turnover")
            turn_f = round(float(turn_val), 2) if turn_val is not None and turn_val == turn_val else None
            if bool(r.get("is_oneword_bar")) and (turn_f is None or turn_f < 5.0):
                continue  # 排除物理不可买的一字二板

            c0 = float(r.get("close") or 0.0)
            fut = rows[idx + 1:idx + 8]
            if c0 <= 0 or not fut:
                continue

            _, axis_map = _get_th(d8)
            my_axes = axis_map.get(code) or []
            my_set = set(my_axes)
            # 排除已有同题材一字二板兔（那属于 B1 主策略范畴）
            has_same_ow2 = any(
                rc != code and my_set and (my_set & set(axis_map.get(rc) or []))
                for rc in ow2_by_day.get(d8, set())
            )
            if has_same_ow2:
                continue

            # 判定广义锚点
            rel_set: set[str] = set()
            for th in my_axes:
                rel_set.update(theme_match.related_themes(th))
            has_chain_ow2 = any(
                rc != code and rel_set and (rel_set & set(axis_map.get(rc) or []))
                for rc in ow2_by_day.get(d8, set())
            )
            has_hi3_lead = any(
                rc != code and my_set and (my_set & set(axis_map.get(rc) or []))
                for rc in hi3_zt_by_day.get(d8, set())
            )
            has_ow1_lead = any(
                rc != code and my_set and (my_set & set(axis_map.get(rc) or []))
                for rc in ow1_by_day.get(d8, set())
            )
            if not my_axes:
                alt_anchor = "无题材·市场自选"
            elif has_chain_ow2:
                alt_anchor = "关联产业链有兔"
            elif has_hi3_lead:
                alt_anchor = "高标同题材带动"
            elif has_ow1_lead:
                alt_anchor = "一字首板带动"
            else:
                alt_anchor = "独立换手龙头"

            # 真实断板卖点结算（若断板日跌停封死则顺延至次日不跌停收盘，并标 exit_blocked=True）
            exit_row = None
            exit_blocked = False
            max_streak = 2
            broke = False
            for fr in fut:
                if not broke:
                    if bool(fr.get("is_zt")):
                        max_streak = max(max_streak, int(fr.get("streak") or 0))
                        continue
                    broke = True
                if not bool(fr.get("is_dt")):
                    exit_row = fr
                    break
                exit_blocked = True
            if exit_row is None:
                continue  # 仍在连板或跌停封死未出清，不伪造结算
            exit_px = float(exit_row.get("close") or 0.0)
            if exit_px <= 0:
                continue
            net_ret = round(exit_px / c0 - 1.0 - rt_cost, 4)

            gap_val = r.get("open_gap")
            gap_f = round(float(gap_val), 2) if gap_val is not None and gap_val == gap_val else None
            tb = "低换手(≤10%)" if (turn_f is not None and turn_f <= 10.0) else (
                "中换手(10-18%)" if (turn_f is not None and turn_f <= 18.0) else "高换手(>18%)"
            )
            gb = "弱转强高开" if (gap_f is not None and gap_f >= 3.0) else "平低开"
            ds = down_share_by_day.get(d_str)
            ice_x = bool(ds is not None and ds >= 0.75)
            path_key = f"{alt_anchor} | {tb} | {gb}" + (" | 冰点穿越" if ice_x else "")

            shadow_trades.append({
                "entry_day": d_str,
                "exit_day": str(exit_row.get("trade_date")),
                "ts_code": code,
                "name": nm or code,
                "theme": my_axes[0] if my_axes else "未归类",
                "alt_anchor": alt_anchor,
                "turnover": turn_f,
                "open_gap": gap_f,
                "ice_crossing": ice_x,
                "max_streak": max_streak,
                "net_ret": net_ret,
                "exit_blocked": exit_blocked,
                "path": path_key,
            })

    # 按结算日推进独立蚁群信息素学习
    events_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in shadow_trades:
        rw = compute_reward(t["net_ret"], exit_blocked=t["exit_blocked"], filled=True)
        events_by_day[t["exit_day"]].append({**t, **rw})

    tau: dict[str, float] = {}
    p_stats: dict[str, dict[str, Any]] = {}
    for d_ev in sorted(events_by_day.keys()):
        for pk in list(tau.keys()):
            tau[pk] = round((1.0 - RHO) * tau[pk] + RHO * TAU_INIT, 4)
        by_p: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ev in events_by_day[d_ev]:
            by_p[ev["path"]].append(ev)
        for pk, evs in by_p.items():
            before = tau.get(pk, TAU_INIT)
            mean_r = sum(e["reward"] for e in evs) / len(evs)
            step = max(-MAX_DAILY_STEP, min(MAX_DAILY_STEP, ALPHA * mean_r))
            tau[pk] = round(max(TAU_MIN, min(TAU_MAX, before + step)), 4)
            st = p_stats.setdefault(pk, {"samples": 0, "wins": 0, "sum_ret": 0.0, "nuclear_n": 0})
            for e in evs:
                st["samples"] += 1
                if e["net_ret"] > 0:
                    st["wins"] += 1
                st["sum_ret"] += e["net_ret"]
                if e["kind"] == "penalty_nuclear":
                    st["nuclear_n"] += 1

    # 应用硬性毕业晋升门禁
    path_rows: list[dict[str, Any]] = []
    graduated_paths: list[str] = []
    promising_n = 0
    for pk, t_val in tau.items():
        st = p_stats[pk]
        n = st["samples"]
        wr = round(st["wins"] / n, 4) if n else 0.0
        avg_r = round(st["sum_ret"] / n, 4) if n else 0.0
        nuc_r = round(st["nuclear_n"] / n, 4) if n else 0.0
        gaps: list[str] = []
        if n < B2_GRADUATION_GATE["min_samples"]:
            gaps.append(f"样本 {n}/{int(B2_GRADUATION_GATE['min_samples'])}")
        if wr < B2_GRADUATION_GATE["min_win_rate"]:
            gaps.append(f"胜率 {wr:.0%}<55%")
        if avg_r < B2_GRADUATION_GATE["min_avg_net_ret"]:
            gaps.append(f"净均 {avg_r:+.1%}<+3%")
        if nuc_r > B2_GRADUATION_GATE["max_nuclear_rate"]:
            gaps.append(f"核按钮 {nuc_r:.0%}>10%")
        if t_val < B2_GRADUATION_GATE["min_pheromone"]:
            gaps.append(f"τ={t_val:.2f}<1.10")

        if not gaps:
            status, status_cn = "graduated", "已达标·可解锁卫星仓(0.3×)"
            graduated_paths.append(pk)
        elif avg_r > 0.015 and wr >= 0.45 and t_val >= 1.00:
            status, status_cn = "promising", "潜力观察·待样本达标"
            promising_n += 1
        else:
            status, status_cn = "shadow_only", "淘汰/维持影子观察"
        path_rows.append({
            "path": pk,
            "pheromone": round(t_val, 3),
            "samples": n,
            "win_rate": wr,
            "avg_net_ret": avg_r,
            "nuclear_n": st["nuclear_n"],
            "nuclear_rate": nuc_r,
            "status": status,
            "status_cn": status_cn,
            "gap_to_graduate": " · ".join(gaps) if gaps else "全部达标",
        })
    rank_ord = {"graduated": 0, "promising": 1, "shadow_only": 2}
    path_rows.sort(key=lambda x: (rank_ord[x["status"]], -x["pheromone"], -x["samples"]))

    n_all = len(shadow_trades)
    all_wr = round(sum(1 for t in shadow_trades if t["net_ret"] > 0) / n_all, 4) if n_all else None
    all_avg = round(sum(t["net_ret"] for t in shadow_trades) / n_all, 4) if n_all else None
    all_nuc = sum(1 for t in shadow_trades if t["exit_blocked"] or t["net_ret"] <= -0.15)
    shadow_trades.sort(key=lambda x: (x["entry_day"], x["net_ret"]), reverse=True)

    unthemed_scouts = [
        {
            "entry_day": t["entry_day"],
            "exit_day": t["exit_day"],
            "ts_code": t["ts_code"],
            "name": t["name"],
            "net_ret": t["net_ret"],
            "exit_blocked": t["exit_blocked"],
            "path": t["path"],
            "anchor_bucket": "无题材·市场自选",
            "turnover": t["turnover"],
            "open_gap": t["open_gap"],
            "ice_crossing": t["ice_crossing"],
        }
        for t in shadow_trades
        if t.get("alt_anchor") == "无题材·市场自选"
    ]

    return {
        "available": True,
        "period": f"{min(scan_days)}..{max(scan_days)}",
        "gate": B2_GRADUATION_GATE,
        "summary": {
            "n_trades": n_all,
            "win_rate": all_wr,
            "avg_net_ret": all_avg,
            "nuclear_n": all_nuc,
            "n_paths": len(path_rows),
            "n_graduated": len(graduated_paths),
            "n_promising": promising_n,
            "n_unthemed_scouts": len(unthemed_scouts),
        },
        "graduated_paths": graduated_paths,
        "paths": path_rows[:20],
        "recent_trades": shadow_trades[:15],
        "unthemed_scouts": unthemed_scouts,
        "note": (
            f"B2 影子挑战者（{min(scan_days)}..{max(scan_days)}）：共追踪 {n_all} 笔纯换手二板（含无题材市场自选 {len(unthemed_scouts)} 笔、"
            f"关联产业链有兔/高标带动/一字首板带动），达标可解锁卫星仓路径 {len(graduated_paths)} 条、潜力观察 {promising_n} 条。"
        ),
    }


def collect_unthemed_market_scouts(
    orders: Any,
    *,
    panel: Any = None,
    lookback_days: int = 25,
    as_of_day: str | None = None,
) -> list[dict[str, Any]]:
    """严格因果提取「因无题材分类错过但市场选出的可买换手二板」侦察蚁样本（正负全量）。"""
    b2 = evaluate_b2_turnover_challenger(
        orders, panel=panel, lookback_days=lookback_days, as_of_day=as_of_day,
    )
    if not b2.get("available"):
        return []
    return list(b2.get("unthemed_scouts") or [])


# ---------------------------------------------------------------------------
# 12 因子独立信息素分解引擎（2026-11-26：解决多因子维度爆炸 + 小样本收缩）
# ---------------------------------------------------------------------------

FACTOR_PRIOR_M = 15.0  # 贝叶斯小样本收缩伪样本数（防小样本过拟合）：eff_delta = (tau - 1) * n / (n + 15)

FACTOR_META: dict[str, dict[str, Any]] = {
    "auction":       {"label": "集合竞价溢价", "weight": 0.11},
    "seal_time":     {"label": "上板时间",     "weight": 0.09},
    "turnover":      {"label": "封板换手",     "weight": 0.10},
    "seal_quality":  {"label": "封板质量",     "weight": 0.10},
    "gene_20d":      {"label": "近期涨停基因", "weight": 0.09},
    "breakout_60d":  {"label": "60日位置突破", "weight": 0.07},
    "anchor":        {"label": "题材共振锚定", "weight": 0.10},
    "board_breadth": {"label": "同板块梯队",   "weight": 0.08},
    "high_board":    {"label": "高标存亡环境", "weight": 0.07},
    "zt_premium":    {"label": "昨日涨停溢价", "weight": 0.06},
    "lhb":           {"label": "龙虎榜席位结构", "weight": 0.07},
    "catalyst":      {"label": "外盘/政策催化", "weight": 0.06},
}


def _board_turn_bucket(turn: Any, code: str | None = None) -> str:
    try:
        v = float(turn)
    except (TypeError, ValueError):
        return "换手未知"
    c = str(code or "").zfill(6)
    is_20 = c.startswith(("300", "301", "302", "688", "689", "43", "83", "87", "92"))
    lo, hi = (12.0, 28.0) if is_20 else (8.0, 18.0)
    if v < lo:
        return f"低换手(<{int(lo)}%)"
    if v <= hi:
        return f"健康换手({int(lo)}-{int(hi)}%)"
    return f"高换手(>{int(hi)}%)"


def extract_factors(record: dict[str, Any],
                    *,
                    panel_ctx: dict[str, Any] | None = None) -> dict[str, str]:
    """从单笔委托/成交/侦察记录提取 12 维事前因子分桶（缺项如实跳过，不编造）。"""
    ctx = panel_ctx or {}
    ev = (record.get("guitu") or {}).get("evidence") or {}
    code = str(record.get("ts_code") or "").zfill(6)
    out: dict[str, str] = {}

    # 1) 集合竞价溢价
    gap = record.get("open_gap")
    if gap is None:
        gap = ctx.get("open_gap")
    if gap is not None:
        try:
            gf = float(gap)
            if gf >= 3.0:
                out["auction"] = "弱转强高开(≥3%)"
            elif gf >= -1.0:
                out["auction"] = "平开(-1%~3%)"
            else:
                out["auction"] = "低开弱承接(<-1%)"
        except (TypeError, ValueError):
            pass

    # 2) 上板时间
    fs = record.get("first_seal")
    if fs is None:
        fs = record.get("first_seal_hhmmss")
    if fs is None:
        fs = ev.get("first_seal")
    sb = _seal_bucket(fs)
    if sb != "时段未知":
        out["seal_time"] = sb

    # 3) 封板换手（按 10cm/20cm 动态分档）
    turn = record.get("turn_at_seal")
    if turn is None:
        turn = record.get("turnover")
    if turn is None:
        turn = ev.get("turnover")
    if turn is None:
        turn = ctx.get("turnover")
    tb = _board_turn_bucket(turn, code)
    if tb != "换手未知":
        out["turnover"] = tb

    # 4) 封板质量（严格事前优先：broken_at_entry 优先于事后 broken_max，杜绝训练-推理错位）
    br = record.get("broken_at_entry")
    if br is None:
        br = record.get("broken_max")
    if br is None:
        br = record.get("broken")
    if br is None:
        br = ev.get("broken")
    bb = _broken_bucket(br)
    if bb != "开板未知":
        out["seal_quality"] = bb

    # 5) 近期涨停基因（近 20 日除本轮外涨停次数）
    g20 = record.get("gene_20d")
    if g20 is None:
        g20 = ctx.get("gene_20d")
    if g20 is not None:
        try:
            gn = int(g20)
            if gn >= 2:
                out["gene_20d"] = "老妖活跃(近20日≥2板)"
            elif gn == 1:
                out["gene_20d"] = "有涨停记忆(1板)"
            else:
                out["gene_20d"] = "冷门首启(0板)"
        except (TypeError, ValueError):
            pass

    # 6) 60日位置突破
    bo60 = record.get("breakout_60d")
    if bo60 is None:
        bo60 = ctx.get("breakout_60d")
    if bo60 is not None:
        out["breakout_60d"] = "突破60日新高" if bool(bo60) else "下方有套牢"

    # 7) 题材共振锚定（板块无关）
    out["anchor"] = _anchor_bucket(record)

    # 8) 同板块梯队厚度
    breadth = record.get("board_zt_count")
    if breadth is None:
        breadth = ctx.get("board_zt_count")
    if breadth is not None:
        try:
            bn = int(breadth)
            if bn >= 5:
                out["board_breadth"] = "主线大板块(≥5家)"
            elif bn >= 2:
                out["board_breadth"] = "支线板块(2-4家)"
            else:
                out["board_breadth"] = "孤狼单打(1家)"
        except (TypeError, ValueError):
            pass

    # 9) 高标存亡环境
    hb = record.get("high_board_state") or ctx.get("high_board_state")
    if hb:
        out["high_board"] = str(hb)

    # 10) 昨日涨停溢价
    prem = record.get("zt_premium")
    if prem is None:
        prem = ctx.get("zt_premium")
    if prem is not None:
        try:
            pf = float(prem)
            if pf >= 2.0:
                out["zt_premium"] = "强溢价(≥+2%)"
            elif pf >= -1.0:
                out["zt_premium"] = "中性溢价(-1%~+2%)"
            else:
                out["zt_premium"] = "负溢价亏钱(<-1%)"
        except (TypeError, ValueError):
            pass

    # 11) 龙虎榜席位结构（严格事前 T-1..T-5 只读本地缓存，绝无未来函数）
    lhb_tag = record.get("lhb_tag") or (record.get("lhb_profile") or {}).get("tag") or ctx.get("lhb_tag")
    if not lhb_tag and code and code != "000000":
        d_ev = _norm_day(record.get("day") or record.get("entry_date") or record.get("entry_day"))
        if d_ev:
            try:
                from backend import lhb as _lhb
                lhb_tag = _lhb.cached_stock_tag_before(d_ev, code)
            except Exception:  # noqa: BLE001
                lhb_tag = None
    if lhb_tag:
        out["lhb"] = str(lhb_tag)

    # 12) 外盘/政策消息催化极性（严格事前，用于动态校验新闻利好是否真有效或只是兑现坑）
    cat_s = record.get("catalyst_score")
    if cat_s is not None:
        try:
            cs = float(cat_s)
            if cs >= 2.0:
                out["catalyst"] = "强催化共振(≥+2)"
            elif cs >= 1.0:
                out["catalyst"] = "温和催化(+1~+2)"
            elif cs <= -2.0:
                out["catalyst"] = "利空承压(≤-2)"
            else:
                out["catalyst"] = "催化中性"
        except (TypeError, ValueError):
            pass

    return out


def causal_catalyst_weight_before(
    day: str,
    orders: Any,
    trades: list[dict[str, Any]] | None = None,
) -> float:
    """严格因果计算外盘/政策催化因子的动态权重乘数 ∈ [0.0, 1.5]。

    - 只使用 `exit_date < day` 的已平仓真实交易（绝无未来函数）；
    - 按小样本收缩 n/(n+10)：当带催化(≥+1)历史成交真实收益显著为正时权重向 1.5 上浮，
      当消息利好频繁变成「利好兑现砸盘」亏钱时权重自动衰减至 0.0；
    - 样本不足（n=0）时保持中性基准 1.0。
    """
    cut8 = _norm_day(day)
    ord_map: dict[str, dict[str, Any]] = {}
    for o in _as_list(orders):
        oid = str(o.get("id") or "")
        if oid:
            ord_map[oid] = o
        ed = _norm_day(o.get("day") or o.get("entry_date"))
        c = str(o.get("ts_code") or "").zfill(6)
        if ed and c:
            ord_map[f"{ed}|{c}"] = o

    rets: list[float] = []
    for t in (trades or []):
        ex_d = _norm_day(t.get("exit_date"))
        if not ex_d or (cut8 and ex_d >= cut8):
            continue
        r = t.get("net_return")
        if r is None:
            r = t.get("r_net")
        if r is None:
            r = t.get("net_ret")
        if r is None:
            r = t.get("gross_return")
        if r is None:
            r = t.get("gross_ret")
        if r is None:
            continue
        o = ord_map.get(str(t.get("order_id") or ""))
        if o is None:
            ed = _norm_day(t.get("entry_date"))
            c = str(t.get("ts_code") or "").zfill(6)
            o = ord_map.get(f"{ed}|{c}")
        if not o or o.get("catalyst_score") is None:
            continue
        try:
            if float(o["catalyst_score"]) >= 1.0:
                rets.append(float(r))
        except (TypeError, ValueError):
            continue

    n = len(rets)
    if n == 0:
        return 1.0
    avg_r = sum(rets) / n
    shrink = n / (n + 10.0)
    # avg_r = +5% 满样本 → +0.5 (1.5x); avg_r = -5% 满样本 → -0.5 (0.5x), -10% → 0.0x
    w = 1.0 + avg_r * 10.0 * shrink
    return round(max(0.0, min(1.5, w)), 3)


def _precompute_panel_factor_ctx(
    pairs: set[tuple[str, str]],
    panel: Any = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """一次性从真实日线面板计算目标 (day8, code6) 的严格事前因子（竞价/20日基因/60日突破/高标存亡/昨日涨停溢价）。"""
    if not pairs or panel is None:
        return {}
    try:
        import polars as pl
        from backend import feature_engine
        if panel.height == 0:
            return {}
        target_days = sorted({d for d, _ in pairs if d})
        target_codes = sorted({c for _, c in pairs if c})
        if not target_days or not target_codes:
            return {}
        all_days = sorted({str(d) for d in panel["trade_date"].unique().to_list()})
        d8_to_dash = {_norm_day(d): d for d in all_days}
        valid_dash = [d8_to_dash[d] for d in target_days if d in d8_to_dash]
        if not valid_dash:
            return {}
        min_idx = min(all_days.index(d) for d in valid_dash)
        max_idx = max(all_days.index(d) for d in valid_dash)
        slice_start = all_days[max(0, min_idx - 65)]
        slice_end = all_days[max_idx]

        # A) 个股级事前因子（竞价溢价 / 20日涨停基因 / 60日新高突破）
        cols = ["ts_code", "trade_date", "open", "high", "close"]
        if "turnover" in panel.columns:
            cols.append("turnover")
        sub = (
            panel.filter(pl.col("ts_code").is_in(target_codes))
            .filter(pl.col("trade_date").cast(pl.Utf8) >= slice_start)
            .filter(pl.col("trade_date").cast(pl.Utf8) <= slice_end)
            .select(cols)
            .sort(["ts_code", "trade_date"])
        )
        out: dict[tuple[str, str], dict[str, Any]] = {}
        target_dash_set = set(valid_dash)
        for (c_raw,), g in sub.group_by("ts_code", maintain_order=True):
            c6 = str(c_raw).zfill(6)
            rows = g.to_dicts()
            closes = [float(r["close"] or 0.0) for r in rows]
            if not closes:
                continue
            import numpy as np
            zt_arr, _st_arr = feature_engine.zt_flags(c6, np.array(closes, dtype=float))
            for idx, r in enumerate(rows):
                d_dash = str(r["trade_date"])
                if d_dash not in target_dash_set or idx < 1:
                    continue
                d8 = _norm_day(d_dash)
                if (d8, c6) not in pairs:
                    continue
                pc = closes[idx - 1]
                op = float(r.get("open") or 0.0)
                c_now = closes[idx]
                open_gap = round((op / pc - 1.0) * 100.0, 2) if (pc > 0 and op > 0) else None
                # 近 20 日除本轮连板(idx-1, idx)外的历史涨停数
                lo20 = max(0, idx - 22)
                hi20 = max(0, idx - 1)
                gene_20d = int(sum(1 for k in range(lo20, hi20) if bool(zt_arr[k])))
                # 60日新高突破（对比 idx-60..idx-1 最高价，严格事前）
                lo60 = max(0, idx - 60)
                prev_hi60 = max((float(rows[k].get("high") or closes[k]) for k in range(lo60, idx)), default=0.0)
                bo60 = bool(prev_hi60 > 0 and c_now >= prev_hi60 * 0.99)
                tv = r.get("turnover")
                out[(d8, c6)] = {
                    "open_gap": open_gap,
                    "gene_20d": gene_20d,
                    "breakout_60d": bo60,
                    "turnover": float(tv) if (tv is not None and tv == tv) else None,
                }

        # B) 市场级事前/当日环境因子（昨日涨停溢价 + 昨日高标存亡）
        mkt_start = all_days[max(0, min_idx - 10)]
        mkt = (
            panel.filter(pl.col("trade_date").cast(pl.Utf8) >= mkt_start)
            .filter(pl.col("trade_date").cast(pl.Utf8) <= slice_end)
            .select(["ts_code", "trade_date", "close"])
            .sort(["ts_code", "trade_date"])
            .with_columns(pl.col("close").shift(1).over("ts_code").alias("pc"))
        )
        m_codes = mkt["ts_code"].unique().to_list()
        lim_m = {str(x): float(feature_engine.limit_pct(str(x))) * 100.0 for x in m_codes}
        mkt = mkt.with_columns(
            pl.col("ts_code").cast(pl.Utf8).replace_strict(lim_m, return_dtype=pl.Float64).alias("lim")
        ).with_columns(
            ((pl.col("close") / pl.col("pc") - 1.0) * 100.0).alias("pct")
        ).with_columns([
            (pl.col("pct") >= pl.col("lim") - 0.3).fill_null(False).alias("is_zt"),
            (pl.col("pct") <= -(pl.col("lim") - 0.3)).fill_null(False).alias("is_dt"),
        ]).with_columns([
            pl.col("is_zt").cast(pl.Int32).alias("_z"),
            (~pl.col("is_zt")).cast(pl.Int32).cum_sum().over("ts_code").alias("_off"),
        ]).with_columns(
            pl.when(pl.col("is_zt"))
            .then(pl.col("_z").cum_sum().over(["ts_code", "_off"]))
            .otherwise(0).cast(pl.Int32).alias("streak")
        ).with_columns([
            pl.col("streak").shift(1).over("ts_code").fill_null(0).alias("prev_streak"),
            pl.col("is_zt").shift(1).over("ts_code").fill_null(False).alias("prev_zt"),
        ])

        day_env: dict[str, dict[str, Any]] = {}
        mkt_t = mkt.filter(pl.col("trade_date").cast(pl.Utf8).is_in(valid_dash))
        for (d_dash,), dg in mkt_t.group_by("trade_date"):
            d8 = _norm_day(d_dash)
            zt_rows = dg.filter(pl.col("prev_zt") & pl.col("pct").is_not_null())
            prem = float(zt_rows["pct"].mean()) if zt_rows.height else None
            hi_rows = dg.filter((pl.col("prev_streak") >= 3) & pl.col("pct").is_not_null())
            hb_state = None
            if hi_rows.height:
                dt_n = int(hi_rows.filter(pl.col("is_dt")).height)
                zt_n = int(hi_rows.filter(pl.col("is_zt")).height)
                if dt_n >= 2 or (dt_n >= 1 and dt_n / hi_rows.height >= 0.4):
                    hb_state = "高标核按钮"
                elif zt_n >= 1 and dt_n == 0:
                    hb_state = "高标晋级引领"
                else:
                    hb_state = "高标分歧震荡"
            day_env[d8] = {"zt_premium": round(prem, 2) if prem is not None else None,
                           "high_board_state": hb_state}

        # C) 同板块当日涨停梯队厚度（复用 _load_theme_map_for_day，有题材池的日期才填）
        breadth_cache: dict[str, dict[str, int]] = {}
        for d8, c6 in pairs:
            slot = out.setdefault((d8, c6), {})
            env = day_env.get(d8)
            if env:
                slot["zt_premium"] = env.get("zt_premium")
                slot["high_board_state"] = env.get("high_board_state")
            if d8 not in breadth_cache:
                _, amap = _load_theme_map_for_day(d8)
                th_cnt: dict[str, int] = defaultdict(int)
                for _cc, ths in amap.items():
                    for th in ths:
                        th_cnt[th] += 1
                code_b: dict[str, int] = {}
                for _cc, ths in amap.items():
                    if ths:
                        code_b[_cc] = max(th_cnt.get(th, 1) for th in ths)
                breadth_cache[d8] = code_b
            if c6 in breadth_cache[d8]:
                slot["board_zt_count"] = breadth_cache[d8][c6]
        return out
    except Exception:  # noqa: BLE001
        return {}


def evolve_factors(
    events_by_day: dict[str, list[dict[str, Any]]],
    timeline: list[str],
    *,
    rho: float = RHO,
    alpha: float = ALPHA,
) -> dict[str, Any]:
    """10 因子独立信息素演化（含小样本贝叶斯收缩 n/(n+5)，严格因果按结算日推进）。"""
    # key = (dim, bucket)
    tau: dict[tuple[str, str], float] = {}
    f_stats: dict[tuple[str, str], dict[str, Any]] = {}

    for day in timeline:
        for fk in list(tau.keys()):
            tau[fk] = round((1.0 - rho) * tau[fk] + rho * TAU_INIT, 4)
        day_evts = events_by_day.get(day) or []
        by_fk: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for ev in day_evts:
            facs = ev.get("factors") or {}
            for dim, bucket in facs.items():
                if dim in FACTOR_META and bucket:
                    by_fk[(dim, str(bucket))].append(ev)
        for fk, evs in by_fk.items():
            before = tau.get(fk, TAU_INIT)
            mean_r = sum(e["reward"] for e in evs) / len(evs)
            step = max(-MAX_DAILY_STEP, min(MAX_DAILY_STEP, alpha * mean_r))
            after = round(max(TAU_MIN, min(TAU_MAX, before + step)), 4)
            tau[fk] = after
            st = f_stats.setdefault(fk, {"n": 0, "wins": 0, "nuclear_n": 0, "sum_ret": 0.0, "last_day": day})
            for e in evs:
                if e.get("filled") or e.get("source") == "unthemed_market_scout":
                    st["n"] += 1
                    if e["ret"] > 0:
                        st["wins"] += 1
                    st["sum_ret"] += float(e["ret"])
                    if "nuclear" in str(e.get("kind") or ""):
                        st["nuclear_n"] += 1
                st["last_day"] = day

    rows: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, dict[str, float]]] = {}
    for (dim, bucket), t_val in tau.items():
        st = f_stats.get((dim, bucket), {})
        n = int(st.get("n") or 0)
        wins = int(st.get("wins") or 0)
        shrink = n / (n + FACTOR_PRIOR_M) if n > 0 else 0.0
        shrunk_delta = (t_val - TAU_INIT) * shrink
        eff_mult = round(max(0.30, min(1.25, 1.0 + 0.5 * shrunk_delta)), 3)
        trend = "强化" if shrunk_delta > 0.03 else ("衰减" if shrunk_delta < -0.03 else "中性")
        meta = FACTOR_META[dim]
        rows.append({
            "dim": dim,
            "dim_label": meta["label"],
            "weight": meta["weight"],
            "bucket": bucket,
            "pheromone": round(t_val, 3),
            "shrink_ratio": round(shrink, 3),
            "shrunk_delta": round(shrunk_delta, 4),
            "eff_mult": eff_mult,
            "samples": n,
            "win_rate": round(wins / n, 3) if n > 0 else None,
            "avg_ret": round(st["sum_ret"] / n, 4) if n > 0 else None,
            "nuclear_n": int(st.get("nuclear_n") or 0),
            "trend": trend,
            "last_day": st.get("last_day") or "",
        })
        lookup.setdefault(dim, {})[bucket] = {
            "tau": round(t_val, 3),
            "shrunk_delta": round(shrunk_delta, 4),
            "eff_mult": eff_mult,
            "samples": n,
        }

    dim_order = list(FACTOR_META.keys())
    rows.sort(key=lambda x: (dim_order.index(x["dim"]), -x["shrunk_delta"], -x["samples"]))
    return {
        "n_dims": len({r["dim"] for r in rows}),
        "n_buckets": len(rows),
        "prior_m": FACTOR_PRIOR_M,
        "rows": rows,
        "lookup": lookup,
    }


def score_candidate_factors(
    factor_lookup: dict[str, dict[str, dict[str, float]]],
    cand_factors: dict[str, str],
) -> dict[str, Any]:
    """将候选票命中的多维因子信息素（小样本收缩后）加权合成为安全仓位系数 ∈ [0.30, 1.25]。"""
    w_sum = 0.0
    delta_sum = 0.0
    details: list[dict[str, Any]] = []
    for dim, bucket in (cand_factors or {}).items():
        if dim not in FACTOR_META:
            continue
        info = (factor_lookup.get(dim) or {}).get(bucket)
        if not info:
            continue
        w = float(FACTOR_META[dim]["weight"])
        sd = float(info.get("shrunk_delta") or 0.0)
        w_sum += w
        delta_sum += w * sd
        details.append({
            "dim": dim,
            "label": FACTOR_META[dim]["label"],
            "bucket": bucket,
            "tau": info.get("tau", 1.0),
            "shrunk_delta": sd,
            "samples": info.get("samples", 0),
        })
    weighted_delta = (delta_sum / w_sum) if w_sum > 0 else 0.0
    size_mult = round(max(0.30, min(1.25, 1.0 + 0.8 * weighted_delta)), 3)
    return {
        "size_mult": size_mult,
        "weighted_delta": round(weighted_delta, 4),
        "n_matched": len(details),
        "details": details,
    }


def causal_factor_scores_before(
    day: str,
    orders: Any,
    trades: Any,
    candidates: dict[str, dict[str, Any]],
    *,
    all_days: list[str] | None = None,
    panel: Any = None,
) -> dict[str, Any]:
    """严格因果 12 维事前因子贝叶斯收缩仓位打分引擎（替代稀疏 4 维笛卡尔路径主决策）。

    - 只使用 `exit_date < day` 的真实历史成交与 `day` 之前的侦察蚁反馈；
    - 候选标的严格使用挂单瞬间的事前字段（`turn_at_seal`、`broken_at_entry` 等）提取 12 维因子；
    - 同步输出高换手/烂板因子收缩 delta，供 `fill_verify.adaptive_cancel_thresholds` 联动。
    """
    d_cut = _norm_day(day)
    if not d_cut:
        return {"by_code": {}, "high_turn_delta": None, "rotten_delta": None, "n_trades": 0}
    past_trades = [
        t for t in _as_list(trades)
        if (_norm_day(t.get("exit_date")) or _norm_day(t.get("entry_date"))) < d_cut
    ]
    past_orders = [
        o for o in _as_list(orders)
        if _norm_day(o.get("day") or o.get("entry_date")) < d_cut
    ]
    past_days = [x for x in (all_days or []) if _norm_day(x) < d_cut] if all_days else None
    if not past_trades and not any(o.get("missed_next_ret") is not None for o in past_orders):
        return {"by_code": {}, "high_turn_delta": None, "rotten_delta": None, "n_trades": 0}

    res = evolve_from_records(past_orders, past_trades, all_days=past_days, panel=panel)
    lookup = ((res.get("factor_pheromones") or {}).get("lookup") or {})
    if not lookup:
        return {"by_code": {}, "high_turn_delta": None, "rotten_delta": None, "n_trades": len(past_trades)}

    by_code: dict[str, dict[str, Any]] = {}
    for raw_c, cand in (candidates or {}).items():
        c6 = str(raw_c).zfill(6)
        # 确保候选记录显式锁定事前 broken_at_entry（优先首帧 broken）与 turn_at_seal
        c_rec = dict(cand)
        if c_rec.get("broken_at_entry") is None:
            first_row = c_rec.get("first") or {}
            if first_row.get("broken") is not None:
                c_rec["broken_at_entry"] = first_row.get("broken")
        if c_rec.get("turn_at_seal") is None:
            first_row = c_rec.get("first") or {}
            if first_row.get("turnover") is not None:
                c_rec["turn_at_seal"] = first_row.get("turnover")
        facs = extract_factors(c_rec)
        scored = score_candidate_factors(lookup, facs)
        if scored["n_matched"] > 0:
            by_code[c6] = {
                "size_mult": scored["size_mult"],
                "weighted_delta": scored["weighted_delta"],
                "n_matched": scored["n_matched"],
                "factors": facs,
                "details": scored["details"],
            }

    # 提取高换手/烂板因子收缩 delta，供排板撤单阈值动态联动
    ht_delta: float | None = None
    for bkt, info in (lookup.get("turnover") or {}).items():
        if "高换手" in bkt:
            ht_delta = float(info.get("shrunk_delta") or 0.0)
            break
    rot_delta: float | None = None
    for bkt, info in (lookup.get("seal_quality") or {}).items():
        if "烂板" in bkt:
            rot_delta = float(info.get("shrunk_delta") or 0.0)
            break

    return {
        "by_code": by_code,
        "high_turn_delta": ht_delta,
        "rotten_delta": rot_delta,
        "n_trades": len(past_trades),
    }


CANCEL_KIND_CN: dict[str, str] = {
    "main_withdraw": "主力撤单",
    "seal_bleed": "封单失血",
    "rabbit_drag_bleed": "兔子拖累失血",
    "thin_seal": "跌破薄封线",
    "rotten_broken": "烂板开板超限",
    "high_turnover": "换手突破警戒",
}


def evaluate_cancel_counterfactual(
    orders: Any,
    *,
    as_of_day: str | None = None,
) -> dict[str, Any]:
    """撤单反事实对照与自我进化因子：检验每次主动撤单究竟是「精准躲坑」还是「被主力假撤单洗盘吓跑」。

    严格因果：只统计次日真实收益已落盘且 `missed_next_day < as_of_day` 的撤单样本；
    结合小样本收缩 `n/(n+10)` 输出各撤单类型的自适应阈值微调系数 `threshold_adj_by_kind`：
      - 频繁被假撤单吓跑（撤后次日平均上涨）→ 正系数，自动放宽对应撤单阈值；
      - 确实躲过炸板核按钮（撤后次日平均下跌）→ 负系数，自动收紧/维持对应撤单阈值。
    """
    cut8 = _norm_day(as_of_day) if as_of_day else ""
    order_list = _as_list(orders)
    cancelled = [
        o for o in order_list
        if o.get("status") == "cancelled" and not o.get("invalid_historical_bug")
    ]
    if not cancelled:
        return {
            "available": True,
            "n_cancelled": 0,
            "n_verified": 0,
            "by_kind": [],
            "threshold_adj_by_kind": {},
            "cases": [],
            "note": "尚无主动撤单记录，产生撤单后自动追踪次日走势验证是否被主力洗盘吓跑。",
        }

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cases: list[dict[str, Any]] = []
    for o in cancelled:
        kind = str(o.get("cancel_kind") or (o.get("queue_sim") or {}).get("cancel_kind") or "unknown")
        mnr = o.get("missed_next_ret")
        mnd = _norm_day(o.get("missed_next_day"))
        verified = (mnr is not None) and (not cut8 or (mnd and mnd < cut8))
        ret_f = float(mnr) if verified else None
        verdict = "待次日验证"
        if ret_f is not None:
            if ret_f >= 0.03:
                verdict = "被主力假撤单洗飞"
            elif ret_f < 0:
                verdict = "撤对了·成功躲坑"
            else:
                verdict = "中性（小幅波动）"
        row = {
            "day": str(o.get("day") or o.get("entry_date") or ""),
            "ts_code": str(o.get("ts_code") or "").zfill(6),
            "name": str(o.get("name") or ""),
            "cancel_kind": kind,
            "cancel_kind_cn": CANCEL_KIND_CN.get(kind, kind),
            "cancel_ts": o.get("cancel_frame_ts") or (o.get("queue_sim") or {}).get("cancel_ts"),
            "next_day": mnd or None,
            "next_ret": round(ret_f, 4) if ret_f is not None else None,
            "verdict": verdict,
        }
        cases.append(row)
        buckets[kind].append(row)

    by_kind: list[dict[str, Any]] = []
    adj_map: dict[str, float] = {}
    total_verified = 0
    for kind, items in sorted(buckets.items()):
        ver_items = [x for x in items if x["next_ret"] is not None]
        nv = len(ver_items)
        total_verified += nv
        n_saved = sum(1 for x in ver_items if x["next_ret"] < 0)
        n_shaken = sum(1 for x in ver_items if x["next_ret"] >= 0.03)
        avg_r = round(sum(x["next_ret"] for x in ver_items) / nv, 4) if nv else None
        # 小样本收缩 n/(n+10)：频繁被洗飞(avg_r>0) → 正微调(放宽撤单门槛)；频繁躲坑(avg_r<0) → 负微调(保持/收紧)
        shrink = nv / (nv + 10.0) if nv else 0.0
        adj = round(max(-0.15, min(0.25, (avg_r or 0.0) * 2.0 * shrink)), 4) if nv else 0.0
        adj_map[kind] = adj
        by_kind.append({
            "kind": kind,
            "kind_cn": CANCEL_KIND_CN.get(kind, kind),
            "n_total": len(items),
            "n_verified": nv,
            "n_saved": n_saved,
            "n_shaken_out": n_shaken,
            "avg_next_ret": avg_r,
            "threshold_adj": adj,
            "suggestion": (
                f"疑似假撤单洗盘偏多（放宽阈值 +{adj:.0%}）" if adj >= 0.03
                else (f"撤单躲坑有效（维持/微收紧 {adj:.0%}）" if adj <= -0.02 else "样本积累中·维持动态基准")
            ),
        })

    cases.sort(key=lambda x: x["day"], reverse=True)
    return {
        "available": True,
        "n_cancelled": len(cancelled),
        "n_verified": total_verified,
        "by_kind": by_kind,
        "threshold_adj_by_kind": adj_map,
        "cases": cases[:20],
        "note": f"已追踪主动撤单 {len(cancelled)} 笔（次日已验证 {total_verified} 笔），反事实对比自动区分「真出货躲坑」与「主力假撤单洗盘」。",
    }


def snapshot() -> dict[str, Any]:
    """从真实生产账本只读计算当前信息素进化快照 + 踏空反思 + B2 纯换手影子挑战者 + 撤单反事实对照。"""
    try:
        from backend import feeds, live_sim
        sf = getattr(live_sim, "STATE_FILE", paths.data_dir() / "paper" / "sim_state.json")
        panel = None
        try:
            panel = feeds.build_panel()
        except Exception:  # noqa: BLE001
            panel = None
        if not Path(sf).is_file():
            b2 = evaluate_b2_turnover_challenger([], panel=panel)
            base = evolve_from_records([], [], market_scouts=b2.get("unthemed_scouts"), panel=panel)
            base["missed_reflection"] = analyze_missed_winners([], panel=panel)
            base["b2_challenger"] = b2
            base["cancel_counterfactual"] = evaluate_cancel_counterfactual([])
            return base
        from backend import position_sizing as _ps
        st = json.loads(Path(sf).read_text(encoding="utf-8"))
        raw_orders = st.get("orders") or {}
        orders = enrich_unfilled_regret(raw_orders, panel=panel)
        if isinstance(orders, dict):
            orders = {k: v for k, v in orders.items() if not _ps.is_invalid_record(v)}
        clean_t = _ps.clean_trades(st.get("trades") or [], raw_orders)
        b2 = evaluate_b2_turnover_challenger(orders, panel=panel)
        res = evolve_from_records(
            orders,
            clean_t,
            all_days=st.get("days") or None,
            market_scouts=b2.get("unthemed_scouts"),
            panel=panel,
        )
        res["missed_reflection"] = analyze_missed_winners(orders, panel=panel)
        res["b2_challenger"] = b2
        res["cancel_counterfactual"] = evaluate_cancel_counterfactual(orders)
        return res
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "paths": [],
            "feedback_log": [],
            "advice": [],
            "missed_reflection": {"available": False, "missed_winners": [], "factor_proposals": []},
            "b2_challenger": {"available": False, "paths": [], "recent_trades": []},
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
