# -*- coding: utf-8 -*-
"""涨跌停家数的**唯一口径**（2026-11-21 统一，用户裁定：按东财，剔除 ST）。

东财「涨/跌停池」的实际定义（2026-11-20 用 58 家逐只反推验证）：

1. **精确价**：收盘价 == 交易所涨/跌停价（不用 `pct_chg >= 9.65` 这类百分比阈值）；
2. **封死**：跌停要求盘口买一为空、涨停要求卖一为空。
   「收盘价 = 跌停价但买一还有挂单」属于**跌停未封死**，东财不计入池；
3. **剔除 ST**：`*ST` / `ST` / `S*ST` 一律不算。

实测（2026-11-20 收盘，腾讯盘口）：
    精确价 + 封死 + 剔 ST  ->  涨停 28 / 跌停 58   == 东财涨停池 28 / 跌停池 58
    精确价（含 ST、含未封死）->  涨停 30 / 跌停 68   （审计对账用的「触及」口径）

历史交易日没有盘口留档，只能退化为「收盘价 == 涨跌停价 + 剔 ST」，
此时 `limit_basis` 会标成 `panel_close_price_ex_st`，**绝不冒充**东财池口径。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from backend import feature_engine, trading_rules

#: 价格相等容差。只吸收浮点误差；**不做「1 分钱」容差**
#: （±0.011 会把 6 只「收在跌停价上方 1 分钱」的票算成跌停，2026-11-20 实测）。
PRICE_TOL = feature_engine.ZT_TOL

BASIS_EM_POOL = "em_pool:sealed_ex_st"
BASIS_PANEL = "panel_close_price_ex_st"
BASIS_TOUCHED = "price_touched_all"

NOTE_POOL = "东财口径：收盘封死 + 剔除 ST"
NOTE_PANEL = "无盘口留档：退化为收盘精确价 + 剔除 ST（不含封死判定）"


def is_st(name: Any) -> bool:
    """ST 判定：唯一实现在 `feature_engine.is_st`（含 *ST / S*ST / 退）。"""
    return feature_engine.is_st(str(name or ""))


def _empty() -> dict[str, Any]:
    return {
        "n": 0,
        "limit_up": 0, "limit_down": 0,
        "limit_up_touched": 0, "limit_down_touched": 0,
        "limit_up_st": 0, "limit_down_st": 0,
        "limit_up_unsealed": 0, "limit_down_unsealed": 0,
        "limit_basis": BASIS_PANEL,
        "book_verified": False,
        "limit_note": "无有效行情",
    }


def from_quotes(q: pl.DataFrame | None) -> dict[str, Any]:
    """从腾讯实时行情算东财口径涨跌停（封死 + 剔 ST）。

    需要列：price / prev_close / limit_up / limit_down / name（可选 bid1_vol、ask1_vol）。
    缺少盘口列或盘口不可信时自动退化为「精确价 + 剔 ST」，并在 `book_verified` 标明。
    """
    if q is None or not isinstance(q, pl.DataFrame) or q.height == 0:
        return _empty()
    need = {"price", "prev_close", "limit_up", "limit_down"}
    if not need.issubset(set(q.columns)):
        return _empty()

    v = q.filter(
        pl.col("price").is_not_null()
        & pl.col("prev_close").is_not_null()
        & (pl.col("prev_close") > 0)
        & pl.col("limit_down").is_not_null()
        & (pl.col("limit_down") > 0)
    )
    if v.height == 0:
        return _empty()

    price = v["price"].to_numpy().astype(float)
    lu = v["limit_up"].to_numpy().astype(float)
    ld = v["limit_down"].to_numpy().astype(float)
    lu = np.where(np.isfinite(lu), lu, np.nan)

    at_zt = np.isfinite(lu) & (np.abs(price - lu) <= PRICE_TOL)
    at_dt = np.isfinite(ld) & (np.abs(price - ld) <= PRICE_TOL)

    names = (v["name"].to_list() if "name" in v.columns
             else [""] * v.height)
    st = np.array([is_st(x) for x in names], dtype=bool)

    has_book = {"bid1_vol", "ask1_vol"}.issubset(set(v.columns))
    book_verified = False
    unsealed_zt = np.zeros(v.height, dtype=bool)
    unsealed_dt = np.zeros(v.height, dtype=bool)
    if has_book:
        ask1 = v["ask1_vol"].to_numpy().astype(float)
        bid1 = v["bid1_vol"].to_numpy().astype(float)
        ask1 = np.where(np.isfinite(ask1), ask1, 0.0)
        bid1 = np.where(np.isfinite(bid1), bid1, 0.0)
        # 盘口可信判据：全市场至少一半的票卖一有挂单（停牌/集合竞价/盘前不满足）
        book_verified = float((ask1 > 0).sum()) >= 0.5 * v.height
        if book_verified:
            unsealed_zt = at_zt & (ask1 > 0)   # 涨停但卖一还有货 = 未封死
            unsealed_dt = at_dt & (bid1 > 0)   # 跌停但买一还有人接 = 未封死

    zt_pool = at_zt & ~st & ~unsealed_zt
    dt_pool = at_dt & ~st & ~unsealed_dt

    return {
        "n": int(v.height),
        "limit_up": int(zt_pool.sum()),
        "limit_down": int(dt_pool.sum()),
        "limit_up_touched": int(at_zt.sum()),
        "limit_down_touched": int(at_dt.sum()),
        "limit_up_st": int((at_zt & st).sum()),
        "limit_down_st": int((at_dt & st).sum()),
        "limit_up_unsealed": int((at_zt & ~st & unsealed_zt).sum()),
        "limit_down_unsealed": int((at_dt & ~st & unsealed_dt).sum()),
        "limit_basis": BASIS_EM_POOL if book_verified else BASIS_PANEL,
        "book_verified": bool(book_verified),
        "limit_note": NOTE_POOL if book_verified else NOTE_PANEL,
    }


def _round2(x: np.ndarray) -> np.ndarray:
    """交易所口径：四舍五入到分（唯一实现在 `trading_rules.round_price`）。"""
    return np.asarray(trading_rules.round_price(np.asarray(x, dtype=float)), dtype=float)


def from_panel_day(panel: pl.DataFrame | None, day,
                   names: dict[str, str] | None = None) -> dict[str, Any]:
    """从日线面板算某日涨跌停（收盘精确价 + 可选剔 ST），无盘口。

    `panel` 需包含 `trade_date / ts_code / close / pct_chg`；`names` 缺失时不剔 ST，
    `limit_basis` 会标成 `panel_close_price`（不含 ex_st）。
    """
    if panel is None or not isinstance(panel, pl.DataFrame) or panel.height == 0:
        return _empty()
    need = {"trade_date", "ts_code", "close"}
    if not need.issubset(set(panel.columns)):
        return _empty()

    hist = panel.filter(pl.col("trade_date") <= day).sort(["ts_code", "trade_date"])
    hist = hist.with_columns(pl.col("close").shift(1).over("ts_code").alias("_prev"))
    d = hist.filter(pl.col("trade_date") == day)
    if d.height == 0:
        return _empty()
    d = d.filter(pl.col("_prev").is_not_null() & (pl.col("_prev") > 0)
                 & pl.col("close").is_not_null())
    if d.height == 0:
        return _empty()

    codes = [str(c) for c in d["ts_code"].to_list()]
    close = d["close"].to_numpy().astype(float)
    prev = d["_prev"].to_numpy().astype(float)
    pct = np.array([float(feature_engine.limit_pct(c)) for c in codes])

    zt_price = _round2(prev * (1.0 + pct))
    dt_price = _round2(prev * (1.0 - pct))
    at_zt = np.abs(close - zt_price) <= PRICE_TOL
    at_dt = np.abs(close - dt_price) <= PRICE_TOL

    if names:
        st = np.array([is_st(names.get(c)) for c in codes], dtype=bool)
    else:
        st = np.zeros(len(codes), dtype=bool)

    zt_n = int((at_zt & ~st).sum())
    dt_n = int((at_dt & ~st).sum())
    return {
        "n": int(d.height),
        "limit_up": zt_n,
        "limit_down": dt_n,
        "limit_up_touched": int(at_zt.sum()),
        "limit_down_touched": int(at_dt.sum()),
        "limit_up_st": int((at_zt & st).sum()),
        "limit_down_st": int((at_dt & st).sum()),
        "limit_up_unsealed": 0,
        "limit_down_unsealed": 0,
        "limit_basis": BASIS_PANEL if names else "panel_close_price",
        "book_verified": False,
        "limit_note": NOTE_PANEL if names else
                      "无盘口留档、无名称表：收盘精确价（未剔 ST）",
    }


def current_names() -> dict[str, str]:
    """当前股票名称表（ST 判定用）。读项目缓存，失败返回空表而不是编。"""
    try:
        from backend import feeds
        df = feeds.stock_list()
        return dict(zip([str(c) for c in df["ts_code"].to_list()],
                        df["name"].to_list()))
    except Exception:  # noqa: BLE001
        return {}
