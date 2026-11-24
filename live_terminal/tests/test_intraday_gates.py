# -*- coding: utf-8 -*-
"""盘中读数（2026-11-24 用户裁定：「这是股票，不是游戏」）。

覆盖三条此前只敢在收盘后算的链路：
  1. `ice_gate.live_metrics` / `assess(intraday=True)` —— 用实时行情算四维冰点；
  2. `live_sim._emotion_intraday_lists` —— 昨日高标/溢价的盘中代理；
  3. `data_health.theme_check` —— 题材池断更告警（11-24 全天 0 委托的根因）。

测试面板刻意**不含 11-24**：盘中就是「今日 bar 还没进面板」的状态。
最后一天 11-21 视为「昨天」：
  000001: 10.0 → 11.0                     连板 1，昨日涨停
  000002: 10.0 → 11.0 → 12.1 → 13.31      连板 3，昨日涨停（高标）
  000003: 10.0 → 10.1                     非涨停
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import data_health, ice_gate, live_sim  # noqa: E402


DATES_4 = ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21"]


def _panel() -> pl.DataFrame:
    rows = [
        ("000001", "2026-11-20", 10.0),
        ("000001", "2026-11-21", 11.0),
        ("000002", "2026-11-18", 10.0),
        ("000002", "2026-11-19", 11.0),
        ("000002", "2026-11-20", 12.1),
        ("000002", "2026-11-21", 13.31),
        ("000003", "2026-11-20", 10.0),
        ("000003", "2026-11-21", 10.1),
    ]
    return pl.DataFrame(rows, schema=["ts_code", "trade_date", "close"],
                        orient="row")


def _quotes() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_code": ["000001", "000002", "000003"],
            "pct": [-10.0, -9.0, 3.0],
            "quote_time": ["20261124093500"] * 3,
        }
    )


# --------------------------------------------------------------- 连板状态口径

def test_prev_day_state_is_as_of_panel_last_day():
    """昨日连板必须取面板最后一天的 streak，不能错位成前天。"""
    st = ice_gate.prev_day_state(_panel()).sort("ts_code")
    got = {r["ts_code"]: (r["prev_streak"], r["prev_is_zt"])
           for r in st.to_dicts()}
    assert got["000001"] == (1, True)
    assert got["000002"] == (3, True)      # 3 连板；若取 prev_streak 会得 2
    assert got["000003"] == (0, False)


# ------------------------------------------------------------- 盘中四维指标

def test_live_metrics_uses_same_dimension_definition():
    m = ice_gate.live_metrics("20261124", panel=_panel(), quotes=_quotes())
    assert m is not None
    assert m["intraday"] is True
    assert m["as_of"] == "2026-11-24T09:35:00"
    assert m["up"] == 1 and m["down"] == 2
    assert m["limit_down_n"] == 1          # 000001 -10% 跌停
    assert m["limit_up_n"] == 0
    assert m["broken_hi_n"] == 1           # 000002 昨日 3 板今日未涨停
    assert m["down_share"] == round(2 / 3, 4)
    assert m["dt_ratio"] is None           # 无涨停 → 分母 0 不硬凑


def test_live_metrics_rejects_other_day_quotes():
    """行情不是当天的（周末/数据源滞后）时不得拿旧价冒充今天。"""
    q = _quotes().with_columns(pl.lit("20261123093500").alias("quote_time"))
    assert ice_gate.live_metrics("20261124", panel=_panel(), quotes=q) is None


def test_live_metrics_without_prev_day_returns_none():
    flat = pl.DataFrame({"ts_code": ["000001"], "trade_date": ["2026-11-21"],
                         "close": [10.0]})
    # 只有一天 → 无涨跌幅，指标不成立
    assert ice_gate.live_metrics("20261124", panel=flat, quotes=_quotes()) is None


# ------------------------------------------------------------- assess 盘中

def test_assess_intraday_returns_readout_without_persisting():
    r = ice_gate.assess("20261124", panel=_panel(), quotes=_quotes(),
                        intraday=True, roll_bounds=False, persist=True)
    assert r["available"] is True
    assert r["intraday"] is True
    assert r["as_of"] == "2026-11-24T09:35:00"
    assert r["level"] in ice_gate.LEVEL_ORDER
    assert r["size_factor"] is not None
    # 盘中读数不进样本库（未收盘的数据不能当历史样本）
    assert r["sample"]["persisted"] is False


def test_assess_without_intraday_still_reports_missing_panel():
    r = ice_gate.assess("20261124", panel=_panel(), roll_bounds=False)
    assert r["available"] is False
    assert "面板无" in r["reason"]


# ------------------------------------------------------- 情绪闸门盘中代理

def test_emotion_intraday_uses_latest_price():
    hi, prem, as_of = live_sim._emotion_intraday_lists(
        "20261124", _panel(), quotes=_quotes())
    assert as_of == "2026-11-24T09:35:00"
    assert hi == [-9.0]                    # 昨日连板≥2 只有 000002
    assert prem == [-10.0, -9.0]           # 昨日涨停：000001、000002（000003 否）


def test_emotion_intraday_rejects_other_day_quotes():
    q = _quotes().with_columns(pl.lit("20261123093500").alias("quote_time"))
    assert live_sim._emotion_intraday_lists("20261124", _panel(), quotes=q) is None


# ---------------------------------------------------------- 题材池新鲜度

def _write_theme(tmp: Path, manifest: dict | None, *, touch_file=True):
    f = tmp / "themes.parquet"
    if touch_file:
        f.write_bytes(b"stub")
    if manifest is not None:
        (tmp / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return f, tmp / "_manifest.json"


def test_theme_check_missing(tmp_path):
    r = data_health.theme_check(datetime(2026, 11, 24, 10, 0),
                                is_trading_day=True,
                                path=tmp_path / "themes.parquet",
                                manifest_path=tmp_path / "_manifest.json")
    assert r["status"] == "missing" and r["ok"] is False
    assert "0 委托" in r["message"]


def test_theme_check_stale_intraday(tmp_path):
    f, mf = _write_theme(tmp_path, {
        "2026-11-20": {"rows": 39, "fetched_at": "2026-11-20T18:00:20"}})
    r = data_health.theme_check(datetime(2026, 11, 24, 10, 0),
                                is_trading_day=True, path=f, manifest_path=mf)
    assert r["status"] == "stale"
    assert "2026-11-24" in r["message"]


def test_theme_check_ok_intraday(tmp_path):
    f, mf = _write_theme(tmp_path, {
        "2026-11-24": {"rows": 120, "fetched_at": "2026-11-24T09:55:00",
                       "intraday": True}})
    r = data_health.theme_check(datetime(2026, 11, 24, 10, 0),
                                is_trading_day=True, path=f, manifest_path=mf)
    assert r["status"] == "ok" and r["ok"] is True


def test_theme_check_stale_when_refresh_stopped(tmp_path):
    f, mf = _write_theme(tmp_path, {
        "2026-11-24": {"rows": 120, "fetched_at": "2026-11-24T09:30:00"}})
    r = data_health.theme_check(datetime(2026, 11, 24, 10, 0),
                                is_trading_day=True, path=f, manifest_path=mf)
    assert r["status"] == "stale"
    assert "没有刷新" in r["message"]


def test_theme_check_stale_after_close(tmp_path):
    """收盘后当日标签未定格 → 次日选股会缺题材。"""
    f, mf = _write_theme(tmp_path, {
        "2026-11-21": {"rows": 80, "fetched_at": "2026-11-21T15:04:00"}})
    r = data_health.theme_check(datetime(2026, 11, 24, 16, 0),
                                is_trading_day=True, path=f, manifest_path=mf)
    assert r["status"] == "stale"
    assert "收盘后" in r["message"]


def test_theme_check_ok_non_trading_day(tmp_path):
    """周末保持上一交易日的标签，不应报警。"""
    f, mf = _write_theme(tmp_path, {
        "2026-11-21": {"rows": 80, "fetched_at": "2026-11-21T15:04:00"}})
    r = data_health.theme_check(datetime(2026, 11, 22, 11, 0),
                                is_trading_day=False, path=f, manifest_path=mf)
    assert r["status"] == "ok" and r["ok"] is True
