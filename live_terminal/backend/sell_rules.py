"""卖出决策（分钟级触发 + 时间兜底）· 用户 2026-11-25 裁定。

用户原话
--------
> 第一，当天买入的股票炸板了未回封，卖出时间为第二天十点前卖出，
> 卖点为快速下跌卖出或者冲高回落也卖出，买入当天没炸板的卖出时间为
> 断板当天收盘前，卖点为快速下跌或者冲高回落

两条卖出路径（按「买入当天收盘时是否仍炸板」分流）
------------------------------------------------
A. ``broke_and_stayed``（买入日炸板且收盘未回封）
     → 卖出窗口：**次一交易日 10:00 之前**
     → 窗口内触发「快速下跌 / 冲高回落」即卖；未触发则按 10:00 前最后价兜底。

B. ``sealed``（买入日收盘封住涨停，未炸板）
     → 卖出窗口：**首个断板日收盘之前**
     → 窗口内触发信号即卖；未触发则按断板日收盘价兜底（现行规则）。

判据：**收盘时仍是炸板状态**才算 A（用户 2026-11-25 明确）。
盘中开板但收盘回封的，属 B。

两个盘中信号（阈值可调，默认保守）
--------------------------------
1. **快速下跌**：任一分钟收盘价相对「该分钟前 N 分钟的最高价」下跌 ≥ ``DROP_PCT``
   （默认 2%）。追的是「一根急杀」，不是缓慢阴跌。
2. **冲高回落**：当日最高价回撤 ≥ ``PULLBACK_PCT``（默认 3%），且回撤发生在
   盘中（不是开盘就低开）。追的是「冲高后掉头」。

诚实边界
--------
* 分钟数据是**腾讯 1 分钟**（该分钟最后成交价），不是逐笔；
  触发时刻最多滞后 1 分钟，成交价用该分钟收盘价近似（不插值）。
* 历史分钟数据腾讯只给近期（约 5 个交易日），更早的只能靠
  ``data/live/<day>/quotes`` 的 5 分钟留档；两者都取不到 → 返回不可判定，
  **不用日线硬凑**（日线看不出「快速下跌」）。
* 时间边界卖出（10:00 / 收盘）是**兜底**，不是首选：有信号优先信号。
"""
from __future__ import annotations

from typing import Any

#: 快速下跌：相对前 N 分钟最高价的跌幅阈值
DROP_PCT = 0.02
#: 快速下跌回看窗口（分钟）
DROP_LOOKBACK = 5
#: 冲高回落：当日最高价回撤阈值
PULLBACK_PCT = 0.03
#: 兜底卖出时刻（A 路径：次日 10:00 前）
FALLBACK_TIME_A = "10:00"


def classify_entry_day(broken_times: int | None,
                       still_sealed_at_close: bool | None) -> str:
    """买入日归类：``broke_and_stayed`` / ``sealed`` / ``unknown``。

    Args:
        broken_times: 买入日全天开板次数（>0 表示炸过板）
        still_sealed_at_close: 收盘时是否仍封在涨停（True=回封住了）

    用户 2026-11-25 裁定：**必须收盘时还是炸板状态**才算「炸板未回封」。
    所以 broken_times>0 但收盘回封 → 归 ``sealed``（走断板收盘卖）。
    """
    if still_sealed_at_close is None:
        return "unknown"
    if still_sealed_at_close:
        return "sealed"
    # 收盘未封：再看是否炸过板（全天从没开板却没封住 → 也算未封，按 A 处理）
    return "broke_and_stayed"


