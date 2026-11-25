# -*- coding: utf-8 -*-
"""冰点判定（v3.1 四维分位）与仓位系数的契约测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import ice_gate as IG  # noqa: E402


# ---------------------------------------------------------------------------
# 纯函数：档位映射 / 严重度 / 判据
# ---------------------------------------------------------------------------

def test_level_by_pct_bounds():
    b = IG.DEFAULT_BOUNDS["limit_down_n"]      # P50=9,P70=15,P80=21,P90=37,P95=60
    assert IG.level_by_pct(5, b) == "L0"
    assert IG.level_by_pct(12, b) == "L1"
    assert IG.level_by_pct(18, b) == "L2"
    assert IG.level_by_pct(30, b) == "L3"
    assert IG.level_by_pct(50, b) == "L4"
    assert IG.level_by_pct(70, b) == "L5"
    assert IG.level_by_pct(None, b) is None
    assert IG.level_by_pct(float("nan"), b) is None


def test_severity_is_max_of_dims():
    dims = {"down_share": "L2", "limit_down_n": "L5",
            "dt_ratio": "L3", "broken_hi_n": "L1"}
    assert IG.severity(dims) == "L5"
    assert IG.severity({"down_share": None}) is None


def test_is_ice_requires_multi_dim_confirmation():
    """单维 L4 不再判冰（v3.1 修复：避免 22.6% 基准率）。"""
    rec_only_one = {"limit_down_n": 50, "down_share": 0.3,
                    "dt_ratio": 0.1, "broken_hi_n": 0}
    dims = {"down_share": "L0", "limit_down_n": "L4",
            "dt_ratio": "L0", "broken_hi_n": "L0"}
    # limit_down_n=50 < 40? 否，50>=40 → 仍按绝对旁路判冰
    assert IG.is_ice(rec_only_one, dims) is True

    rec_no_abs = {"limit_down_n": 20, "down_share": 0.3,
                  "dt_ratio": 0.1, "broken_hi_n": 0}
    assert IG.is_ice(rec_no_abs, dims) is False      # 只有一维 L4 且未达 40 家


def test_is_ice_multi_dim_true():
    rec = {"limit_down_n": 30, "down_share": 0.9, "dt_ratio": 2.0, "broken_hi_n": 7}
    dims = {"down_share": "L5", "limit_down_n": "L3",
            "dt_ratio": "L5", "broken_hi_n": "L4"}
    assert IG.is_ice(rec, dims) is True              # S=L5 且 >=2 维>=L3


def test_absolute_bypass_40():
    rec = {"limit_down_n": 40, "down_share": 0.1, "dt_ratio": 0.1, "broken_hi_n": 0}
    dims = {"down_share": "L0", "limit_down_n": "L0",
            "dt_ratio": "L0", "broken_hi_n": "L0"}
    assert IG.is_ice(rec, dims) is True              # 跌停>=40 直接判冰


def test_broad_decline_bypass_20261125():
    """2026-11-25 用户裁定：普跌(下跌占比>=80%)也判冰，即使跌停家数不足40。

    实测 11-25：下跌占比 87.37%、跌停 28 家 → v3.1 判非冰点（漏判）。
    """
    rec = {"limit_down_n": 28, "down_share": 0.8737,
           "dt_ratio": 0.32, "broken_hi_n": 3}
    dims = {"down_share": "L4", "limit_down_n": "L3",
            "dt_ratio": "L2", "broken_hi_n": "L0"}
    # 旧判据：S=L4 但只有 1 维 >=L3，且跌停 28<40 → False
    assert IG.ICE_DOWN_SHARE_ABS == 0.80
    assert IG.is_ice(rec, dims) is True              # 新旁路必须判冰


def test_broad_decline_does_not_over_trigger():
    """下跌占比 79%（<80%）不触发普跌旁路。"""
    rec = {"limit_down_n": 5, "down_share": 0.79,
           "dt_ratio": 0.1, "broken_hi_n": 0}
    dims = {"down_share": "L2", "limit_down_n": "L0",
            "dt_ratio": "L0", "broken_hi_n": "L0"}
    assert IG.is_ice(rec, dims) is False


# ---------------------------------------------------------------------------
# 仓位系数（用户裁定：冰点小仓试错，不绝对空仓）
# ---------------------------------------------------------------------------

def test_size_factor_never_zero():
    """冰点不空仓：任何档位系数都 > 0。"""
    for lv in IG.LEVEL_ORDER:
        assert IG.size_factor(lv, ice=True) > 0
        assert IG.size_factor(lv, ice=False) > 0


def test_size_factor_values():
    assert IG.size_factor("L5", ice=True) == pytest.approx(0.30)
    assert IG.size_factor("L4", ice=True) == pytest.approx(0.50)
    assert IG.size_factor("L0", ice=False) == pytest.approx(1.00)
    # 非冰点即使档位 L4 也不缩仓（判据与仓位解耦）
    assert IG.size_factor("L4", ice=False) == pytest.approx(1.00)


def test_compute_bounds_from_rows():
    rows = [{"limit_down_n": i} for i in range(100)]
    b = IG.compute_bounds(rows)
    assert "limit_down_n" in b
    assert b["limit_down_n"]["n"] == 100
    assert b["limit_down_n"]["P50"] <= b["limit_down_n"]["P90"] <= b["limit_down_n"]["P95"]
    # 样本不足 30 不产出边界
    assert IG.compute_bounds([{"limit_down_n": 1}]) == {}


def test_dim_levels_falls_back_when_bounds_missing():
    """加固：bounds 为空/残缺时必须退回 DEFAULT_BOUNDS，不能静默判 None。

    2026-11-23：若让所有维度变 None，is_ice() 会静默返回 False，
    冰点闸门彻底失效却无报错（比崩溃更危险）。
    """
    rec = {"down_share": 0.95, "limit_down_n": 100,
           "dt_ratio": 3.0, "broken_hi_n": 8}
    expect = {"down_share": "L5", "limit_down_n": "L5",
              "dt_ratio": "L5", "broken_hi_n": "L4"}
    assert IG.dim_levels(rec, {}) == expect
    assert IG.dim_levels(rec, {"down_share": IG.DEFAULT_BOUNDS["down_share"]}) == expect
    assert IG.dim_levels(rec, None) == expect
    # 关键：空 bounds 下仍能判出冰点（不是静默 False）
    assert IG.is_ice(rec, IG.dim_levels(rec, {})) is True


def test_level_by_pct_returns_none_without_bounds():
    """空 bounds 时 level_by_pct 返回 None（不瞎猜档位）。"""
    assert IG.level_by_pct(0.95, {}) is None
    assert IG.level_by_pct(100, {}) is None


# ---------------------------------------------------------------------------
# 真实数据（集成）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def panel():
    from backend import feeds
    return feeds.build_panel()


def test_assess_real_recent_days(panel):
    """真实数据：11-16~11-20 冰点、11-23 非冰点（用户已认可的判断）。"""
    for d in ("20261116", "20261117", "20261118", "20261119", "20261120"):
        r = IG.assess(d, panel=panel)
        assert r["available"] is True
        assert r["is_ice"] is True, f"{d} 应判冰点"
        assert r["size_factor"] < 1.0

    r = IG.assess("20261123", panel=panel)
    assert r["available"] is True
    assert r["is_ice"] is False
    assert r["size_factor"] == pytest.approx(1.0)
    assert r["metrics"]["limit_up_n"] > r["metrics"]["limit_down_n"]


def test_assess_missing_day_degrades(panel):
    r = IG.assess("19900101", panel=panel)
    assert r["available"] is False
    assert r["is_ice"] is False
    assert r["size_factor"] == pytest.approx(1.0)    # 不可判定时不缩仓


def test_assess_has_caveat_about_drift(panel):
    r = IG.assess("20261123", panel=panel)
    assert "漂移" in r["caveat"]
    assert r["bounds_source"] in ("rolling_250d", "snapshot")
