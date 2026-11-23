# -*- coding: utf-8 -*-
"""仓位管理测试：总资金 100 万 + 冰点降仓 + 总仓上限（用户 2026-11-23 裁定）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import live_sim as LS  # noqa: E402


def _cand(price: float = 10.0, code: str = "000001") -> dict:
    return {"ts_code": code, "name": code, "first": {"price": price}}


# ---------------------------------------------------------------------------
# 常量：总资金 100 万
# ---------------------------------------------------------------------------

def test_capital_is_one_million():
    assert LS.CAPITAL_YUAN == pytest.approx(1_000_000.0)
    assert LS.POSITION_PCT == pytest.approx(0.10)
    assert LS.POSITION_SIZE_YUAN == pytest.approx(100_000.0)   # 100万 × 10%
    assert LS.MAX_GROSS_PCT == pytest.approx(0.80)


# ---------------------------------------------------------------------------
# 冰点降仓
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factor,expect", [(1.0, 100_000.0),
                                           (0.5, 50_000.0),
                                           (0.3, 30_000.0)])
def test_size_scales_with_ice_factor(factor, expect):
    plan = LS.plan_orders([_cand()], size_factor=factor)
    assert plan[0]["size_yuan"] == pytest.approx(expect)
    if factor < 1.0:
        assert "冰点降仓" in plan[0]["budget_reason"]
    else:
        assert plan[0]["budget_reason"] == "常态 10%"


def test_factor_never_negative_or_above_one():
    """越界系数被夹到 [0,1]，防止负仓位或放大超过总资金比例。"""
    assert LS.plan_orders([_cand()], size_factor=-1.0)[0]["size_yuan"] >= 0
    assert LS.plan_orders([_cand()], size_factor=5.0)[0]["size_yuan"] <= 100_000.0


# ---------------------------------------------------------------------------
# 总仓上限 80%（不超杠杆）
# ---------------------------------------------------------------------------

def test_total_never_exceeds_gross_cap():
    """无论多少只候选，计划合计不得超过 100万×80%。"""
    for n, factor in [(4, 1.0), (8, 1.0), (20, 1.0), (8, 0.5), (20, 0.3)]:
        cands = [_cand(10.0, f"{i:06d}") for i in range(n)]
        plan = LS.plan_orders(cands, size_factor=factor)
        total = sum(p["size_yuan"] for p in plan)
        assert total <= LS.CAPITAL_YUAN * LS.MAX_GROSS_PCT + 1e-6, \
            f"n={n} factor={factor} 合计 {total} 超总仓上限"


def test_existing_holdings_reduce_available():
    """已持仓占用会压缩可用资金，避免叠加超总仓。"""
    cands = [_cand(10.0, f"{i:06d}") for i in range(4)]
    plan = LS.plan_orders(cands, size_factor=1.0, held_notional=600_000.0)
    total = sum(p["size_yuan"] for p in plan)
    assert total <= LS.CAPITAL_YUAN * LS.MAX_GROSS_PCT - 600_000.0 + 1e-6


def test_available_exhausted_still_gives_one_lot():
    """可用资金耗尽时仍保证 1 手（否则买不进），但会标注原因。"""
    plan = LS.plan_orders([_cand(10.0)], size_factor=1.0,
                          held_notional=LS.CAPITAL_YUAN)
    assert plan[0]["size_yuan"] == pytest.approx(10.0 * LS.LOT_SIZE)  # 1 手
    assert "不足 1 手" in plan[0]["budget_reason"]


def test_empty_candidates():
    assert LS.plan_orders([], size_factor=1.0) == []


# ---------------------------------------------------------------------------
# 与 build_order 集成：冰点日必须是降仓后的 notional
# ---------------------------------------------------------------------------

def test_build_order_uses_budget():
    g = {"first": {"price": 10.0}, "last": {"seal_fund": 1e8}, "name": "测试",
         "industry": "测试", "turnover": 10.0, "n_frames": 1,
         "broken_max": 0, "broken_seen": True, "frame_ts": "t0",
         "last_frame_ts": "t0"}
    o = LS.build_order("20261116", "000001", g, size_factor=0.3, size_yuan=30_000.0)
    assert o["size_factor"] == pytest.approx(0.3)
    assert o["size_budget_yuan"] == pytest.approx(30_000.0)
    # 10 元 × 100 股/手 = 1000 元/手 → 30000/1000 = 30 手 = 3000 股
    assert o["qty"] == 3000
    assert o["notional"] == pytest.approx(30_000.0)
    assert "30,000" in o["notional_basis"]
