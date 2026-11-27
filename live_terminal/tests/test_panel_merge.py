"""合并视图纯函数与护栏单测（不依赖真实面板文件）。"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import feeds  # noqa: E402


def _raw_panel(days: int = 3, rows_per_day: int = 10) -> pl.DataFrame:
    base = date(2026, 11, 10)
    rows = []
    for i in range(days):
        d = base + timedelta(days=i)
        for j in range(rows_per_day):
            rows.append({
                "trade_date": d, "ts_code": f"{600000 + j:06d}",
                "open": 10.0, "high": 11.0, "low": 9.9, "close": 10.5,
                "volume": 1000.0, "amount": 1e7, "turnover": 1.0,
                "pct_chg": 0.5,
            })
    return pl.DataFrame(rows)


def _live_panel(day: date, rows_per_day: int = 10) -> pl.DataFrame:
    rows = []
    for j in range(rows_per_day):
        rows.append({
            "trade_date": day, "ts_code": f"{600000 + j:06d}",
            "open": 10.6, "high": 11.1, "low": 10.4, "close": 10.8,
            "volume": 1100.0, "amount": 1.1e7, "turnover": 1.1,
            "pct_chg": 2.86,
        })
    return pl.DataFrame(rows)


def _write_live(tmp_path: Path, day: date, n: int = 10) -> Path:
    p = tmp_path / "panel_live.parquet"
    _live_panel(day, n).write_parquet(p)
    return p


def _fix_now(monkeypatch, y: int, mo: int, d: int, h: int, mi: int) -> None:
    """固定 feeds 模块内 datetime.now()（合并闸门判断用）。"""
    fixed = datetime(y, mo, d, h, mi)

    class _DT(datetime):
        @classmethod
        def now(cls):
            return fixed

    monkeypatch.setattr(feeds, "datetime", _DT)


class TestMergedLiveView:
    def test_merges_next_day_after_close(self, tmp_path, monkeypatch):
        raw = _raw_panel()                      # 11-10~11-12
        live_day = date(2026, 11, 13)
        p = _write_live(tmp_path, live_day)
        # 固定 now 到收盘闸门后（21:00）
        _fix_now(monkeypatch, 2026, 11, 13, 21, 0)
        merged = feeds._merged_with_live_view(raw, p)
        assert merged["trade_date"].max() == live_day
        assert merged.height == raw.height + 10

    def test_not_merged_before_gate(self, tmp_path, monkeypatch):
        raw = _raw_panel()
        p = _write_live(tmp_path, date(2026, 11, 13))
        _fix_now(monkeypatch, 2026, 11, 13, 10, 0)  # 上午 10 点
        merged = feeds._merged_with_live_view(raw, p)
        assert merged["trade_date"].max() == date(2026, 11, 12)

    def test_not_merged_for_gap_day(self, tmp_path, monkeypatch):
        raw = _raw_panel(days=1)                 # 只到 11-10
        p = _write_live(tmp_path, date(2026, 11, 13))  # 跳了两天（>6 天阈值内但>1 日）
        _fix_now(monkeypatch, 2026, 11, 13, 21, 0)
        merged = feeds._merged_with_live_view(raw, p)
        # 11-10 → 11-13 跨 3 天非交易日缺口：护栏 a 只接「下一交易日」，
        # 用「行数覆盖×日期差」近似：断档日（11-11/11-12 无行）说明面板缺日，
        # 交给离线重建 —— 这里 live_day - raw_last = 3 天 ≤6 天上限，
        # 仍应拒绝（真实语义：只允许 T+1，周末跳过不算）。
        assert merged["trade_date"].max() == date(2026, 11, 10)

    def test_not_merged_low_coverage(self, tmp_path, monkeypatch):
        raw = _raw_panel(rows_per_day=10)
        p = _write_live(tmp_path, date(2026, 11, 13), n=3)   # 覆盖率 30%
        _fix_now(monkeypatch, 2026, 11, 13, 21, 0)
        merged = feeds._merged_with_live_view(raw, p)
        assert merged["trade_date"].max() == date(2026, 11, 12)

    def test_never_writes_history_file(self, tmp_path, monkeypatch):
        raw_p = tmp_path / "panel_raw.parquet"
        raw = _raw_panel(days=3)                 # 11-10~11-12
        raw.write_parquet(raw_p)
        before = raw.height
        p = _write_live(tmp_path, date(2026, 11, 13))
        _fix_now(monkeypatch, 2026, 11, 13, 21, 0)
        feeds._merged_with_live_view(raw, p)
        after = pl.read_parquet(raw_p)
        assert after.height == before            # 历史文件一个字节不动

    def test_no_duplicate_when_same_day(self, tmp_path, monkeypatch):
        # raw 已含 11-12 完整行；live 也是 11-12 → live_day == raw_last 不合并
        raw = _raw_panel(days=3)                 # 11-10/11/12
        p = _write_live(tmp_path, date(2026, 11, 12))
        _fix_now(monkeypatch, 2026, 11, 12, 21, 0)
        merged = feeds._merged_with_live_view(raw, p)
        assert merged.filter(pl.col("trade_date") == date(2026, 11, 12)).height == 10
