# -*- coding: utf-8 -*-
"""数据完整性自检（只读诊断）的行为测试。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import data_health  # noqa: E402


def _make_days(tmp: Path, days: list[str], with_today: bool = False, day: str = "20261123"):
    live = tmp / "live"
    for d in days:
        p = live / d / "pools" / "pools.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n", encoding="utf-8")
    if with_today:
        p = live / day / "pools" / "pools.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n", encoding="utf-8")
    return live


def test_missing_on_trading_day_after_eod_is_warning(monkeypatch, tmp_path):
    """交易日盘后无留档 → missing（数据没到，不是策略没信号）。"""
    monkeypatch.setattr(data_health, "LIVE", _make_days(tmp_path, ["20261120"]))
    monkeypatch.setenv("WUKONG_CHECK_DAY", "20261123")   # 周一交易日
    now = datetime(2026, 11, 23, 16, 0)                   # 盘后
    r = data_health.check(now)
    assert r["status"] == "missing" and r["ok"] is False
    assert "recorder" in r["message"] or "未落盘" in r["message"]
    assert r["latest_archive_day"] == "20261120"
    monkeypatch.delenv("WUKONG_CHECK_DAY")


def test_intraday_no_archive_is_ok(monkeypatch, tmp_path):
    """交易日盘中无当日留档 → intraday（正常，尚未收盘）。"""
    monkeypatch.setattr(data_health, "LIVE", _make_days(tmp_path, ["20261120"]))
    monkeypatch.setenv("WUKONG_CHECK_DAY", "20261123")
    now = datetime(2026, 11, 23, 10, 30)
    r = data_health.check(now)
    assert r["status"] == "intraday" and r["ok"] is True
    monkeypatch.delenv("WUKONG_CHECK_DAY")


def test_today_present_is_ok(monkeypatch, tmp_path):
    """当日留档已落盘 → ok。"""
    monkeypatch.setattr(data_health, "LIVE",
                        _make_days(tmp_path, ["20261120"], with_today=True, day="20261123"))
    monkeypatch.setenv("WUKONG_CHECK_DAY", "20261123")
    now = datetime(2026, 11, 23, 16, 0)
    r = data_health.check(now)
    assert r["status"] == "ok" and r["ok"] is True and r["today_archive_ok"] is True
    monkeypatch.delenv("WUKONG_CHECK_DAY")


def test_weekend_without_archive_is_ok(monkeypatch, tmp_path):
    """周末无当日留档属正常（2026-11-21 是周六）。"""
    monkeypatch.setattr(data_health, "LIVE", _make_days(tmp_path, ["20261120"]))
    monkeypatch.setenv("WUKONG_CHECK_DAY", "20261121")
    now = datetime(2026, 11, 21, 16, 0)
    r = data_health.check(now)
    assert r["status"] == "ok" and r["is_trading_day"] is False
    monkeypatch.delenv("WUKONG_CHECK_DAY")


def test_gap_days():
    assert data_health._gap_days("20261120", "20261123") == 3
    assert data_health._gap_days("bad", "20261123") == 0
