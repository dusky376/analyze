"""蚁群信息素（ACO）+ 非对称奖惩自进化引擎（影子对照，零虚构数据）。

核心设计原则
------------
1. **严格因果（无未来函数）**：第 T 日的路径权重与仓位系数，严格只由 T-1 及之前的真实结算/侦察结果更新而来。
2. **自然挥发（随市场风格切换自动进化）**：
       tau(t+1) = clip((1 - rho) * tau(t) + rho * tau_0 + alpha * mean_reward, tau_min, tau_max)
   当市场主线切换时，旧路径的信息素会按 rho=0.10 自动向中性值 1.0 挥发衰减，不会死守过去模式。
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

RHO = 0.10          # 每日自然挥发率（市场风格切换衰减）
ALPHA = 1.60        # 奖惩沉积系数
TAU_INIT = 1.00     # 中性初始信息素
TAU_MIN = 0.25      # 最低信息素下限（对应 0.30x 仓位）
TAU_MAX = 1.50      # 最高信息素上限（对应 1.25x 仓位）
MAX_DAILY_STEP = 0.15


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
    """将一笔委托或成交映射为可解释的「题材 × 形态」寻优路径键。"""
    ev = (record.get("guitu") or {}).get("evidence") or {}
    theme = (record.get("guitu_theme")
             or record.get("theme")
             or record.get("industry")
             or "未归类")
    theme_s = str(theme).strip() or "未归类"
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
    broken = record.get("broken_max")
    if broken is None:
        broken = record.get("broken")
    if broken is None:
        broken = ev.get("broken")
    bb = _broken_bucket(broken)
    return f"{theme_s} | {sb} | {tb} | {bb}"


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
                        all_days: list[str] | None = None) -> dict[str, Any]:
    """按交易日严格因果推进蚁群信息素更新，并输出影子账本对照。

    Args:
        orders: 委托集合（支持 list 或 sim_state.json 的 dict 形态）
        trades: 已结算成交列表（含 entry_date, exit_date, gross_return/net_return, exit_blocked）
        all_days: 可选的交易日序列（用于在空仓日也执行自然挥发）
    """
    order_list = _as_list(orders)
    trade_list = _as_list(trades)

    order_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for o in order_list:
        d = _norm_day(o.get("day") or o.get("entry_date"))
        c = str(o.get("ts_code") or "").zfill(6)
        if d and c:
            order_by_key[(d, c)] = o

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
        events_by_day[xd].append({
            "day": xd,
            "entry_day": ed,
            "ts_code": c,
            "name": str(merged.get("name") or ""),
            "path": path_key,
            "ret": float(ret),
            "filled": True,
            **rw,
        })

    # 侦察蚁：未成交单若带 missed_next_ret 字段也纳入信息素与遗憾统计
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
                    **rw,
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
                                       "nuclear_n": 0, "regret_n": 0, "last_day": day})
            for e in evs:
                if e["filled"]:
                    st["samples"] += 1
                    if e["ret"] > 0:
                        st["wins"] += 1
                    if e["kind"] == "penalty_nuclear":
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
        avg_rw = round(st["sum_reward"] / max(1, n + int(st.get("regret_n") or 0)), 4)
        trend = "强化" if t_val > 1.05 else ("衰减" if t_val < 0.95 else "中性")
        path_rows.append({
            "path": pk,
            "pheromone": round(t_val, 3),
            "size_mult": pheromone_to_size_mult(t_val),
            "samples": n,
            "win_rate": round(wins / n, 3) if n > 0 else None,
            "avg_reward": avg_rw,
            "nuclear_n": int(st.get("nuclear_n") or 0),
            "regret_n": int(st.get("regret_n") or 0),
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

    return {
        "available": True,
        "mode": "causal_feedback",
        "params": {
            "rho_evaporation": rho,
            "alpha_deposit": alpha,
            "tau_range": [TAU_MIN, TAU_MAX],
            "size_mult_range": [0.30, 1.25],
            "max_daily_step": MAX_DAILY_STEP,
        },
        "n_paths": len(path_rows),
        "n_trades": len(base_rets),
        "paths": path_rows,
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


def snapshot() -> dict[str, Any]:
    """从真实生产账本只读计算当前信息素进化快照（绝不写生产账本）。"""
    try:
        from backend import live_sim
        sf = getattr(live_sim, "STATE_FILE", paths.data_dir() / "paper" / "sim_state.json")
        if not Path(sf).is_file():
            return evolve_from_records([], [])
        st = json.loads(Path(sf).read_text(encoding="utf-8"))
        orders = enrich_unfilled_regret(st.get("orders") or {})
        return evolve_from_records(
            orders,
            st.get("trades") or [],
            all_days=st.get("days") or None,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "paths": [],
            "feedback_log": [],
            "advice": [],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
