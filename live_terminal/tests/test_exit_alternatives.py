# -*- coding: utf-8 -*-
"""卖法对照（只读研究模块）的契约测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import exit_alternatives as EA  # noqa: E402


@pytest.fixture(scope="module")
def report():
    return EA.analyze()


def test_shape_and_availability(report):
    assert report["available"] is True
    assert isinstance(report["n"], int) and report["n"] >= 0
    for k in ("current", "stoploss"):
        s = report["summary"][k]
        for f in ("n", "win_rate", "mean", "median"):
            assert f in s
    assert "caveats" in report and report["caveats"]


def test_no_nan_or_inf(report):
    """JSON 边界必须是干净数字（None 允许，NaN/Inf 不允许）。"""
    def walk(v):
        if isinstance(v, float):
            assert v == v, "NaN leaked"
            assert abs(v) != float("inf"), "Inf leaked"
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(report)


def test_each_trade_has_comparison_fields(report):
    for t in report["trades"]:
        for k in ("id", "code", "name", "entry_date", "entry_price",
                  "current_return", "stoploss_return", "delta_pp",
                  "stoploss_exit_date", "stoploss_note", "context"):
            assert k in t
        ctx = t["context"]
        for k in ("gate_blocked", "break_is_limit_down", "break_date"):
            assert k in ctx


def test_delta_consistency(report):
    """delta_pp 必须等于 (stoploss - current) * 100。"""
    for t in report["trades"]:
        if t["delta_pp"] is None:
            assert t["stoploss_return"] is None
            continue
        expect = (t["stoploss_return"] - t["current_return"]) * 100.0
        assert t["delta_pp"] == pytest.approx(expect, abs=1e-9)


def test_summary_delta_matches_means(report):
    s = report["summary"]
    if s["delta_mean_pp"] is None:
        assert s["current"]["mean"] is None or s["stoploss"]["mean"] is None
        return
    expect = (s["stoploss"]["mean"] - s["current"]["mean"]) * 100.0
    assert s["delta_mean_pp"] == pytest.approx(expect, abs=1e-6)


def test_core_logic_is_readonly_pure_functions():
    """核心判据是纯函数，可直接单测。"""
    row_dt = {"pct": -0.1000, "lim": 0.10, "high": 9.0, "dtp": 9.0}
    row_ok = {"pct": -0.02, "lim": 0.10, "high": 9.5, "dtp": 9.0}
    assert EA.is_dt(row_dt) is True
    assert EA.is_dt(row_ok) is False
    assert EA.oneword_dt(row_dt) is True          # high 贴在跌停价 → 一字
    assert EA.oneword_dt({**row_dt, "high": 9.5}) is False
    assert EA.try_close(row_dt) is None            # 收盘跌停不可成交
    row_ok2 = {**row_ok, "close": 9.80, "date": "2026-11-18"}
    got = EA.try_close(row_ok2)
    assert got is not None and got["price"] == 9.80
