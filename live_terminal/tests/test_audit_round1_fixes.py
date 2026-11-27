# -*- coding: utf-8 -*-
"""Round-1 审计报告全量修复回归测试（P0-1 ~ P0-4 & P1-1 ~ P1-5）。"""
from __future__ import annotations

import pytest


def test_p0_1_partial_profit_accepts_end_and_preserved_in_decide_adaptive():
    from backend import sell_rules

    pts = [
        {"t": "09:30", "price": 10.20, "avg_price": 10.20},
        {"t": "09:40", "price": 10.75, "avg_price": 10.50},
        {"t": "09:50", "price": 10.45, "avg_price": 10.52},  # 回撤 >2% 且跌破均价线
        {"t": "10:10", "price": 10.40, "avg_price": 10.48},
    ]
    # 带 end 截断至 09:40 时未触发
    assert sell_rules.find_partial_profit_signal(pts, code="000889", prev_close=10.0, end="09:40") is None
    # 带 end 截断至 09:55 时正常触发，不抛 TypeError
    psig = sell_rules.find_partial_profit_signal(pts, code="000889", prev_close=10.0, end="09:55")
    assert psig is not None and psig["kind"] == "partial_take_profit" and psig["ratio"] == pytest.approx(0.50)

    dec = sell_rules.decide_adaptive(
        "sealed", points=pts, code="000889", prev_close=10.0, open_price=10.20, end="09:55",
    )
    assert dec.get("partial_signal") is not None


def test_p0_3_path_b_morning_above_vwap_not_washed_out():
    """路径 B 早盘 09:30-10:30 高于昨收+2% 且在 VWAP 上方的强势换手回踩不得被 3% 回撤洗飞。"""
    from backend import sell_rules

    # 高开 +4.5% → 09:31 冲 +7.5% (10.75) → 09:33 回踩 +3.8% (10.38)，仍在 VWAP 10.35 上方
    pts = [
        {"t": "09:30", "price": 10.45, "avg_price": 10.45, "volume": 1000},
        {"t": "09:31", "price": 10.75, "avg_price": 10.55, "volume": 1200},
        {"t": "09:33", "price": 10.38, "avg_price": 10.35, "volume": 800},
        {"t": "09:40", "price": 10.95, "avg_price": 10.50, "volume": 1500},
    ]
    sig = sell_rules.find_adaptive_signal(
        pts, code="000889", prev_close=10.0, open_price=10.45, entry_kind="sealed",
    )
    assert sig is None or sig["kind"] != "pullback"


def test_p0_3_path_a_decide_adaptive_auction_cut_and_peer_turtle():
    from backend import sell_rules

    pts = [{"t": "09:30", "price": 9.60}, {"t": "09:35", "price": 9.65}]
    # 三弱齐聚（本乌龟低开 -4%、兔子核按钮、无同题材其他乌龟带动）→ 竞价直走
    dec_cut = sell_rules.decide_adaptive(
        "broke_and_stayed", points=pts, code="000889", prev_close=10.0, open_price=9.60,
        rabbit_nuclear=True, peer_turtle_strong=False,
    )
    assert dec_cut["action"] == "sell" and dec_cut["kind"] == "auction_cut"

    # 若同题材其他乌龟走强带动 → 不竞价一刀切，继续观察到 10:00
    dec_hold = sell_rules.decide_adaptive(
        "broke_and_stayed", points=pts, code="000889", prev_close=10.0, open_price=9.60,
        rabbit_nuclear=True, peer_turtle_strong=True,
    )
    assert dec_hold["kind"] != "auction_cut"


def test_p0_2_weak_to_strong_marks_vol_unverified():
    from backend import buy_signals

    res = buy_signals.scan_reseal_and_weak_to_strong([
        {"ts_code": "002261", "name": "拓维信息", "prev_sealed": True, "open_gap": 4.0, "auction_vol_ratio": None},
        {"ts_code": "000889", "name": "中嘉博创", "prev_sealed": True, "open_gap": 3.5, "auction_vol_ratio": 0.08},
    ])
    w2s = {x["ts_code"]: x for x in res["weak_to_strong_auction"]}
    assert w2s["002261"]["vol_unverified"] is True
    assert w2s["000889"]["vol_unverified"] is False


def test_p0_4_recorder_intel_interval_not_stuck_at_180():
    from backend import recorder

    assert recorder.INTEL_EVERY <= 30.0
    assert recorder.INTEL_EVERY_BURST < recorder.INTEL_EVERY


def test_p1_5_cancel_counterfactual_skips_invalid_historical_bug():
    from backend import evolution

    orders = {
        "20261127|000889|B1": {
            "id": "20261127|000889|B1", "ts_code": "000889", "status": "cancelled",
            "cancel_kind": "seal_bleed", "missed_next_ret": 0.10, "missed_next_day": "20261128",
            "invalid_historical_bug": True,
        }
    }
    res = evolution.evaluate_cancel_counterfactual(orders, as_of_day="20261201")
    assert res["n_cancelled"] == 0 and res["n_verified"] == 0
