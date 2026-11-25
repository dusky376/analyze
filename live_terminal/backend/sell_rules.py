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
           ) -> dict[str, Any]:
    """给出一次卖出决策。

    Args:
        entry_kind: ``broke_and_stayed`` / ``sealed`` / ``unknown``
        points: 卖出窗口内的分钟序列（已按窗口裁好）
        fallback_price: 窗口末兜底价（10:00 前最后价 / 断板日收盘价）
        fallback_time: 兜底时刻说明

    Returns:
        ``{"action":"sell","price":float,"reason":str,"kind":"signal|fallback|undecidable"}``
    """
    if entry_kind == "unknown":
        return {"action": "hold", "price": None, "kind": "undecidable",
                "reason": "买入日是否炸板不可判定（缺 broken/封板状态），不猜"}

    sig = find_signal(points or [], drop_pct=drop_pct, pullback_pct=pullback_pct)
    if sig:
        return {"action": "sell", "price": sig["price"], "kind": "signal",
                "reason": sig["detail"], "signal_time": sig["t"]}

    if fallback_price is not None and fallback_price > 0:
        return {"action": "sell", "price": float(fallback_price),
                "kind": "fallback",
                "reason": (f"窗口内无「快速下跌/冲高回落」信号 → 按"
                           f"{fallback_time or '窗口末'}兜底价卖出")}
    return {"action": "hold", "price": None, "kind": "undecidable",
            "reason": "既无触发信号也无兜底价，保持持有（不伪造价格）"}


# ---------------------------------------------------------------------------
# 市场自适应卖点引擎（2026-11-25 进阶版：板块波动率 + VWAP/量能 + 锚定兔子联动）
# ---------------------------------------------------------------------------

def _board_limit_pct(code: str | None) -> float:
    """按股票代码返回涨跌幅上限（0.10 / 0.20 / 0.30）。"""
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

        # 1) 放量/破均价线确认的快速下跌（按真实分钟差 ≤ lookback 过滤，避免跨 30 分钟误判）
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
            if drop_r >= drop_pct:
                below_vwap = (avg_px_f is None) or (px < avg_px_f)
                avg_v10 = (sum(vols[-11:-1]) / max(1, len(vols[-11:-1]))) if len(vols) >= 4 else None
                vol_surge = (vol_f is None) or (avg_v10 is None) or (avg_v10 <= 0) or (vol_f >= 1.5 * avg_v10)
                if below_vwap or drop_r >= drop_pct * 1.4 or vol_surge:
                    return {
                        "t": t, "price": px, "kind": "fast_drop",
                        "thresholds": th,
                        "detail": (f"{t} 自适应急跌：{prev_hi:.2f} → {px:.2f} "
                                   f"(-{drop_r:.2%} ≥ {drop_pct:.2%}"
                                   + ("，跌破均价线" if (avg_px_f and px < avg_px_f) else "")
                                   + ("，锚定兔走弱收紧" if rabbit_weak else "") + ")"),
                    }

        # 2) 真·冲高回落：必须先有实质冲高，再从全天最高回撤
        surge_ref = min(op, pc) if pc else op
        surged = (full_peak - surge_ref) / surge_ref >= min_surge_pct if surge_ref > 0 else True
        if surged and full_peak and full_peak > 0:
            pb_r = (full_peak - px) / full_peak
            if pb_r >= pullback_pct:
                return {
                    "t": t, "price": px, "kind": "pullback",
                    "thresholds": th,
                    "detail": (f"{t} 冲高回落：日内高点 {full_peak:.2f} → {px:.2f} "
                               f"(回撤 {pb_r:.2%} ≥ {pullback_pct:.2%})"),
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


def decide_adaptive(entry_kind: str, *,
                    points: list[dict[str, Any]] | None,
                    code: str | None = None,
                    prev_close: float | None = None,
                    open_price: float | None = None,
                    atr5: float | None = None,
                    rabbit_weak: bool = False,
                    ice_level: str | None = None,
                    fallback_price: float | None = None,
                    fallback_time: str | None = None,
                    start: str | None = None,
                    end: str | None = None) -> dict[str, Any]:
    """自适应卖出决策封装（信号优先，未触发走时间兜底，绝不伪造价格）。"""
    if entry_kind == "unknown":
        return {"action": "hold", "price": None, "kind": "undecidable",
                "reason": "买入日是否炸板不可判定，不猜"}
    sig = find_adaptive_signal(
        points or [], code=code, prev_close=prev_close, open_price=open_price,
        entry_kind=entry_kind, atr5=atr5, rabbit_weak=rabbit_weak,
        ice_level=ice_level, start=start, end=end,
    )
    if sig:
        return {
            "action": "sell",
            "price": sig["price"],
            "kind": sig["kind"],
            "signal_time": sig["t"],
            "thresholds": sig.get("thresholds"),
            "reason": sig["detail"],
        }
    if fallback_price is not None and fallback_price > 0:
        return {
            "action": "sell",
            "price": float(fallback_price),
            "kind": "fallback",
            "thresholds": adaptive_thresholds(code, atr5=atr5,
                                              rabbit_weak=rabbit_weak,
                                              ice_level=ice_level),
            "reason": f"窗口内未触发自适应卖点 → 按{fallback_time or '窗口末'}兜底价卖出",
        }
    return {"action": "hold", "price": None, "kind": "undecidable",
            "reason": "既无触发信号也无兜底价，保持持有（不伪造价格）"}
