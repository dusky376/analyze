# -*- coding: utf-8 -*-
"""2026-11-25 升级套件验证测试：自适应卖点、买点评分/扫板雷达、信息素奖惩进化、数据源与前端契约。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import buy_signals, evolution, feeds, minute_flow, sell_rules  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 自适应卖点引擎（sell_rules.py）
# ---------------------------------------------------------------------------

def test_adaptive_thresholds_board_and_rabbit():
    t10 = sell_rules.adaptive_thresholds("002931")
    t20 = sell_rules.adaptive_thresholds("301391")
    assert t10["limit_pct"] == pytest.approx(0.10)
    assert t20["limit_pct"] == pytest.approx(0.20)
    assert t20["drop_pct"] > t10["drop_pct"]

    t_weak = sell_rules.adaptive_thresholds("002931", rabbit_weak=True)
    assert t_weak["drop_pct"] == pytest.approx(t10["drop_pct"] * 0.60, rel=1e-2)
    assert t_weak["pullback_pct"] == pytest.approx(t10["pullback_pct"] * 0.60, rel=1e-2)


def test_adaptive_auction_dump_path_a():
    pts = [{"t": "09:30", "price": 9.40}, {"t": "09:31", "price": 9.35}]
    sig = sell_rules.find_adaptive_signal(
        pts, code="002931", prev_close=10.0, open_price=9.40,
        entry_kind="broke_and_stayed",
    )
    assert sig is not None
    assert sig["kind"] == "auction_dump"
    assert sig["t"] == "09:30"


def test_adaptive_pullback_requires_real_surge():
    # 低开后缓慢阴跌（从未真正冲高），不应触发「冲高回落」
    pts = [
        {"t": "09:31", "price": 9.80},
        {"t": "09:40", "price": 9.72},
        {"t": "09:50", "price": 9.64},
        {"t": "10:00", "price": 9.56},
        {"t": "10:10", "price": 9.48},
    ]
    sig = sell_rules.find_adaptive_signal(
        pts, code="002931", prev_close=10.0, open_price=9.80,
        entry_kind="sealed",
    )
    assert sig is None


def test_adaptive_late_weak_exit_path_b():
    pts = [
        {"t": "09:31", "price": 10.10, "avg_price": 10.10},
        {"t": "14:42", "price": 9.95, "avg_price": 10.08},
    ]
    sig = sell_rules.find_adaptive_signal(
        pts, code="002931", prev_close=10.0, open_price=10.10,
        entry_kind="sealed",
    )
    assert sig is not None
    assert sig["kind"] == "late_weak_exit"
    assert sig["t"] == "14:42"


# ---------------------------------------------------------------------------
# 2. 买点质量评估与临封扫板雷达（buy_signals.py）
# ---------------------------------------------------------------------------

def test_buy_quality_good_vs_rotten_and_bleed():
    good = buy_signals.evaluate_turtle_quality(
        {"ts_code": "002771", "broken_max": 0, "turn_at_seal": 7.8, "first_seal": "09:32:00"},
        rabbit_count=2, peak_seal_yi=1.5, current_seal_yi=1.4,
    )
    assert good["grade"] == "A"
    assert good["cancel_warn"] is False

    rotten = buy_signals.evaluate_turtle_quality(
        {"ts_code": "600715", "broken_max": 8, "turn_at_seal": 28.1, "first_seal": "13:15:00"},
        rabbit_count=1,
    )
    assert rotten["grade"] == "REJECT"
    assert rotten["reject"] is True

    bleed = buy_signals.evaluate_turtle_quality(
        {"ts_code": "000819", "broken_max": 1, "turn_at_seal": 10.0, "first_seal": "09:35:00"},
        rabbit_count=1, peak_seal_yi=2.0, current_seal_yi=0.4,
    )
    assert bleed["cancel_warn"] is True
    assert bleed["grade"] == "REJECT"


def test_pre_seal_sweep_radar():
    cands = buy_signals.scan_pre_seal_candidates(
        theme_rabbits={"算力": ["603324"]},
        theme_members={"算力": ["603324", "002771", "301391", "000001"]},
        quotes={
            "603324": {"name": "盛剑科技", "price": 20.0, "open": 20.0, "pct": 10.0},
            "002771": {"name": "真视通", "price": 10.8, "open": 10.2, "pct": 8.0},
            "301391": {"name": "卡莱特", "price": 57.5, "open": 52.0, "pct": 15.0},
            "000001": {"name": "弱票", "price": 10.2, "open": 10.1, "pct": 2.0},
        },
    )
    codes = [c["ts_code"] for c in cands]
    assert "002771" in codes
    assert "301391" in codes
    assert "603324" not in codes  # 兔子本身排除
    assert "000001" not in codes  # 涨幅不足 7%


# ---------------------------------------------------------------------------
# 3. 信息素进化 + 非对称奖惩引擎（evolution.py）
# ---------------------------------------------------------------------------

def test_evolution_asymmetric_reward_and_causal_shadow():
    r_win = evolution.compute_reward(0.16, exit_blocked=False, filled=True)
    r_loss = evolution.compute_reward(-0.08, exit_blocked=False, filled=True)
    r_nuc = evolution.compute_reward(-0.22, exit_blocked=True, filled=True)
    assert r_win["kind"] == "reward_big"
    assert r_win["reward"] > 0.16
    assert r_loss["reward"] == pytest.approx(-0.096, rel=1e-3)
    assert r_nuc["kind"] == "penalty_nuclear"
    assert r_nuc["reward"] == pytest.approx(-0.44, rel=1e-3)

    # 因果演化：第一笔核按钮重罚后，第二笔同路径仓位系数应自动下调
    orders = [
        {"day": "20261116", "ts_code": "600715", "name": "文投", "guitu_theme": "AI传媒",
         "first_seal": "09:30:24", "turn_at_seal": 28.0, "broken_max": 8, "status": "filled"},
        {"day": "20261120", "ts_code": "000819", "name": "后续烂板", "guitu_theme": "AI传媒",
         "first_seal": "09:32:00", "turn_at_seal": 25.0, "broken_max": 6, "status": "filled"},
    ]
    trades = [
        {"entry_date": "20261116", "exit_date": "20261118", "ts_code": "600715", "name": "文投",
         "net_return": -0.26, "exit_blocked": True},
        {"entry_date": "20261120", "exit_date": "20261121", "ts_code": "000819", "name": "后续烂板",
         "net_return": -0.15, "exit_blocked": True},
    ]
    res = evolution.evolve_from_records(orders, trades)
    assert res["available"] is True
    assert res["n_paths"] == 1
    p = res["paths"][0]
    assert p["pheromone"] < 1.0
    assert p["size_mult"] < 1.0
    assert p["nuclear_n"] == 2
    # 影子账本在第二笔自动降仓，均值亏损应小于基准
    sc = res["shadow_compare"]
    assert sc["shadow_avg_ret"] > sc["baseline_avg_ret"]
    assert sc["delta_pp"] > 0


# ---------------------------------------------------------------------------
# 4. 数据源修复、分钟线持久化与 Flask API 契约
# ---------------------------------------------------------------------------

def test_minute_flow_bse_symbol_and_persistence(tmp_path, monkeypatch):
    assert minute_flow._symbol("920001") == "bj920001"
    assert minute_flow._symbol("600519") == "sh600519"
    assert minute_flow._symbol("002931") == "sz002931"
    assert minute_flow._em_secid("600519") == "1.600519"
    assert minute_flow._em_secid("002931") == "0.002931"

    monkeypatch.setattr(minute_flow.paths, "live_dir", lambda: tmp_path)
    pts = [
        {"t": "09:31", "price": 10.12, "avg_price": 10.10, "volume": 1200, "amount": 1214400, "cum_amount": 1214400, "source": "eastmoney_1min"},
        {"t": "09:32", "price": 10.20, "avg_price": 10.14, "volume": 1500, "amount": 1530000, "cum_amount": 2744400, "source": "eastmoney_1min"},
    ]
    saved = minute_flow.persist_minute_series("002931", "20261125", pts)
    assert saved is not None and saved.is_file()
    loaded = minute_flow.load_persisted_minute("002931", "20261125")
    assert loaded is not None and len(loaded) == 2
    assert loaded[1]["avg_price"] == pytest.approx(10.14)


def test_sina_quote_parser():
    sample = (
        'var hq_str_sz002931="盛通股份,10.10,10.00,10.50,10.80,10.05,10.49,10.50,'
        '1200000,12450000,50000,10.49,0,0,0,0,0,0,0,0,30000,10.50,0,0,0,0,0,0,0,0,'
        '2026-11-25,10:15:30,00";'
    )
    row = feeds._parse_sina_line(sample)
    assert row is not None
    assert row["ts_code"] == "002931"
    assert row["price"] == pytest.approx(10.50)
    assert row["prev_close"] == pytest.approx(10.00)
    assert row["pct"] == pytest.approx(5.0)
    assert row["quote_time"] == "20261125101530"
    assert row["feed_source"] == "sina"


def test_api_evolution_and_frontend_contract():
    import app as app_module
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    r = client.get("/api/evolution")
    assert r.status_code == 200
    d = json.loads(r.data.decode("utf-8"))
    assert d["available"] is True
    assert "paths" in d and "shadow_compare" in d

    # 再次核对前端契约（含文件清单与禁用词）
    html = client.get("/").data.decode("utf-8")
    js = client.get("/app.js").data.decode("utf-8")
    assert "悟空真经" in html
    assert "今日作战台" in html and "持仓与账本" in html and "进化与回放" in html
    for word in ("威科夫", "冰点二板", "规则 1", "LLM", "蚂蚁觅食",
                 "情绪周期", "周期看板", "题材雷达", "决策总线", "周度复盘"):
        assert word not in html + js
