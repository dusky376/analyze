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
