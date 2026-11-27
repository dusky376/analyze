# -*- coding: utf-8 -*-
"""仓位规划与账户级熔断风控模块（自 live_sim.py 解耦拆出，对外保持 100% 接口兼容）。"""
from __future__ import annotations

from typing import Any

from backend import feature_engine

CAPITAL_YUAN = 1_000_000.0
#: 常态单笔仓位占总资金比例（10% = 10 万；无质量分级时的基准）
POSITION_PCT = 0.10
#: 复利弹性分级基础仓（仅当候选带 buy_quality_pre.grade 时生效，常态单票硬顶 15%，真主线顺风软顶 18%）
GRADE_BASE_PCT: dict[str, float] = {"A": 0.14, "B": 0.10, "C": 0.06}
MAX_SINGLE_PCT = 0.15
MAX_SINGLE_MAINLINE_PCT = 0.18
BOARD_20CM_RISK_MULT = 0.65
MAINLINE_BOOST_MULT = 1.20
#: 总仓上限占比（80%，留 20% 现金）
MAX_GROSS_PCT = 0.80
#: 常态单笔上限（元）= 100 万 × 10%
POSITION_SIZE_YUAN = CAPITAL_YUAN * POSITION_PCT
#: A 股最小交易单位（1 手 = 100 股），数量必须是它的整数倍。
LOT_SIZE = 100
#: 单日聚焦前排上限（避免类似 11-24 单日摊大饼 21 只集体吃面）
MAX_PER_THEME_ORDERS = 2
MAX_DAILY_ORDERS = 5


def plan_orders(candidates: list[dict[str, Any]], *,
                size_factor: float = 1.0,
                capital: float = CAPITAL_YUAN,
                held_notional: float = 0.0,
                evo_mults: dict[str, float] | None = None,
                mainline_boost_codes: set[str] | None = None) -> list[dict[str, Any]]:
    """给当日候选分配单笔金额（复利弹性分级 + 20cm等风险缩放 + 真主线顺风松绑 + 冰点/因果信息素 + 总仓 80% 上限）。"""
    factor = max(0.0, min(1.0, float(size_factor or 1.0)))
    cap = max(0.0, float(capital or 0.0))
    gross_cap = cap * MAX_GROSS_PCT
    available = max(0.0, gross_cap - max(0.0, float(held_notional or 0.0)))
    boost_set = {str(x).zfill(6) for x in (mainline_boost_codes or set())}

    n = len(candidates)
    if n == 0:
        return []
    out: list[dict[str, Any]] = []
    remaining = available
    for i, g in enumerate(candidates):
        code = str(g.get("ts_code") or "")
        code6 = code.zfill(6) if code.isdigit() else code
        left_n = n - i
        per_cap = remaining / left_n if left_n > 0 else 0.0

        # 1) 买点质量分级基础仓（无 buy_quality_pre 时保持 POSITION_PCT=10% 兼容旧口径）
        bq = g.get("buy_quality_pre")
        grade = str((bq or {}).get("grade") or "").upper() if isinstance(bq, dict) else ""
        base_pct = GRADE_BASE_PCT.get(grade, POSITION_PCT) if grade in GRADE_BASE_PCT else POSITION_PCT
        base = cap * base_pct * factor

        # 2) 20cm/30cm 板块等风险缩放（单日波动 2 倍 → 仓位 ×0.65，保持单笔最大止损风险一致）
        board_mult = 1.0
        if code6 and len(code6) == 6 and code6.isdigit():
            try:
                if float(feature_engine.limit_pct(code6)) >= 0.20:
                    board_mult = BOARD_20CM_RISK_MULT
            except Exception:  # noqa: BLE001
                board_mult = 1.0

        # 3) 真主线爆发顺风弹性（仅对共振确认的 A 级前排松绑）
        is_mainline = bool(code6 in boost_set and grade == "A")
        ml_mult = MAINLINE_BOOST_MULT if is_mainline else 1.0
        single_cap = cap * (MAX_SINGLE_MAINLINE_PCT if is_mainline else MAX_SINGLE_PCT)

        # 4) 严格因果信息素系数
        em = 1.0
        if evo_mults and code in evo_mults:
            try:
                em = max(0.30, min(1.25, float(evo_mults[code])))
            except (TypeError, ValueError):
                em = 1.0

        target_base = min(single_cap, base * board_mult * ml_mult * em)
        size_yuan = min(target_base, per_cap)
        reason = []
        if grade in GRADE_BASE_PCT:
            reason.append(f"{grade}级基础 {base_pct:.0%}")
        if board_mult < 1.0:
            reason.append(f"20cm等风险 ×{board_mult:.2f}")
        if is_mainline:
            reason.append(f"真主线顺风 ×{ml_mult:.2f}")
        if size_yuan < target_base - 1e-6:
            reason.append(f"可用资金均分降至 {size_yuan:,.0f}")
        if factor < 1.0:
            reason.append(f"冰点降仓 ×{factor:.2f}")
        if abs(em - 1.0) >= 0.02:
            reason.append(f"信息素反馈 ×{em:.2f}")
        # 至少 1 手成本，避免出现 "0 股"
        price = None
        try:
            price = float((g.get("first") or {}).get("price"))
        except (TypeError, ValueError):
            price = None
        min_lot_cost = (price * LOT_SIZE) if (price and price > 0) else 0.0
        if size_yuan < min_lot_cost and min_lot_cost > 0:
            size_yuan = min_lot_cost
            reason.append("不足 1 手按 1 手")
        out.append({"code": code,
                    "size_yuan": round(size_yuan, 2),
                    "evo_mult": round(em, 3),
                    "grade": grade or None,
                    "mainline_boost": is_mainline,
                    "budget_reason": "；".join(reason) or "常态 10%"})
        remaining = max(0.0, remaining - size_yuan)
    return out


