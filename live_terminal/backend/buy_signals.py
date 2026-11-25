"""买点质量评估、烂板撤单预警与临封扫板雷达（E5:B1 进阶买点模块）。

解决当前单一排板的两大实战痛点：
1. 排板逆向选择：强势乌龟封单极硬排不进，反复炸板高换手的烂板反而全成交；
   本模块给出质量分级（A/B/C/REJECT）与封单失血撤单预警，供实盘/影子账本过滤。
2. 强票买不进：09:25 一字兔锚定后，提前扫描同题材强势拉升（距涨停 1.5%–3%）的
   临封标的，提供“半路/临封扫板”观察清单（只读真实行情，绝不伪造信号）。
"""
from __future__ import annotations

from typing import Any


def _limit_pct(code: str) -> float:
    c = str(code or "").strip().zfill(6)
    if c.startswith(("43", "83", "87", "92")):
        return 0.30
    if c.startswith(("300", "301", "302", "688", "689")):
        return 0.20
    return 0.10


def _hhmmss_to_min(raw: Any) -> int | None:
    s = str(raw or "").replace(":", "").strip()
    if len(s) == 5 and s.isdigit():
        s = "0" + s
    if len(s) >= 4 and s[:4].isdigit():
        hh, mm = int(s[:2]), int(s[2:4])
        if 9 <= hh <= 15 and 0 <= mm <= 59:
            return hh * 60 + mm
    return None


def evaluate_turtle_quality(order: dict[str, Any], *,
                            rabbit_count: int = 1,
                            peak_seal_yi: float | None = None,
                            current_seal_yi: float | None = None) -> dict[str, Any]:
    """对二板乌龟候选进行综合买点质量评分与撤单预警（0~100 分）。

    防御性硬条件（参考历史复盘假设）：
    - 炸板次数 > 3（烂板高危）
    - 换手率超限（主板 > 22%，20cm > 35%）
    - 封单失血：盘中封单额较峰值骤降 >= 70% 时触发撤单预警（cancel_warn=True）
    """
    code = str(order.get("ts_code") or "").zfill(6)
    lim = _limit_pct(code)
    max_turn = 35.0 if lim >= 0.20 else 22.0

    broken = order.get("broken_max")
    if broken is None:
        broken = order.get("broken")
    try:
        broken_n = int(broken) if broken is not None else None
    except (TypeError, ValueError):
        broken_n = None

    turn = order.get("turn_at_seal")
    if turn is None:
        turn = order.get("turnover")
    try:
        turn_f = float(turn) if turn is not None else None
    except (TypeError, ValueError):
        turn_f = None

    first_min = _hhmmss_to_min(order.get("first_seal"))

    score = 65.0
    reasons: list[str] = []
    reject = False

    # 1) 兔子共振强度
    rc = max(0, int(rabbit_count or 0))
    if rc >= 2:
        score += 12.0
        reasons.append(f"多兔共振({rc}只) +12")
    elif rc == 1:
        score += 4.0
        reasons.append("单兔锚定 +4")

    # 2) 首封时间（早盘封板显著强于午后板）
    if first_min is not None:
        if first_min <= 9 * 60 + 40:
            score += 12.0
            reasons.append("早盘首封(≤09:40) +12")
        elif first_min <= 10 * 60 + 30:
            score += 4.0
            reasons.append("上午首封 +4")
        elif first_min >= 13 * 60:
            score -= 12.0
            reasons.append("午后弱封 -12")

    # 3) 炸板次数
    if broken_n is not None:
        if broken_n == 0:
            score += 8.0
            reasons.append("0次炸板 +8")
        elif broken_n <= 2:
            score += 2.0
            reasons.append(f"良性分歧({broken_n}次开板) +2")
        elif broken_n == 3:
            score -= 10.0
            reasons.append("多次炸板(3次) -10")
        else:
            score -= 30.0
            reject = True
            reasons.append(f"烂板高危(开板{broken_n}次>3) 建议回避")

    # 4) 换手率（非线性：适度换手最优，极高换手往往高位出货）
    if turn_f is not None:
        if 3.0 <= turn_f <= 12.0:
            score += 8.0
            reasons.append(f"健康换手({turn_f:.1f}%) +8")
        elif turn_f > max_turn:
            score -= 25.0
            reject = True
            reasons.append(f"过高换手({turn_f:.1f}%>{max_turn:.0f}%) 建议回避")
        elif turn_f > max_turn * 0.75:
            score -= 10.0
            reasons.append(f"换手偏高({turn_f:.1f}%) -10")

    # 5) 封单失血撤单预警
    cancel_warn = False
    if peak_seal_yi and peak_seal_yi > 0 and current_seal_yi is not None:
        drop_ratio = (peak_seal_yi - current_seal_yi) / peak_seal_yi
        if drop_ratio >= 0.70:
            cancel_warn = True
            score -= 20.0
            reasons.append(f"封单失血-{drop_ratio:.0%}({peak_seal_yi:.2f}→{current_seal_yi:.2f}亿) 建议撤单")

    score = max(0.0, min(100.0, round(score, 1)))
    if reject or cancel_warn or score < 40:
        grade = "REJECT"
    elif score >= 80:
        grade = "A"
    elif score >= 60:
        grade = "B"
    else:
        grade = "C"

    return {
        "grade": grade,
        "score": score,
        "cancel_warn": cancel_warn,
        "reject": reject,
        "reasons": reasons,
    }


def scan_pre_seal_candidates(theme_rabbits: dict[str, list[str]],
                             theme_members: dict[str, list[str]],
                             quotes: dict[str, dict[str, Any]],
                             *,
                             already_sealed: set[str] | None = None) -> list[dict[str, Any]]:
    """临封扫板雷达：题材内已确认一字兔时，扫描同题材尚未封死但强势冲板的候选。

    仅使用传入的真实实时行情 quotes，绝不合成数据。
    触发条件：主板涨幅 ∈ [7.0%, 9.7%)，20cm 涨幅 ∈ [14.0%, 19.4%)，且现价 ≥ 开盘价。
    """
    sealed = already_sealed or set()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for theme, rabbits in (theme_rabbits or {}).items():
        if not rabbits:
            continue
        rabbit_set = {str(r).zfill(6) for r in rabbits}
        for raw_code in theme_members.get(theme, []):
            c = str(raw_code).zfill(6)
            if c in rabbit_set or c in sealed or c in seen:
                continue
            q = quotes.get(c)
            if not q:
                continue
            pct = q.get("pct")
            px = q.get("price")
            op = q.get("open")
            if pct is None or px is None or float(px) <= 0:
                continue
            pct_f = float(pct)
            lim_pct = _limit_pct(c) * 100.0
            low_th = 0.70 * lim_pct
            high_th = 0.97 * lim_pct
            if low_th <= pct_f < high_th and (op is None or float(px) >= float(op)):
                seen.add(c)
                out.append({
                    "ts_code": c,
                    "name": q.get("name") or "",
                    "theme": theme,
                    "rabbits": sorted(rabbit_set),
                    "price": float(px),
                    "pct": round(pct_f, 2),
                    "gap_to_limit": round(lim_pct - pct_f, 2),
                    "quote_time": q.get("quote_time") or "",
                    "signal": "pre_seal_sweep",
                    "note": f"同题材有{len(rabbit_set)}只一字兔，现涨 +{pct_f:.2f}%，距涨停 {lim_pct - pct_f:.2f}%",
                })
    out.sort(key=lambda x: x["gap_to_limit"])
    return out
