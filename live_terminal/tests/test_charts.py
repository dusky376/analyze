# -*- coding: utf-8 -*-
"""个股图表模块测试（K线 + 分时，真实数据）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import charts  # noqa: E402


def test_kline_real_data():
    r = charts.kline("002931", days=10)
    assert r["available"] is True
    assert len(r["bars"]) == 10
    b = r["bars"][-1]
    for k in ("date", "open", "high", "low", "close", "volume"):
        assert k in b and b[k] is not None


def test_kline_unknown_code():
    r = charts.kline("999999", days=5)
    assert r["available"] is False
    assert r["bars"] == []


def test_intraday_real_data():
    r = charts.intraday("002931", "20261124")
    assert r["available"] is True
    assert r["n"] >= 10
    pts = r["points"]
    assert pts[0]["t"] < pts[-1]["t"]
    assert all(p["price"] > 0 for p in pts)


def test_intraday_no_archive():
    r = charts.intraday("002931", "19900101")
    assert r["available"] is False
    assert r["points"] == []


def test_stock_chart_combined():
    r = charts.stock_chart("002931", day="20261124", kdays=5)
    assert r["available"] is True
    assert r["kline"]["available"] is True
    assert r["intraday"]["available"] is True