def find_signal(points: list[dict[str, Any]],
                *, drop_pct: float = DROP_PCT,
                lookback: int = DROP_LOOKBACK,
                pullback_pct: float = PULLBACK_PCT,
                start: str | None = None,
                end: str | None = None,
                ) -> dict[str, Any] | None:
    """在分钟序列里找第一个「快速下跌 / 冲高回落」触发点。

    Args:
        points: ``[{"t": "HH:MM", "price": float}, ...]``（按时间升序）
        start / end: 可选的窗口边界（含），'HH:MM'

    Returns:
        ``{"t","price","kind","detail"}`` 或 None（无触发）。
        ``kind`` ∈ ``fast_drop`` / ``pullback``。
    """
    if not points:
        return None
    win = [p for p in points
           if (start is None or str(p.get("t")) >= start)
           and (end is None or str(p.get("t")) <= end)]
    if not win:
        return None

    peak = None
    for i, p in enumerate(win):
        try:
            px = float(p["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if px <= 0:
            continue
        # 1) 快速下跌：相对前 lookback 分钟最高价
        if i > 0:
            prev = win[max(0, i - lookback):i]
            prev_hi = max((float(q["price"]) for q in prev
                           if q.get("price") is not None), default=None)
            if prev_hi and prev_hi > 0 and (prev_hi - px) / prev_hi >= drop_pct:
                return {
                    "t": p.get("t"), "price": px, "kind": "fast_drop",
                    "detail": (f"{p.get('t')} 快速下跌：{prev_hi:.2f} → {px:.2f}，"
                               f"跌幅 {(prev_hi - px) / prev_hi:.2%} ≥ {drop_pct:.0%}"),
                }
        # 2) 冲高回落：从当日最高回撤
        peak = px if peak is None else max(peak, px)
        if peak and peak > 0 and (peak - px) / peak >= pullback_pct:
            return {
                "t": p.get("t"), "price": px, "kind": "pullback",
                "detail": (f"{p.get('t')} 冲高回落：高点 {peak:.2f} → {px:.2f}，"
                           f"回撤 {(peak - px) / peak:.2%} ≥ {pullback_pct:.0%}"),
            }
    return None


def decide(entry_kind: str,
           *,
           points: list[dict[str, Any]] | None,
           fallback_price: float | None = None,
           fallback_time: str | None = None,
           drop_pct: float = DROP_PCT,
           pullback_pct: float = PULLBACK_PCT,
           open_gap_pct: float | None = None,
           rabbit_strong: bool | None = None,
           rabbit_nuclear: bool = False,
           peer_turtle_strong: bool = False,
           ) -> dict[str, Any]:
    """给出一次卖出决策（融入兔子强弱松紧、同题材其他乌龟带动与炸板竞价直走细化）。

    Args:
        entry_kind: ``broke_and_stayed`` / ``sealed`` / ``unknown``
        points: 卖出窗口内的分钟序列（已按窗口裁好）
        fallback_price: 窗口末兜底价（10:00 前最后价 / 断板日收盘价）
        fallback_time: 兜底时刻说明
        open_gap_pct: 本炸板乌龟当日集合竞价涨跌幅（%），用于炸板路径判定是否竞价直走
        rabbit_strong: 兔子次日/断板日是否继续走强上板、无负反馈（True=放宽/观察，False=收紧）
        rabbit_nuclear: 兔子是否核按钮/负反馈（True=立刻收紧阈值或炸板竞价直走）
        peer_turtle_strong: 同题材另外的乌龟次日是否走强带动（True=继续观察到 10:00，不竞价一刀切）

    Returns:
        ``{"action":"sell","price":float,"reason":str,"kind":"signal|auction_cut|fallback|undecidable"}``
    """
    if entry_kind == "unknown":
        return {"action": "hold", "price": None, "kind": "undecidable",
                "reason": "买入日是否炸板不可判定（缺 broken/封板状态），不猜"}

    # 1) 路径 A（买入日炸板未回封）竞价细化：
    #    仅当「本炸板乌龟竞价弱(≤-3%)」且「兔子未继续走强/负反馈」且「同题材其他乌龟也未走强」时，开盘直走；
    #    只要三者任一走强（兔子继续走强无负反馈 / 本炸板乌龟竞价强承接 / 同题材其他乌龟走强）→ 继续走 10:00 观察窗口
    if entry_kind == "broke_and_stayed" and open_gap_pct is not None:
        weak_auction = float(open_gap_pct) <= -3.0
        rabbit_bad = bool(rabbit_nuclear or rabbit_strong is False)
        if weak_auction and rabbit_bad and not peer_turtle_strong:
            open_px = None
            if points:
                try:
                    open_px = float(points[0]["price"])
                except (KeyError, TypeError, ValueError):
                    open_px = None
            if open_px is None and fallback_price is not None and fallback_price > 0:
                open_px = float(fallback_price)
            if open_px and open_px > 0:
                return {
                    "action": "sell",
                    "price": open_px,
                    "kind": "auction_cut",
                    "signal_time": (points[0].get("t") if points else "09:30"),
                    "reason": (
                        f"炸板路径竞价直走：本炸板乌龟低开 {float(open_gap_pct):+.2f}%、"
                        + ("兔子核按钮负反馈" if rabbit_nuclear else "兔子未继续走强")
                        + "且同题材其他乌龟无带动 → 开盘直接止损，不观察到 10:00"
                    ),
                }

    # 2) 路径 B（买入日封住）断板日阈值随兔子强弱动态松紧（兔子强防洗盘，兔子核按钮立刻收紧）
    eff_drop = float(drop_pct)
    eff_pull = float(pullback_pct)
    adj_note = ""
    if entry_kind == "sealed":
        if rabbit_nuclear:
            eff_drop = round(eff_drop * 0.70, 4)
            eff_pull = round(eff_pull * 0.70, 4)
            adj_note = f"（兔子核按钮收紧至急跌{eff_drop:.1%}/回撤{eff_pull:.1%}）"
        elif rabbit_strong is True:
            eff_drop = round(eff_drop * 1.35, 4)
            eff_pull = round(eff_pull * 1.35, 4)
            adj_note = f"（兔子仍强放宽至急跌{eff_drop:.1%}/回撤{eff_pull:.1%}防洗盘）"

    sig = find_signal(points or [], drop_pct=eff_drop, pullback_pct=eff_pull)
    if sig:
        return {"action": "sell", "price": sig["price"], "kind": "signal",
                "reason": sig["detail"] + adj_note, "signal_time": sig["t"]}

    if fallback_price is not None and fallback_price > 0:
        return {"action": "sell", "price": float(fallback_price),
                "kind": "fallback",
                "reason": (f"窗口内无「快速下跌/冲高回落」信号{adj_note} → 按"
                           f"{fallback_time or '窗口末'}兜底价卖出")}
    return {"action": "hold", "price": None, "kind": "undecidable",
            "reason": "既无触发信号也无兜底价，保持持有（不伪造价格）"}


# ---------------------------------------------------------------------------
# 市场自适应卖点引擎（2026-11-25 进阶版：板块波动率 + VWAP/量能 + 锚定兔子联动）
# ---------------------------------------------------------------------------

def _board_limit_pct(code: str | None) -> float:
    """按股票代码返回涨跌幅上限（复用 feature_engine.limit_pct 单一事实源）。"""
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


def adaptive_thresholds(code: str | None = None, *,
                        atr5: float | None = None,
                        rabbit_weak: bool = False,
                        ice_level: str | None = None) -> dict[str, float]:
    """根据板块涨跌幅上限、近 5 日波动率、锚定兔子强弱与市场冰点动态计算卖点阈值。

    相比固定 2%/3% 的改进：
    1. 20cm/30cm 标的不再被 2% 常规噪声误杀；
    2. 锚定一字兔若盘中炸板/核按钮（rabbit_weak=True），阈值自动收紧 40% 保命；
    3. 市场冰点（L4/L5）下自动收紧 15%–25%，防连续核按钮。
    """
    lim = _board_limit_pct(code)
    base_drop = 0.20 * lim       # 主板 2.0%，20cm 4.0%，北交 6.0%
    base_pull = 0.30 * lim       # 主板 3.0%，20cm 6.0%，北交 9.0%
    if atr5 is not None and atr5 > 0:
        base_drop = max(base_drop, min(0.45 * lim, 0.25 * float(atr5)))
        base_pull = max(base_pull, min(0.60 * lim, 0.38 * float(atr5)))

    tighten = 1.0
    if rabbit_weak:
        tighten *= 0.60          # 锚定兔子走弱 → 立即收紧 40%
    if ice_level == "L5":
        tighten *= 0.75
    elif ice_level == "L4":
        tighten *= 0.85

    drop_pct = round(max(0.012, base_drop * tighten), 4)
    pullback_pct = round(max(0.018, base_pull * tighten), 4)
    min_surge_pct = round(max(0.015, 0.18 * lim * tighten), 4)
    return {
        "limit_pct": lim,
        "drop_pct": drop_pct,
        "pullback_pct": pullback_pct,
        "min_surge_pct": min_surge_pct,
        "tighten_factor": round(tighten, 3),
    }


def _hhmm_min(t: str) -> int | None:
    s = str(t or "").strip()
    if len(s) >= 5 and s[2] == ":" and s[:2].isdigit() and s[3:5].isdigit():
        return int(s[:2]) * 60 + int(s[3:5])
    return None


def find_adaptive_signal(points: list[dict[str, Any]], *,
                         code: str | None = None,
                         prev_close: float | None = None,
                         open_price: float | None = None,
                         entry_kind: str = "sealed",
                         atr5: float | None = None,
                         rabbit_weak: bool = False,
                         ice_level: str | None = None,
                         lookback: int = DROP_LOOKBACK,
                         start: str | None = None,
                         end: str | None = None,
                         ) -> dict[str, Any] | None:
    """市场自适应盘中卖点扫描（支持 VWAP 均价线、量能脉冲、竞价抢跑与尾盘防核按钮）。

    修复原版 find_signal 的 4 个缺陷：
      1. 冲高回落用全天真实最高价 full_peak，不再被窗口裁断；
      2. 必须真正「冲高」（相对昨收或开盘涨幅 ≥ min_surge_pct）才触发冲高回落；
      3. 有量能/VWAP 时要求放量破均价线确认急杀（缩量洗盘不误杀），且按真实分钟差回看；
      4. 路径 A 竞价大幅低开（≤ -5%）直接 09:30 抢跑，路径 B 14:40 未涨停且弱于 VWAP 提前规避尾盘核按钮。
    """
    if not points:
        return None
    th = adaptive_thresholds(code, atr5=atr5, rabbit_weak=rabbit_weak,
                             ice_level=ice_level)
    drop_pct = th["drop_pct"]
    pullback_pct = th["pullback_pct"]
    min_surge_pct = th["min_surge_pct"]
    lim = th["limit_pct"]

    # 整理全天有效点并同步追踪全天最高价（不因 start/end 裁窗而丢失高点）
    valid_all: list[dict[str, Any]] = []
    for p in points:
        try:
            px = float(p["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if px <= 0:
            continue
        valid_all.append(p)
    if not valid_all:
        return None

    op = float(open_price) if (open_price and open_price > 0) else float(valid_all[0]["price"])
    pc = float(prev_close) if (prev_close and prev_close > 0) else None
    zt_approx = (pc * (1.0 + lim - 0.005)) if pc else None

    # 0) 路径 A（昨日炸板未回封）竞价核按钮抢跑：大幅低开直接 09:30 抢跑，不死等 10:00
    if entry_kind == "broke_and_stayed" and pc and pc > 0:
        open_pct = (op - pc) / pc
        if open_pct <= -(0.50 * lim):  # 主板 ≤ -5%，20cm ≤ -10%
            first_t = str(valid_all[0].get("t") or "09:30")
            if (start is None or first_t >= start) and (end is None or first_t <= end):
                return {
                    "t": first_t,
                    "price": float(valid_all[0]["price"]),
                    "kind": "auction_dump",
                    "thresholds": th,
                    "detail": (f"{first_t} 竞价大幅低开 {open_pct:.2%}（炸板次日弱承接）→ "
                               "开盘抢跑，规避核按钮"),
                }

    full_peak: float | None = None
    vols: list[float] = []
    for i, p in enumerate(valid_all):
        t = str(p.get("t") or "")
        cur_m = _hhmm_min(t)
        px = float(p["price"])
        avg_px = p.get("avg_price")
        avg_px_f = float(avg_px) if (avg_px is not None and float(avg_px) > 0) else None
        vol = p.get("volume")
        vol_f = float(vol) if (vol is not None and float(vol) >= 0) else None

        full_peak = px if full_peak is None else max(full_peak, px)
        if vol_f is not None:
            vols.append(vol_f)

        if (start is not None and t < start) or (end is not None and t > end):
            continue

        # 0.5) 路径 B 次日不及预期低开抢跑/反抽无力离场
        # - 显著低开 ≤ -3%（主板，20cm ≤ -6%）：09:35 后反抽仍弱于开盘与均价线立即止损（兼容历史测试）；
        #   极端核按钮低开 ≤ -5%（主板）则 09:30 直接竞价抢跑，不等 09:35。
        # - 温和低开 -2% ~ -3%（主板）：09:33 后仍弱于开盘与均价线即走，不拖到尾盘埋成跌停。
        if entry_kind == "sealed" and pc and pc > 0:
            open_pct = (op - pc) / pc
            if open_pct <= -(0.50 * lim) and t >= "09:30":
                return {
                    "t": t, "price": px, "kind": "weak_open_exit",
                    "thresholds": th,
                    "detail": (f"{t} 次日核按钮低开 {open_pct:.2%}（严重不及预期）→ "
                               "竞价直接抢跑，避免封死跌停"),
                }
            if open_pct <= -(0.30 * lim) and t >= "09:35" and px < op * 0.995:
                if avg_px_f is None or px < avg_px_f:
                    return {
                        "t": t, "price": px, "kind": "weak_open_exit",
                        "thresholds": th,
                        "detail": (f"{t} 次日大幅低开 {open_pct:.2%}（不及预期）且反抽无力"
                                   f"（现价 {px:.2f} < 开盘 {op:.2f}），提前止损防核按钮"),
                    }
            if -(0.30 * lim) < open_pct <= -(0.20 * lim) and t >= "09:33" and px < op * 0.995:
                if avg_px_f is not None and px < avg_px_f:
                    return {
                        "t": t, "price": px, "kind": "weak_open_exit",
                        "thresholds": th,
                        "detail": (f"{t} 次日低开 {open_pct:.2%} 且破开盘/均价线"
                                   f"（现价 {px:.2f} < 均价 {avg_px_f:.2f}），弱票不拖延止损"),
                    }

        # 1) 放量/破均价线确认的快速下跌（按真实分钟差 ≤ lookback 过滤；
        #    09:30-09:35 洗盘滤波 + 强势高开且运行在均价线上的票 09:30-09:50 放宽 1.5× 防洗飞大肉）
        if i > 0:
            raw_prev = valid_all[max(0, i - lookback):i]
            if cur_m is not None:
                prev = [q for q in raw_prev
                        if (_hhmm_min(str(q.get("t") or "")) is None
                            or 0 < cur_m - _hhmm_min(str(q.get("t") or "")) <= lookback)]
            else:
                prev = raw_prev
            prev_hi = max((float(q["price"]) for q in prev), default=0.0)
            drop_r = (prev_hi - px) / prev_hi if prev_hi > 0 else 0.0
            in_open_churn = ("09:30" <= t <= "09:35")
            in_early_turnover = ("09:30" <= t <= "09:45")
            open_pct_cur = ((op - pc) / pc) if (pc and pc > 0) else 0.0
            flat_or_mild_open = bool(
                entry_kind == "sealed" and pc and pc > 0
                and -(0.20 * lim) < open_pct_cur < (0.30 * lim)
            )
            strong_above_vwap = bool(
                entry_kind == "sealed" and pc and pc > 0
                and open_pct_cur >= (0.10 * lim)
                and avg_px_f is not None and px >= avg_px_f
                and ("09:30" <= t <= "09:50")
            )
            eff_drop = drop_pct * 1.6 if in_open_churn else (drop_pct * 1.5 if strong_above_vwap else drop_pct)
            if drop_r >= eff_drop:
                below_vwap = (avg_px_f is None) or (px < avg_px_f)
                avg_v10 = (sum(vols[-11:-1]) / max(1, len(vols[-11:-1]))) if len(vols) >= 4 else None
                vol_surge = (vol_f is None) or (avg_v10 is None) or (avg_v10 <= 0) or (vol_f >= 1.5 * avg_v10)
                # 09:30~09:45 路径 B 红盘(≥+2%)且稳在均价线上方时，严禁 or vol_surge 绕过 VWAP 护栏把换手牛股卖飞
                red_above_vwap_early = bool(
                    entry_kind == "sealed" and in_early_turnover
                    and avg_px_f is not None and not below_vwap
                    and pc and pc > 0 and px >= pc * 1.02
                )
                # 平开/小低开(-2%~+3%)开盘前 5 分钟避免首根分钟 K 线秒割，等 09:35 承接确认
                in_pre5_buffer = bool(flat_or_mild_open and ("09:30" <= t < "09:35"))
                if in_pre5_buffer:
                    trigger_fd = bool(below_vwap and drop_r >= eff_drop * 1.6)
                elif red_above_vwap_early:
                    trigger_fd = bool(below_vwap and vol_surge) or (drop_r >= eff_drop * 1.8 and below_vwap)
                else:
                    trigger_fd = bool(below_vwap or drop_r >= eff_drop * 1.4 or vol_surge)
                if trigger_fd:
                    return {
                        "t": t, "price": px, "kind": "fast_drop",
                        "thresholds": th,
                        "detail": (f"{t} 自适应急跌：{prev_hi:.2f} → {px:.2f} "
                                   f"(-{drop_r:.2%} ≥ {eff_drop:.2%}"
                                   + ("，跌破均价线" if (avg_px_f and px < avg_px_f) else "")
                                   + ("，早盘洗盘滤波后确认" if in_open_churn else "") + ")"),
                    }

        # 2) 真·冲高回落：必须先有实质冲高，再从全天最高回撤
        #    P0-3 护栏：路径 B（昨日封住）早盘 09:30–10:30 换手晋级期，若现价仍强于昨收+2%（或开盘价）
        #    且运行在 VWAP 均价线上方，绝不因单纯 3% 换手回踩把 4~6 板大妖股洗飞；
        #    09:30–09:35 剧烈换手窗阈值放宽 ×1.6 且必须跌破均价线才确认回落卖点。
        surge_ref = min(op, pc) if pc else op
        surged = (full_peak - surge_ref) / surge_ref >= min_surge_pct if surge_ref > 0 else True
        if surged and full_peak and full_peak > 0:
            pb_r = (full_peak - px) / full_peak
            in_open_churn = ("09:30" <= t <= "09:35")
            in_morning_turnover = (entry_kind == "sealed" and "09:30" <= t <= "10:30")
            above_vwap_strong = bool(
                in_morning_turnover
                and avg_px_f is not None and px >= avg_px_f
                and ((pc and px >= pc * 1.02) or (op and px >= op))
            )
            eff_pb = pullback_pct * 1.6 if (in_open_churn and entry_kind == "sealed") else pullback_pct
            if pb_r >= eff_pb and not above_vwap_strong:
                if not (in_open_churn and entry_kind == "sealed" and avg_px_f is not None and px >= avg_px_f):
                    return {
                        "t": t, "price": px, "kind": "pullback",
                        "thresholds": th,
                        "detail": (f"{t} 冲高回落：日内高点 {full_peak:.2f} → {px:.2f} "
                                   f"(回撤 {pb_r:.2%} ≥ {eff_pb:.2%}"
                                   + ("，跌破均价线" if (avg_px_f is not None and px < avg_px_f) else "") + ")"),
                    }

        # 3) 路径 B 尾盘防核按钮（14:40 后未涨停且弱于均价线）
        if entry_kind == "sealed" and t >= "14:40":
            not_zt = (zt_approx is None) or (px < zt_approx)
            weak_vs_vwap = (avg_px_f is not None and px < avg_px_f * 0.995)
            weak_vs_pc = (pc is not None and px < pc * 0.98)
            if not_zt and (weak_vs_vwap or weak_vs_pc):
                return {
                    "t": t, "price": px, "kind": "late_weak_exit",
                    "thresholds": th,
                    "detail": f"{t} 尾盘未回封且弱于均价线（{px:.2f}），提前离场防次日核按钮",
                }
    return None


def find_partial_profit_signal(points: list[dict[str, Any]], *,
                               code: str | None = None,
                               prev_close: float | None = None,
                               end: str | None = None) -> dict[str, Any] | None:
    """分批锁利信号：盘中冲高 ≥ 60% 涨停幅度（主板≥+6%）未封板，回落破均价线或回撤≥2% 建议先锁半仓。"""
    if not points or not prev_close or prev_close <= 0:
        return None
    lim = _board_limit_pct(code)
    pc = float(prev_close)
    target_surge = 0.60 * lim     # 主板 +6.0%，20cm +12.0%
    zt_approx = pc * (1.0 + lim - 0.005)
    peak = 0.0
    for p in points:
        t_str = str(p.get("t") or "")
        if end and t_str and t_str > end:
            break
        try:
            px = float(p["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if px <= 0:
            continue
        peak = max(peak, px)
        if (peak - pc) / pc < target_surge or px >= zt_approx:
            continue
        avg_px = p.get("avg_price")
        avg_f = float(avg_px) if (avg_px is not None and float(avg_px) > 0) else None
        pb = (peak - px) / peak if peak > 0 else 0.0
        if pb >= 0.02 and (avg_f is None or px < avg_f):
            return {
                "action": "partial_sell",
                "ratio": 0.50,
                "t": t_str,
                "price": px,
                "kind": "partial_take_profit",
                "detail": (f"{t_str} 冲高至 +{(peak/pc - 1):.2%} 未封板并回落至 {px:.2f} → "
                           "建议先兑现 50% 锁利，余仓博连板/断板"),
            }
    return None


def decide_adaptive(entry_kind: str, *,
                    points: list[dict[str, Any]] | None,
                    code: str | None = None,
                    prev_close: float | None = None,
                    open_price: float | None = None,
                    atr5: float | None = None,
                    rabbit_weak: bool = False,
                    rabbit_strong: bool | None = None,
                    rabbit_nuclear: bool = False,
                    peer_turtle_strong: bool = False,
                    open_gap_pct: float | None = None,
                    ice_level: str | None = None,
                    fallback_price: float | None = None,
                    fallback_time: str | None = None,
                    start: str | None = None,
                    end: str | None = None) -> dict[str, Any]:
    """自适应卖出决策封装（信号优先，融入炸板竞价直走与兔子强弱动态松紧，未触发走时间兜底；附分批锁利建议）。"""
    if entry_kind == "unknown":
        return {"action": "hold", "price": None, "kind": "undecidable",
                "reason": "买入日是否炸板不可判定，不猜"}

    pts = points or []
    eff_open_gap = open_gap_pct
    if eff_open_gap is None and open_price and prev_close and float(prev_close) > 0:
        eff_open_gap = (float(open_price) - float(prev_close)) / float(prev_close) * 100.0

    # P0-3 路径 A（买入日炸板未回封）竞价细化：
    # 仅当「本炸板乌龟低开 ≤-3%」且「兔子未继续走强/负反馈」且「同题材其他乌龟无带动」时竞价直走；
    # 任一走强则继续走 10:00 观察窗口
    if entry_kind == "broke_and_stayed" and eff_open_gap is not None:
        weak_auc = float(eff_open_gap) <= -3.0
        rabbit_bad = bool(rabbit_nuclear or rabbit_strong is False)
        if weak_auc and rabbit_bad and not peer_turtle_strong:
            open_px_eff = float(open_price) if (open_price and float(open_price) > 0) else None
            if open_px_eff is None and pts and pts[0].get("price"):
                open_px_eff = float(pts[0]["price"])
            if open_px_eff is None and fallback_price and float(fallback_price) > 0:
                open_px_eff = float(fallback_price)
            if open_px_eff and open_px_eff > 0:
                return {
                    "action": "sell",
                    "price": open_px_eff,
                    "kind": "auction_cut",
                    "signal_time": (str(pts[0].get("t")) if pts else "09:30"),
                    "partial_signal": None,
                    "reason": (
                        f"炸板路径竞价直走：本炸板乌龟低开 {float(eff_open_gap):+.2f}%、"
                        + ("兔子核按钮负反馈" if rabbit_nuclear else "兔子未继续走强")
                        + "且同题材其他乌龟无带动 → 开盘直接止损，不观察到 10:00"
                    ),
                }

    eff_rabbit_weak = bool(rabbit_weak or rabbit_nuclear or (rabbit_strong is False))
    # 路径 B 断板日按兔子强弱动态松紧（兔子仍强放宽 atr5 代理容错，兔子核按钮走 eff_rabbit_weak 收紧）
    eff_atr5 = atr5
    if entry_kind == "sealed" and rabbit_strong is True and not rabbit_nuclear:
        eff_atr5 = (float(atr5) * 1.35) if (atr5 is not None and float(atr5) > 0) else 0.09

    partial = find_partial_profit_signal(pts, code=code, prev_close=prev_close, end=end)
    sig = find_adaptive_signal(
        pts, code=code, prev_close=prev_close, open_price=open_price,
        entry_kind=entry_kind, atr5=eff_atr5, rabbit_weak=eff_rabbit_weak,
        ice_level=ice_level, start=start, end=end,
    )
    if sig:
        return {
            "action": "sell",
            "price": sig["price"],
            "kind": sig["kind"],
            "signal_time": sig["t"],
            "thresholds": sig.get("thresholds"),
            "partial_signal": partial,
            "reason": sig["detail"],
        }
    if fallback_price is not None and fallback_price > 0:
        return {
            "action": "sell",
            "price": float(fallback_price),
            "kind": "fallback",
            "thresholds": adaptive_thresholds(code, atr5=eff_atr5,
                                              rabbit_weak=eff_rabbit_weak,
                                              ice_level=ice_level),
            "partial_signal": partial,
            "reason": f"窗口内未触发自适应卖点 → 按{fallback_time or '窗口末'}兜底价卖出",
        }
    return {"action": "hold", "price": None, "kind": "undecidable",
            "partial_signal": partial,
            "reason": "既无触发信号也无兜底价，保持持有（不伪造价格）"}
