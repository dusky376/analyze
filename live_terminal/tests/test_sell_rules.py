# -*- coding: utf-8 -*-
"""卖出规则测试（用户 2026-11-25 裁定）。

规则：
  A. 买入日炸板且**收盘未回封** → 次日 10:00 前卖；信号=快速下跌/冲高回落
  B. 买入日收盘封住（未炸板）→ 断板日收盘前卖；同样信号优先
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import sell_rules as SR  # noqa: E402


# ---------------------------------------------------------------------------
# 买入日归类（用户裁定：必须收盘时仍炸板才算 A）
# ---------------------------------------------------------------------------

def test_classify_sealed_when_closed_sealed():
    """收盘封住 → sealed（即使盘中开过板）。"""
    assert SR.classify_entry_day(broken_times=3, still_sealed_at_close=True) == "sealed"
    assert SR.classify_entry_day(broken_times=0, still_sealed_at_close=True) == "sealed"


def test_classify_broke_and_stayed():
    """收盘未封 + 炸过板 → broke_and_stayed。"""
    assert SR.classify_entry_day(broken_times=2, still_sealed_at_close=False) == "broke_and_stayed"


def test_classify_unknown_without_state():
    assert SR.classify_entry_day(broken_times=1, still_sealed_at_close=None) == "unknown"


# ---------------------------------------------------------------------------
# 快速下跌信号
# ---------------------------------------------------------------------------

def _pts(seq):
    """[(t, price), ...] → points"""
    return [{"t": t, "price": p} for t, p in seq]


def test_fast_drop_detected():
    # 前 5 分钟最高 10.00，跌到 9.75 = -2.5% ≥ 2%
    pts = _pts([("09:31", 9.95), ("09:32", 10.00), ("09:33", 9.98),
                ("09:34", 9.97), ("09:35", 9.90), ("09:36", 9.75)])
    sig = SR.find_signal(pts)
    assert sig is not None
    assert sig["kind"] == "fast_drop"
    assert sig["t"] == "09:36"
    assert sig["price"] == pytest.approx(9.75)


def test_no_signal_on_slow_grind():
    """缓慢阴跌（每分钟跌 0.3%）不触发快速下跌。"""
    pts = _pts([(f"09:{30+i:02d}", 10.0 * (1 - 0.003 * i)) for i in range(20)])
    sig = SR.find_signal(pts, pullback_pct=0.99)   # 关掉回撤，只测急跌
    assert sig is None


# ---------------------------------------------------------------------------
# 冲高回落信号
# ---------------------------------------------------------------------------

def test_pullback_detected():
    # 冲高到 11.00 后回落到 10.60 = -3.6% ≥ 3%
    pts = _pts([("09:31", 10.0), ("09:40", 11.00), ("09:50", 10.90),
                ("10:00", 10.60)])
    sig = SR.find_signal(pts, drop_pct=0.99)      # 关掉急跌，只测回撤
    assert sig is not None
    assert sig["kind"] == "pullback"
    assert sig["t"] == "10:00"


def test_pullback_not_triggered_on_small_dip():
    pts = _pts([("09:31", 10.0), ("09:40", 10.20), ("09:50", 10.10)])
    sig = SR.find_signal(pts, drop_pct=0.99)
    assert sig is None


# ---------------------------------------------------------------------------
# 窗口限制（A 路径：10:00 前）
# ---------------------------------------------------------------------------

def test_window_limits_to_before_1000():
    """10:00 之后的信号不该在 A 路径窗口内触发。"""
    pts = _pts([("10:05", 10.0), ("10:10", 9.70)])   # 3% 急跌，但已过 10:00
    sig = SR.find_signal(pts, start="09:30", end="10:00")
    assert sig is None


# ---------------------------------------------------------------------------
# decide 决策
# ---------------------------------------------------------------------------

def test_decide_uses_signal_over_fallback():
    pts = _pts([("09:31", 10.0), ("09:35", 9.70)])
    r = SR.decide("broke_and_stayed", points=pts, fallback_price=9.80,
                  fallback_time="10:00")
    assert r["action"] == "sell"
    assert r["kind"] == "signal"
    assert r["price"] == pytest.approx(9.70)


def test_decide_falls_back_when_no_signal():
    pts = _pts([("09:31", 10.0), ("09:40", 10.01), ("09:55", 10.00)])
    r = SR.decide("broke_and_stayed", points=pts, fallback_price=10.00,
                  fallback_time="10:00")
    assert r["action"] == "sell"
    assert r["kind"] == "fallback"
    assert r["price"] == pytest.approx(10.00)
    assert "10:00" in r["reason"]


def test_decide_unknown_kind_holds():
    r = SR.decide("unknown", points=_pts([("09:31", 10.0)]), fallback_price=10.0)
    assert r["action"] == "hold"
    assert r["kind"] == "undecidable"


def test_decide_no_price_holds():
    """既无信号也无兜底价 → 持有，不伪造。"""
    r = SR.decide("sealed", points=[], fallback_price=None)
    assert r["action"] == "hold"
    assert r["price"] is None


def test_decide_sealed_path_uses_close_fallback():
    """B 路径（未炸板）：窗口内无信号 → 断板日收盘价兜底。"""
    pts = _pts([("09:31", 10.0), ("14:50", 10.05), ("14:59", 10.02)])
    r = SR.decide("sealed", points=pts, fallback_price=10.02,
                  fallback_time="断板日收盘前")
    assert r["action"] == "sell"
    assert r["kind"] == "fallback"
    assert r["price"] == pytest.approx(10.02)


# ---------------------------------------------------------------------------
# 阈值可调
# ---------------------------------------------------------------------------

def test_thresholds_configurable():
    # 跌 1.5%：2% 阈值不触发，1% 阈值触发（避开 0.01 浮点边界）
    pts = _pts([("09:31", 10.0), ("09:36", 9.85)])
    assert SR.find_signal(pts, drop_pct=0.02) is None
    assert SR.find_signal(pts, drop_pct=0.01) is not None
