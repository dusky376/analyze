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
    """复用 feature_engine.limit_pct 作为板块涨跌幅唯一事实源。"""
    try:
        from backend import feature_engine
        return float(feature_engine.limit_pct(str(code or "").strip().zfill(6)))
    except Exception:  # noqa: BLE001
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
                            current_seal_yi: float | None = None,
                            catalyst_score: int | float | None = None,
                            catalyst_weight: float = 1.0,
                            news_alert: dict[str, Any] | None = None,
                            rabbit_seal_yi: float | None = None) -> dict[str, Any]:
    """对二板乌龟候选进行综合买点质量评分与撤单预警（0~100 分）。

    防御性硬条件（反排板逆向选择）：
    - 个股硬利空雷区（减持/立案/问询/核查/暴雷）→ 直接 REJECT 回避
    - 炸板次数 > 3（烂板高危）→ REJECT
    - 换手率超限（主板 > 22%，20cm > 35%）→ REJECT
    - 封单失血：盘中封单额较峰值骤降 >= 60% 时触发撤单预警（cancel_warn=True）
    - 外盘/政策催化共振加分；大单强兔（≥1亿）加分、迷你弱兔（<0.3亿）扣分
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

    # 换手口径：严格以「二板上板前（首封瞬间）换手 turn_at_seal」为准，其次首帧换手，不用收盘总换手事后误杀
    turn = order.get("turn_at_seal")
    if turn is None:
        turn = (order.get("first") or {}).get("turnover")
    if turn is None:
        turn = order.get("turnover")
    try:
        turn_f = float(turn) if turn is not None else None
    except (TypeError, ValueError):
        turn_f = None

    first_min = _hhmmss_to_min(order.get("first_seal") or order.get("first_seal_hhmmss"))

    score = 65.0
    reasons: list[str] = []
    reject = False

    # 0) 个股硬利空雷区（减持/立案/问询/核查）一票否决
    if isinstance(news_alert, dict) and news_alert.get("risk_level") == "danger":
        tags = "、".join(news_alert.get("danger_tags") or ["硬利空"])
        score -= 35.0
        reject = True
        reasons.append(f"消息雷区({tags}) 一票回避")
    elif isinstance(news_alert, dict) and news_alert.get("is_clarification_cooling"):
        score -= 12.0
        reasons.append("异动降温/澄清否认公告 -12")

    # 0.5) 监管严重异动红线预警（逼近10日+100%/30日+200%停牌核查）
    dev = order.get("abnormal_deviation")
    if isinstance(dev, dict) and dev.get("near_severe"):
        score -= 25.0
        reject = True
        reasons.append(f"监管红线({dev.get('note') or '逼近严重异动'}) 建议回避")

    # 1) 兔子共振强度 & 兔子封单强度
    rc = max(0, int(rabbit_count or 0))
    if rc >= 2:
        score += 12.0
        reasons.append(f"多兔共振({rc}只) +12")
    elif rc == 1:
        score += 4.0
        reasons.append("单兔锚定 +4")
    if rabbit_seal_yi is not None:
        if rabbit_seal_yi >= 1.0:
            score += 6.0
            reasons.append(f"大单强兔({rabbit_seal_yi:.2f}亿) +6")
        elif 0 < rabbit_seal_yi < 0.30:
            score -= 6.0
            reasons.append(f"弱封单兔({rabbit_seal_yi:.2f}亿<0.3亿) -6")

    # 2) 首封时间（早盘封板显著强于 10:30 后弱封）
    if first_min is not None:
        if first_min <= 9 * 60 + 40:
            score += 12.0
            reasons.append("早盘首封(≤09:40) +12")
        elif first_min <= 10 * 60 + 30:
            score += 4.0
            reasons.append("上午首封 +4")
        elif first_min >= 13 * 60:
            score -= 14.0
            reasons.append("午后弱封(≥13:00) -14")
        else:
            score -= 6.0
            reasons.append("10:30后偏晚封板 -6")

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

    # 5) 美股映射 / 国内政策催化共振（克制上限 ±4 × 动态因果权重 ∈ [0, 1.5]，仅作辅助排序不凌驾盘口）
    if catalyst_score is not None:
        cs = float(catalyst_score)
        cw = max(0.0, min(1.5, float(catalyst_weight)))
        tag_w = f"(动态×{cw:.2f})" if abs(cw - 1.0) >= 0.05 else ""
        if cs >= 2:
            pts = round(4.0 * cw, 1)
            if pts > 0:
                score += pts
                reasons.append(f"外盘/政策强催化共振 +{pts:g}{tag_w}")
        elif cs >= 1:
            pts = round(2.0 * cw, 1)
            if pts > 0:
                score += pts
                reasons.append(f"题材消息催化 +{pts:g}{tag_w}")
        elif cs <= -2:
            pts = round(4.0 * cw, 1)
            if pts > 0:
                score -= pts
                reasons.append(f"外盘/政策利空承压 -{pts:g}{tag_w}")

    # 5.5) 封单绝对规模与近 5 日龙虎榜机构/顶级游资合力破平加分（只计入 raw_score 用于黄金窗口破平，保持基础 score 与客观参考语义兼容）
    tie_bonus = 0.0
    first_row = order.get("first") or {}
    seal_yuan = order.get("seal_fund")
    if seal_yuan is None:
        seal_yuan = first_row.get("seal_fund")
    try:
        seal_yi = float(seal_yuan) / 1e8 if seal_yuan is not None else None
    except (TypeError, ValueError):
        seal_yi = None
    if seal_yi is not None:
        if seal_yi >= 2.0:
            tie_bonus += 6.0
            reasons.append(f"大封单硬核({seal_yi:.2f}亿≥2亿) 破平+6")
        elif seal_yi >= 1.0:
            tie_bonus += 3.0
            reasons.append(f"亿元封单({seal_yi:.2f}亿) 破平+3")

    lhb_prof = order.get("lhb_profile")
    if isinstance(lhb_prof, dict):
        net_yi = lhb_prof.get("net_amt_yi")
        try:
            net_yi_f = float(net_yi) if net_yi is not None else None
        except (TypeError, ValueError):
            net_yi_f = None
        if net_yi_f is not None:
            if net_yi_f >= 1.0:
                tie_bonus += 6.0
                reasons.append(f"龙虎榜主力净买({net_yi_f:+.2f}亿) 破平+6")
            elif net_yi_f >= 0.3:
                tie_bonus += 3.0
                reasons.append(f"龙虎榜净买({net_yi_f:+.2f}亿) 破平+3")
        if lhb_prof.get("tag"):
            net_txt = f" 净买{net_yi_f:+.2f}亿" if net_yi_f is not None else ""
            reasons.append(f"龙虎榜参考({lhb_prof['tag']}{net_txt})")

    # 6) 封单失血撤单预警（≥60% 失血预警）
    cancel_warn = False
    if peak_seal_yi and peak_seal_yi > 0 and current_seal_yi is not None:
        drop_ratio = (peak_seal_yi - current_seal_yi) / peak_seal_yi
        if drop_ratio >= 0.60:
            cancel_warn = True
            score -= 20.0
            reasons.append(f"封单失血-{drop_ratio:.0%}({peak_seal_yi:.2f}→{current_seal_yi:.2f}亿) 建议撤单")

    raw_score = round(score + tie_bonus, 1)
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
        "raw_score": raw_score,
        "cancel_warn": cancel_warn,
        "reject": reject,
        "reasons": reasons,
    }


def rank_and_focus_candidates(
    groups: dict[str, dict[str, Any]],
    *,
    max_per_theme: int = 2,
    max_total: int = 5,
    catalyst_by_theme: dict[str, float] | None = None,
    catalyst_weight: float = 1.0,
    stock_alerts: dict[str, dict[str, Any]] | None = None,
    drop_rejected: bool = True,
    cancelled_codes: set[str] | None = None,
    invalid_codes: set[str] | None = None,
    existing_theme_counts: dict[str, int] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """乌龟前排筛选：释放撤单/脏单槽位，硬过滤在前，未截断 raw_score 与黄金5分钟卡位排序在后。"""
    if not groups:
        return {}, []
    from backend import rabbit_state as _rs
    cancel_set = {str(x).zfill(6) for x in (cancelled_codes or set())}
    invalid_set = {str(x).zfill(6) for x in (invalid_codes or set())}
    scored: list[tuple[int, int, float, float, float, int, str, dict[str, Any], dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    for code, g in groups.items():
        c6 = str(code).zfill(6)
        theme = str(g.get("guitu_theme") or g.get("industry") or "")
        if c6 in cancel_set or c6 in invalid_set:
            skipped.append({
                "ts_code": code,
                "name": g.get("name") or "",
                "theme": theme,
                "reason": "当日已撤单/异常脏单，让出同题材前排槽位给顺延候选",
                "buy_quality": None,
            })
            continue
        rabbits = g.get("guitu_rabbits") or []
        cat_sc = (catalyst_by_theme or {}).get(theme)
        alert = (stock_alerts or {}).get(c6)
        bq = evaluate_turtle_quality(
            g,
            rabbit_count=len(rabbits) if isinstance(rabbits, list) else 1,
            catalyst_score=cat_sc,
            catalyst_weight=catalyst_weight,
            news_alert=alert,
            rabbit_seal_yi=g.get("rabbit_max_seal_yi"),
        )
        # 1) 4 条硬过滤一票否决在前
        if drop_rejected and bq["reject"]:
            skipped.append({
                "ts_code": code,
                "name": g.get("name") or "",
                "theme": theme,
                "reason": "买点硬过滤回避：" + "；".join(bq["reasons"]),
                "buy_quality": bq,
            })
            continue
        # 2) 客观卡位排序：09:30~09:35 黄金5分钟内按未截断 raw_score + 封单规模破平，其余按首封先后
        fmin = _hhmmss_to_min(g.get("first_seal") or g.get("first_seal_hhmmss"))
        first_row = g.get("first") or {}
        probe_row = {
            "seal_fund": g.get("seal_fund") or first_row.get("seal_fund"),
            "amount": g.get("amount") or first_row.get("amount"),
            "turnover": g.get("turn_at_seal") or g.get("turnover") or first_row.get("turnover"),
            "float_mv": g.get("float_mv") or first_row.get("float_mv"),
        }
        s2m = _rs.seal_to_float_mv_ratio(probe_row)
        sf_yi = float(probe_row.get("seal_fund") or 0.0) / 1e8
        rank_time = fmin if fmin is not None else 9999
        # 09:30~09:35 黄金 5 分钟同一窗口内用 raw_score + 封单规模破平，防止杂毛早封 2 分钟压死中军
        time_bucket = 570 if (fmin is not None and 570 <= fmin <= 575) else rank_time
        rank_strength = float(s2m) if s2m is not None else (sf_yi * 0.01)
        raw_sc = float(bq.get("raw_score") if bq.get("raw_score") is not None else bq["score"])
        turn_v = probe_row.get("turnover")
        try:
            turn_vf = float(turn_v) if turn_v is not None else None
        except (TypeError, ValueError):
            turn_vf = None
        is_quasi_oneword = bool(
            fmin is not None and fmin <= 9 * 60 + 30
            and turn_vf is not None and turn_vf < 1.5
            and s2m is not None and float(s2m) >= 0.08
        )
        bq["quasi_oneword"] = is_quasi_oneword
        bq["positioning"] = {
            "first_seal_min": fmin,
            "time_bucket": time_bucket,
            "seal_to_fmv": s2m,
            "seal_fund_yi": round(sf_yi, 3),
            "raw_score": raw_sc,
        }
        scored.append((1 if is_quasi_oneword else 0, time_bucket, -raw_sc, -rank_strength, -sf_yi, rank_time, code, g, bq))

    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4], x[5], x[6]))
    selected: dict[str, dict[str, Any]] = {}
    theme_cnt: dict[str, int] = dict(existing_theme_counts or {})
    for _q_ord, _tb, _raw_neg, _rs_neg, _sf_neg, _rt, code, g, bq in scored:
        theme = str(g.get("guitu_theme") or g.get("industry") or "未归类")
        if len(selected) >= max_total:
            skipped.append({
                "ts_code": code, "name": g.get("name") or "", "theme": theme,
                "reason": f"聚焦前排：当日精选上限 {max_total} 只（卡位顺延，评分 {bq['raw_score']}）",
                "buy_quality": bq,
            })
            continue
        if theme_cnt.get(theme, 0) >= max_per_theme:
            skipped.append({
                "ts_code": code, "name": g.get("name") or "", "theme": theme,
                "reason": f"同题材「{theme}」已取前 {max_per_theme} 只前排乌龟，不摊大饼",
                "buy_quality": bq,
            })
            continue
        gg = dict(g)
        gg["buy_quality_pre"] = bq
        selected[code] = gg
        theme_cnt[theme] = theme_cnt.get(theme, 0) + 1
    return selected, skipped


def check_abnormal_deviation(closes: list[float] | None, *,
                             code: str | None = None) -> dict[str, Any]:
    """监管严重异动红线预警（A 股 10 日涨幅偏离 +100% / 30 日 +200% 停牌核查风险）。

    仅用真实历史收盘价（严格事前），当个股距严重异动仅差 1~2 个板时触发高危预警。
    """
    vals = [float(x) for x in (closes or []) if x is not None and float(x) > 0]
    if len(vals) < 11:
        return {"available": False, "near_severe": False, "ret_10d": None, "ret_30d": None, "note": "历史不足11日"}
    cur = vals[-1]
    r10 = cur / vals[-11] - 1.0
    r30 = (cur / vals[-31] - 1.0) if len(vals) >= 31 else None
    near_10 = r10 >= 0.85
    near_30 = bool(r30 is not None and r30 >= 1.70)
    near = near_10 or near_30
    reasons = []
    if near_10:
        reasons.append(f"近10日已涨 +{r10:.0%}（逼近+100%严重异动红线）")
    if near_30 and r30 is not None:
        reasons.append(f"近30日已涨 +{r30:.0%}（逼近+200%严重异动红线）")
    return {
        "available": True,
        "near_severe": near,
        "ret_10d": round(r10, 4),
        "ret_30d": round(r30, 4) if r30 is not None else None,
        "note": "；".join(reasons) if reasons else f"监管异动安全（10日 {r10:+.0%}）",
    }


def scan_reseal_and_weak_to_strong(
    candidates: list[dict[str, Any]],
    *,
    min_reseal_yi: float = 0.30,
) -> dict[str, list[dict[str, Any]]]:
    """除纯排板外的两类实战买点雷达（解决量化通道下强票排不进、烂板全成交的痛点）：

    1. 回封确认扫板（reseal_confirm）：曾开板分歧（broken>=1）但重新封回涨停且封单稳健（≥ min_reseal_yi）；
    2. 二进三竞价弱转强（weak_to_strong_auction）：昨日封住的换手二板，次日集合竞价高开 +2%~+7% 且竞价放量。
    """
    reseal: list[dict[str, Any]] = []
    w2s: list[dict[str, Any]] = []
    for c in (candidates or []):
        code = str(c.get("ts_code") or "").zfill(6)
        if not code or code == "000000":
            continue
        lim_pct = _limit_pct(code) * 100.0
        broken = int(c.get("broken") or 0)
        sealed_now = bool(c.get("sealed_now"))
        seal_yi = float(c.get("seal_fund_yi") or 0.0)
        if sealed_now and broken >= 1 and broken <= 2 and seal_yi >= min_reseal_yi:
            reseal.append({
                "ts_code": code,
                "name": c.get("name") or "",
                "signal": "reseal_confirm",
                "broken": broken,
                "seal_fund_yi": round(seal_yi, 3),
                "note": f"分歧回封确认（开板{broken}次后回封，封单 {seal_yi:.2f} 亿 ≥ {min_reseal_yi:.2f} 亿）",
            })
        open_gap = c.get("open_gap")
        auc_ratio = c.get("auction_vol_ratio")  # 竞价成交额 / 昨日成交额
        if open_gap is not None and bool(c.get("prev_sealed")):
            og = float(open_gap)
            lo_g, hi_g = 0.20 * lim_pct, 0.70 * lim_pct  # 主板 +2%~+7%，20cm +4%~+14%
            if lo_g <= og <= hi_g and (auc_ratio is None or float(auc_ratio) >= 0.05):
                vol_unver = auc_ratio is None
                w2s.append({
                    "ts_code": code,
                    "name": c.get("name") or "",
                    "signal": "weak_to_strong_auction",
                    "open_gap": round(og, 2),
                    "auction_vol_ratio": round(float(auc_ratio), 4) if auc_ratio is not None else None,
                    "vol_unverified": vol_unver,
                    "note": (
                        f"竞价弱转强（高开 +{og:.2f}%，竞价量比 {float(auc_ratio):.1%} ≥ 5% 确认放量）"
                        if not vol_unver else
                        f"竞价弱转强观察（高开 +{og:.2f}%，缺竞价量能核验 vol_unverified）"
                    ),
                })
    reseal.sort(key=lambda x: -x["seal_fund_yi"])
    w2s.sort(key=lambda x: -x["open_gap"])
    return {"reseal_confirm": reseal, "weak_to_strong_auction": w2s}


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


def scan_first_to_second_sweep(
    theme_rabbits: dict[str, list[str]],
    prev_first_board_by_theme: dict[str, list[str]],
    quotes: dict[str, dict[str, Any]],
    *,
    already_sealed: set[str] | None = None,
    max_ask1_fmv_ratio: float = 0.0020,
    max_ask1_amt_ratio: float = 0.020,
    fallback_ask1_yuan: float = 6_000_000.0,
) -> list[dict[str, Any]]:
    """昨日首板池 → 今日一进二临封扫板双轨雷达（影子 + 可执行扫板触发）。

    只扫描「昨日真实首板（prev_streak==1）且与今日 09:25 一字二板兔子同题材」的未封板标的：
      1. 涨幅进入临封冲板窗口：主板 [+7.5%, 涨停前)、20cm [+15.0%, 涨停前)；
      2. 盘中强势承接：现价 ≥ 开盘价，且（有均价线时）现价 ≥ 均价线×0.995，换手健康（主板≤22%/20cm≤35%）；
      3. 五档薄卖一吃单加速（executable=True）：
         涨幅 ≥ 0.85×涨停幅度、卖一距涨停 ≤ 2 档（ask1 ≥ lim_px - 0.025）且卖一挂单薄（自适应流通市值/成交额门槛）。
         缺少五档 ask1_vol 时仅记为观察/影子（executable=False），绝不编造五档。
    """
    from backend import rabbit_state as _rs

    sealed = {str(x).zfill(6) for x in (already_sealed or set())}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for theme, rabbits in (theme_rabbits or {}).items():
        if not rabbits:
            continue
        rabbit_set = {str(r).zfill(6) for r in rabbits if str(r).strip()}
        for raw_code in (prev_first_board_by_theme or {}).get(theme, []):
            c = str(raw_code).zfill(6)
            if c in rabbit_set or c in sealed or c in seen:
                continue
            q = quotes.get(c)
            if not q:
                continue
            px = float(q.get("price") or 0.0)
            pc = float(q.get("prev_close") or 0.0)
            op = float(q.get("open") or 0.0)
            pct_raw = q.get("pct")
            if px <= 0:
                continue
            if pct_raw is not None:
                pct_f = float(pct_raw)
            elif pc > 0:
                pct_f = (px / pc - 1.0) * 100.0
            else:
                continue

            lim = _limit_pct(c)
            lim_pct = lim * 100.0
            low_th = 0.75 * lim_pct
            high_th = 0.99 * lim_pct
            if not (low_th <= pct_f < high_th):
                continue
            if op > 0 and px < op:
                continue
            vwap = q.get("avg_price") or q.get("vwap")
            if vwap is not None and float(vwap) > 0 and px < float(vwap) * 0.995:
                continue
            max_turn = 35.0 if lim >= 0.20 else 22.0
            turn = q.get("turnover")
            if turn is not None:
                try:
                    if float(turn) > max_turn:
                        continue
                except (TypeError, ValueError):
                    pass

            ask1 = float(q.get("ask1") or 0.0)
            ask1_vol = float(q.get("ask1_vol") or 0.0)
            lim_px = round(pc * (1.0 + lim), 2) if pc > 0 else round(px * (1.0 + (lim_pct - pct_f) / 100.0), 2)
            ask1_amt = (ask1 * ask1_vol * 100.0) if (ask1 > 0 and ask1_vol > 0) else None
            fmv = _rs.estimate_float_mv_yuan(q)
            day_amt = float(q.get("amount") or 0.0)
            if fmv and fmv > 0:
                thin_limit = fmv * max_ask1_fmv_ratio
            elif day_amt > 0:
                thin_limit = day_amt * max_ask1_amt_ratio
            else:
                thin_limit = fallback_ask1_yuan

            executable = bool(
                ask1_amt is not None
                and pct_f >= 0.85 * lim_pct
                and ask1 >= lim_px - 0.025
                and ask1_amt <= thin_limit
            )
            seen.add(c)
            ask_note = (
                f"，卖一 {ask1:.2f} 挂单 {ask1_amt / 1e4:.0f}万"
                + ("（薄卖一吃单加速·可扫板）" if executable else "")
                if ask1_amt is not None else "（待五档薄卖一确认）"
            )
            out.append({
                "ts_code": c,
                "name": str(q.get("name") or ""),
                "theme": theme,
                "rabbits": sorted(rabbit_set),
                "price": px,
                "limit_price": lim_px,
                "pct": round(pct_f, 2),
                "gap_to_limit": round(lim_pct - pct_f, 2),
                "turnover": round(float(turn), 2) if turn is not None else None,
                "ask1": ask1 if ask1 > 0 else None,
                "ask1_amt_wan": round(ask1_amt / 1e4, 1) if ask1_amt is not None else None,
                "thin_limit_wan": round(thin_limit / 1e4, 1),
                "executable": executable,
                "exec_mode": "pre_seal_sweep" if executable else "sweep_shadow",
                "quote_time": str(q.get("quote_time") or ""),
                "signal": "pre_seal_sweep",
                "prev_streak": 1,
                "note": (
                    f"昨日首板一进二临封（同题材{len(rabbit_set)}只一字兔），现涨 +{pct_f:.2f}%，"
                    f"距涨停 {lim_pct - pct_f:.2f}%{ask_note}"
                ),
            })
    out.sort(key=lambda x: (0 if x["executable"] else 1, x["gap_to_limit"]))
    return out
