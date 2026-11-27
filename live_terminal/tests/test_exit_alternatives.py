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
def production_report():
    """用**生产账本**跑一份对照报告（隔离的临时账本为空，会返回 available=False）。

    2026-11-23：本模块已改为运行时读 `live_sim.STATE_FILE`，因此默认受
    conftest 的账本隔离约束（这正是修复的隔离洞）。需要真实样本的断言
    必须显式指向生产账本路径。
    """
    import backend.live_sim as LS
    from backend import paths
    prod = paths.paper_dir() / "sim_state.json"
    if not prod.is_file():
        pytest.skip("无生产账本，跳过真实样本断言")
    saved = LS.STATE_FILE
    try:
        LS.STATE_FILE = prod
        return EA.analyze()
    finally:
        LS.STATE_FILE = saved


def test_shape_and_availability(production_report):
    assert production_report["available"] is True
    assert isinstance(production_report["n"], int) and production_report["n"] >= 0
    for k in ("current", "stoploss"):
        s = production_report["summary"][k]
        for f in ("n", "win_rate", "mean", "median"):
            assert f in s
    assert "caveats" in production_report and production_report["caveats"]


def test_isolated_ledger_degrades_gracefully():
    """隔离账本（测试默认环境）下必须优雅降级，不读生产账本。"""
    r = EA.analyze()
    assert r["available"] is False          # 临时账本为空
    assert r["trades"] == []
    assert r["caveats"]                    # 边界声明仍要有


def test_no_nan_or_inf(production_report):
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
    walk(production_report)


def test_each_trade_has_comparison_fields(production_report):
    for t in production_report["trades"]:
        for k in ("id", "code", "name", "entry_date", "entry_price",
                  "current_return", "stoploss_return", "delta_pp",
                  "stoploss_exit_date", "stoploss_note", "context"):
            assert k in t
        ctx = t["context"]
        for k in ("gate_blocked", "break_is_limit_down", "break_date"):
            assert k in ctx


def test_delta_consistency(production_report):
    """delta_pp 必须等于 (stoploss - current) * 100。"""
    for t in production_report["trades"]:
        if t["delta_pp"] is None:
            assert t["stoploss_return"] is None
            continue
        expect = (t["stoploss_return"] - t["current_return"]) * 100.0
        assert t["delta_pp"] == pytest.approx(expect, abs=1e-9)


def test_summary_delta_matches_means(production_report):
    s = production_report["summary"]
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


# ---------------------------------------------------------------------------
# 按环境分组（by_env）
# ---------------------------------------------------------------------------

_DIM_KEYS = {"break_limit_down", "gate", "high_avg_ret", "high_limit_down_share"}


def test_by_env_present_and_well_formed(production_report):
    groups = production_report["by_env"]
    assert isinstance(groups, list) and groups
    for g in groups:
        for k in ("dimension", "bucket", "n", "current", "stoploss",
                  "delta_mean_pp", "small_sample"):
            assert k in g
        assert g["dimension"] in _DIM_KEYS
        assert isinstance(g["n"], int) and g["n"] >= 1
        # small_sample 必须与 n<5 一致
        assert g["small_sample"] is (g["n"] < 5)


def test_by_env_buckets_cover_all_trades_without_overlap(production_report):
    """每个维度的 n 之和必须等于该维度可判定笔数（<= 总笔数）。"""
    for dim in _DIM_KEYS:
        total = sum(g["n"] for g in production_report["by_env"] if g["dimension"] == dim)
        assert total <= production_report["n"], f"{dim} 分组重复计数：{total} > {production_report['n']}"
    # break_limit_down 是必判字段（每笔都有断板日），应覆盖全部
    brk = [g for g in production_report["by_env"] if g["dimension"] == "break_limit_down"]
    if brk:
        assert sum(g["n"] for g in brk) == production_report["n"]


def test_by_env_delta_consistent(production_report):
    for g in production_report["by_env"]:
        if g["delta_mean_pp"] is None:
            assert g["current"]["mean"] is None or g["stoploss"]["mean"] is None
            continue
        expect = (g["stoploss"]["mean"] - g["current"]["mean"]) * 100.0
        assert g["delta_mean_pp"] == pytest.approx(expect, abs=1e-6)


def test_bucket_mapping_labels():
    """分组标签映射：None 不进组，边界值判定正确。"""
    def t(**ctx):
        return {"context": ctx}
    assert EA._bucket(t(break_is_limit_down=True), "break_limit_down") == "断板跌停"
    assert EA._bucket(t(break_is_limit_down=False), "break_limit_down") == "断板未跌停"
    assert EA._bucket(t(break_is_limit_down=None), "break_limit_down") is None
    assert EA._bucket(t(gate_blocked=True), "gate") == "闸门拦截"
    assert EA._bucket(t(gate_blocked=False), "gate") == "闸门通过"
    assert EA._bucket(t(high_avg_ret=0.5), "high_avg_ret") == "昨日高标均涨>0"
    assert EA._bucket(t(high_avg_ret=0.0), "high_avg_ret") == "昨日高标均涨≤0"
    assert EA._bucket(t(high_avg_ret=None), "high_avg_ret") is None
    assert EA._bucket(t(high_limit_down_share=0.2), "high_limit_down_share") == "高标跌停占比≥20%"
    assert EA._bucket(t(high_limit_down_share=0.1), "high_limit_down_share") == "高标跌停占比<20%"
    assert EA._bucket(t(), "unknown_dim") is None