def position_size(price: float | None,
                  *, size_yuan: float = POSITION_SIZE_YUAN,
                  lot: int = LOT_SIZE) -> tuple[int | None, float | None]:
    """价格 → (股数, 成交额)。"""
    try:
        p = float(price) if price is not None else None
    except (TypeError, ValueError):
        p = None
    if p is None or not (p > 0) or not (size_yuan > 0) or lot <= 0:
        return None, None
    lots = max(1, int(size_yuan // (p * lot)))
    qty = lots * lot
    return qty, round(p * qty, 2)


def is_invalid_record(rec: Any, invalid_oids: set[str] | None = None) -> bool:
    """判定订单/持仓/成交记录是否属于已作废的历史 bug 脏单。"""
    if not isinstance(rec, dict):
        return False
    if rec.get("invalid_historical_bug"):
        return True
    oid = rec.get("id") or rec.get("order_id")
    if invalid_oids and oid and str(oid) in invalid_oids:
        return True
    return False


def clean_trades(trades: list[dict[str, Any]] | None,
                 orders: dict[str, dict[str, Any]] | list[dict[str, Any]] | None = None
                 ) -> list[dict[str, Any]]:
    """统一过滤历史 bug 脏单，严禁脏数据污染净值、熔断器、进化与复盘。"""
    invalid_oids: set[str] = set()
    if isinstance(orders, dict):
        invalid_oids = {str(k) for k, v in orders.items() if isinstance(v, dict) and v.get("invalid_historical_bug")}
    elif isinstance(orders, list):
        invalid_oids = {
            str(v.get("id") or v.get("order_id") or "")
            for v in orders if isinstance(v, dict) and v.get("invalid_historical_bug")
        }
    return [t for t in (trades or []) if not is_invalid_record(t, invalid_oids)]


def account_circuit_breaker(
    trades: list[dict[str, Any]] | None,
    day: str,
    *,
    capital: float = CAPITAL_YUAN,
    defense_dd: float = 0.05,
    halt_dd: float = 0.08,
    defense_loss_days: int = 2,
    halt_loss_days: int = 3,
    rolling_days: int = 10,
) -> dict[str, Any]:
    """账户级连亏自动熔断（严格因果：只统计 exit_date < day 的已结算成交，带滚动窗口与自动解冻）。"""
    from backend import dates as _dates

    d_cut = _dates.norm_day(day)
    pnl_by_day: dict[str, float] = {}
    for t in clean_trades(trades):
        xd = _dates.norm_day(t.get("exit_date") or t.get("entry_date"))
        if not xd or (d_cut and xd >= d_cut):
            continue
        nr = t.get("net_return")
        if not isinstance(nr, (int, float)):
            continue
        amt = float(t.get("notional") or 0.0) or POSITION_SIZE_YUAN
        pnl_by_day[str(xd)] = pnl_by_day.get(str(xd), 0.0) + float(nr) * amt

    if not pnl_by_day or capital <= 0:
        return {
            "mode": "normal",
            "mode_cn": "正常态",
            "size_mult": 1.0,
            "block_new_orders": False,
            "only_grade_a": False,
            "consec_loss_days": 0,
            "drawdown_pct": 0.0,
            "reason": "无历史亏损熔断触发，正常按策略执行",
        }

    sorted_days = sorted(pnl_by_day.keys())
    eq = float(capital)
    eq_series: list[float] = [eq]
    consec_loss = 0
    for d_k in sorted_days:
        day_pnl = pnl_by_day[d_k]
        eq += day_pnl
        eq_series.append(eq)
        if day_pnl < 0:
            consec_loss += 1
        elif day_pnl > 0:
            consec_loss = 0

    win_eq = eq_series[-(rolling_days + 1):]
    peak = max(win_eq) if win_eq else float(capital)
    dd = max(0.0, (peak - eq) / peak) if peak > 0 else 0.0

    if consec_loss >= halt_loss_days or (consec_loss > 0 and dd >= halt_dd):
        return {
            "mode": "halt",
            "mode_cn": "熔断空仓观察",
            "size_mult": 0.0,
            "block_new_orders": True,
            "only_grade_a": True,
            "consec_loss_days": consec_loss,
            "drawdown_pct": round(dd, 4),
            "reason": (
                f"触发账户熔断（连亏 {consec_loss} 日 / 滚动回撤 {dd:.1%} ≥ {halt_dd:.0%}）→ "
                "暂停实盘新开仓，待赚钱效应恢复后自动解冻"
            ),
        }
    if dd >= defense_dd or consec_loss >= defense_loss_days:
        thaw_txt = "（盈利已自动解冻，降级为防守试错）" if (dd >= halt_dd and consec_loss == 0) else ""
        return {
            "mode": "defense",
            "mode_cn": "防守减半",
            "size_mult": 0.50,
            "block_new_orders": False,
            "only_grade_a": True,
            "consec_loss_days": consec_loss,
            "drawdown_pct": round(dd, 4),
            "reason": (
                f"触发防守降仓{thaw_txt}（连亏 {consec_loss} 日 / 滚动回撤 {dd:.1%} ≥ {defense_dd:.0%}）→ "
                "仓位减半 ×0.50 且仅保留前排精选"
            ),
        }
    return {
        "mode": "normal",
        "mode_cn": "正常态",
        "size_mult": 1.0,
        "block_new_orders": False,
        "only_grade_a": False,
        "consec_loss_days": consec_loss,
        "drawdown_pct": round(dd, 4),
        "reason": f"账户风控正常（连亏 {consec_loss} 日 / 滚动回撤 {dd:.1%}）",
    }
