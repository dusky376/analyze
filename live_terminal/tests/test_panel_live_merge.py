"""panel_live 合并视图的日期闸门回归（2026-11-16）。

旧实现只看当前时钟：`now.time() < 15:05` 就拒绝合并。结果次日盘中
把「昨天已收盘」的 live bar 又排除掉，signal_date 从 11-13 退回 11-12。
本测试保证：历史交易日的 live bar 在次日盘中也会合并。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import feeds  # noqa: E402


def test_historical_live_bar_merges_before_afternoon(tmp_path):
    raw = pl.DataFrame({
        "ts_code": ["600001", "600001"],
        "trade_date": [date(2026, 11, 11), date(2026, 11, 12)],
        "close": [1.0, 1.1],
    })
    live = pl.DataFrame({
        "ts_code": ["600001"],
        "trade_date": [date(2026, 11, 13)],
        "close": [1.2],
    })
    p = tmp_path / "panel_live.parquet"
    live.write_parquet(p)
    out = feeds._merged_with_live_view(raw, p)
    assert out["trade_date"].max() == date(2026, 11, 13), (
        "昨天已收盘的 live bar 不应因为现在是盘中而被排除"
    )
