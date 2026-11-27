# -*- coding: utf-8 -*-
"""2026-11-27 悟空真经策略实战审计修复与升级回归测试套件。"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import buy_signals, evolution, fill_verify, live_sim, theme_match  # noqa: E402


def _frame(ts: str, rows: list[dict]) -> dict:
    return {
        "ts": ts,
        "zt": {"n": len(rows), "top": rows[:30], "all": rows, "ok": True, "all_complete": True},
        "zb": {"n": 0, "top": []},
        "dt": {"n": 0, "top": []},
    }


# ---------------------------------------------------------------------------
# P0-1: 09:25 开盘一字兔炸板后永久锁定兔子身份，绝不转性为乌龟被时序穿越买入
# ---------------------------------------------------------------------------

def test_p0_1_open_rabbit_never_turns_into_turtle_after_blowout(monkeypatch) -> None:
    """复现 2026-11-27 603386 骏亚科技：09:25 一字二板，09:30 炸板后不得被当成乌龟买入。"""
    rabbit_alive = {
        "ts_code": "000009", "name": "稳封兔", "price": 10.0, "pct": 10.0,
        "streak": 2, "turnover": 0.8, "seal_fund": 2e8, "broken": 0, "first_seal": 92500,
    }
    rabbit_blown_f1 = {
        "ts_code": "603386", "name": "骏亚科技", "price": 15.0, "pct": 10.0,
        "streak": 2, "turnover": 0.47, "seal_fund": 1.2e8, "broken": 0, "first_seal": 92502,
    }
    rabbit_blown_f2 = {
        **rabbit_blown_f1, "turnover": 6.2, "broken": 3, "seal_fund": 0.3e8,
    }
    real_turtle = {
        "ts_code": "002298", "name": "真乌龟", "price": 12.0, "pct": 10.0,
        "streak": 2, "turnover": 8.5, "seal_fund": 0.8e8, "broken": 0, "first_seal": 93500,
    }
    frames = [
        _frame("2026-11-27T09:25:31", [rabbit_alive, rabbit_blown_f1]),
        _frame("2026-11-27T09:45:22", [rabbit_alive, rabbit_blown_f2, real_turtle]),
    ]
    monkeypatch.setattr(
        live_sim, "_theme_tags",
        lambda day, **kw: {"000009": ["算力"], "603386": ["算力"], "002298": ["算力"]},
    )
    picked, _ = live_sim.collect_guitu_targets(frames, "20261127")
    assert "002298" in picked
    assert "603386" not in picked, "开盘一字兔炸板后绝不可转性为乌龟被买入"


# ---------------------------------------------------------------------------
# P0-2: 盘中跌停封死（bid1_vol == 0）阻止盘中穿仓平仓
# ---------------------------------------------------------------------------

def test_p0_2_intraday_limit_down_locked_blocks_sell(monkeypatch) -> None:
    from backend import feeds
    monkeypatch.setattr(live_sim, "_cn_today_str", lambda: "20261127")
    monkeypatch.setattr(live_sim, "_live_price", lambda code: 9.0)
    monkeypatch.setattr(
        live_sim, "_minute_points",
        lambda day, code, live=False: [{"t": "09:30", "price": 9.0}, {"t": "09:31", "price": 9.0}],
    )
    monkeypatch.setattr(
        feeds, "single_quote",
        lambda code, ttl=5.0: {"price": 9.0, "prev_close": 10.0, "bid1": 0.0, "bid1_vol": 0.0},
    )
    pos = {"ts_code": "000001", "entry_date": "20261126", "entry_price": 10.0, "broken_max": 0}
    daily = [{"date": "2026-11-26", "close": 10.0, "zt": True}]
    plan = live_sim.build_sell_plan(pos, daily, code="000001", now_hm="09:35")
    dec = plan["decision"]
    assert dec["action"] == "hold"
    assert dec["kind"] == "exit_blocked_intraday"


# ---------------------------------------------------------------------------
# P0-4: 高标梯队 hi3_rows 排除 ST 股（防止 ST汇洲 污染高标情绪）
# ---------------------------------------------------------------------------

def test_p0_4_emotion_intraday_excludes_st_from_hi3(monkeypatch) -> None:
    from backend import ice_gate
    prev_df = pl.DataFrame({
        "ts_code": ["002122", "600001"],
        "prev_streak": [5, 3],
        "prev_is_zt": [True, True],
    })
    quotes = pl.DataFrame({
        "ts_code": ["002122", "600001"],
        "name": ["ST汇洲", "正常高标"],
        "pct": [5.0, 10.0],
        "quote_time": ["2026-11-27 10:00:00", "2026-11-27 10:00:00"],
    })
    monkeypatch.setattr(ice_gate, "prev_day_state", lambda p: prev_df)
    monkeypatch.setattr(ice_gate, "_quote_stamp", lambda q: "2026-11-27 10:00:00")
    res = live_sim._emotion_intraday_lists("20261127", pl.DataFrame(), quotes=quotes, include_hi3=True)
    assert res is not None
    _hi_ret, _prem, hi3_rows, _stamp = res
    codes = [r["ts_code"] for r in hi3_rows]
    assert "600001" in codes
    assert "002122" not in codes, "ST 股不得进入高标梯队 hi3_rows"


# ---------------------------------------------------------------------------
# P0-5: 催化动态权重修复 net_ret 字段拼写错误
# ---------------------------------------------------------------------------

def test_p0_5_causal_catalyst_weight_reads_net_return() -> None:
    orders = {
        "20261120|000001|B1": {
            "id": "20261120|000001|B1", "day": "20261120",
            "ts_code": "000001", "catalyst_score": 2.0,
        },
        "20261121|000002|B1": {
            "id": "20261121|000002|B1", "day": "20261121",
            "ts_code": "000002", "catalyst_score": 1.5,
        },
    }
    trades = [
        {"order_id": "20261120|000001|B1", "entry_date": "20261120", "exit_date": "20261121",
         "ts_code": "000001", "net_return": -0.08},
        {"order_id": "20261121|000002|B1", "entry_date": "20261121", "exit_date": "20261124",
         "ts_code": "000002", "net_return": -0.06},
    ]
    w = evolution.causal_catalyst_weight_before("20261125", orders, trades)
    assert w < 1.0, f"带催化交易连续亏损时权重应从 1.0 衰减，实际 {w}"


# ---------------------------------------------------------------------------
# P0-7: 题材脏词归位 + F10 僵尸概念过滤
# ---------------------------------------------------------------------------

def test_p0_7_theme_axis_dirty_tags_fixed_and_f10_zombie_filtered(monkeypatch) -> None:
    theme_match.clear_cache()
    assert theme_match.map_to_axes(["钠电池正极材料"], use_cooccurrence=False) == ["new_energy"]
    assert theme_match.map_to_axes(["联碱"], use_cooccurrence=False) == ["chemical"]

    # L4 F10 僵尸概念过滤：当日原生池是「算力」，F10 返回「白酒」(僵尸) + 「液冷」(活跃)，只保留「液冷」
    monkeypatch.setattr(theme_match, "historical_stock_tags", lambda **kw: {})
    monkeypatch.setattr(theme_match, "_news_tags_for_codes", lambda **kw: {})
    monkeypatch.setattr(theme_match, "_lhb_tags_for_codes", lambda **kw: {})
    monkeypatch.setattr(theme_match, "_fetch_f10_concepts", lambda c, **kw: ["白酒", "液冷"])
    enriched = theme_match.enrich_stock_tags(
        {"000001": ["算力"], "000002": ["光模块"], "000003": []},
        codes=["000001", "000002", "000003"],
        allow_remote=False,
    )
    assert "液冷" in enriched.get("000003", [])
    assert "白酒" not in enriched.get("000003", [])


# ---------------------------------------------------------------------------
# P1-1: 昨日首板池 → 今日一进二临封扫板双轨雷达 + 伪一字降权
# ---------------------------------------------------------------------------

def test_p1_1_first_to_second_sweep_and_quasi_oneword_deprioritize() -> None:
    hits = buy_signals.scan_first_to_second_sweep(
        theme_rabbits={"算力": ["603324"]},
        prev_first_board_by_theme={"算力": ["002771", "000888", "000999"]},
        quotes={
            # 002771: +9.1%, 卖一贴涨停且仅 180万 → executable=True
            "002771": {
                "name": "真视通", "price": 10.91, "prev_close": 10.0, "open": 10.2,
                "pct": 9.1, "turnover": 8.5, "ask1": 10.99, "ask1_vol": 1600,
                "amount": 3e8, "quote_time": "09:38:00",
            },
            # 000888: +7.8%, 无五档 ask1_vol → sweep_shadow (executable=False)
            "000888": {
                "name": "影子观察", "price": 10.78, "prev_close": 10.0, "open": 10.3,
                "pct": 7.8, "turnover": 6.0, "quote_time": "09:38:00",
            },
            # 000999: +5.0% 未达 +7.5% → 过滤
            "000999": {
                "name": "未冲板", "price": 10.5, "prev_close": 10.0, "open": 10.1,
                "pct": 5.0, "turnover": 4.0,
            },
        },
        already_sealed={"603324"},
    )
    by_c = {h["ts_code"]: h for h in hits}
    assert "002771" in by_c and by_c["002771"]["executable"] is True
    assert by_c["002771"]["exec_mode"] == "pre_seal_sweep"
    assert "000888" in by_c and by_c["000888"]["executable"] is False
    assert by_c["000888"]["exec_mode"] == "sweep_shadow"
    assert "000999" not in by_c

    # 伪一字降权：09:30:00 极低换手(<1.5%) + 极高封单比(≥8%) 应排在可成交换手乌龟之后
    groups = {
        "000001": {
            "name": "伪一字", "guitu_theme": "算力", "guitu_rabbits": ["兔"],
            "first_seal": "09:30:00", "turn_at_seal": 0.9, "broken_max": 0,
            "seal_fund": 5e8, "float_mv": 30e8, "first": {},
        },
        "000002": {
            "name": "换手乌龟", "guitu_theme": "算力", "guitu_rabbits": ["兔"],
            "first_seal": "09:34:00", "turn_at_seal": 7.5, "broken_max": 0,
            "seal_fund": 1.2e8, "float_mv": 30e8, "first": {},
        },
    }
    selected, _ = buy_signals.rank_and_focus_candidates(groups, max_per_theme=1, max_total=1)
    assert list(selected.keys()) == ["000002"]


# ---------------------------------------------------------------------------
# P1-2: 早盘缩量厚封防假撤单洗盘保护
# ---------------------------------------------------------------------------

def test_p1_2_early_anti_washout_cancel_guard() -> None:
    # 复现 002298 中电鑫龙 09:45：峰值 1.158亿 → 剩余 0.596亿(≥5000万)，同期成交仅 256万(2.2% ≤ 3%)
    ev = fill_verify.evaluate_cancel_trigger(
        seal_fund=0.5958e8,
        peak_seal=1.158e8,
        prev_seal=1.158e8,
        interval_traded_amt=0.0256e8,
        day_amount=4.0e8,
        broken=0,
        ts="2026-11-27T09:45:22",
        code="002298",
    )
    assert ev["should_cancel"] is False, "早盘缩量厚封假撤单洗盘不应误撤"

    # 若剩余封单已跌破 5000万且成交放量（非洗盘），仍应正常撤单
    ev2 = fill_verify.evaluate_cancel_trigger(
        seal_fund=0.45e8,
        peak_seal=1.0e8,
        prev_seal=1.0e8,
        interval_traded_amt=0.05e8,
        day_amount=4.0e8,
        broken=0,
        ts="2026-11-27T09:45:22",
        code="002298",
    )
    assert ev2["should_cancel"] is True


# ---------------------------------------------------------------------------
# P1-3: 退潮初期区分「同题材退潮拦截」vs「跨题材多兔高低切 0.5× 试错」
# ---------------------------------------------------------------------------

def test_p1_3_high_low_cut_downgrades_ebb_block() -> None:
    gate = {
        "available": True,
        "premium": -0.8,
        "high_board_pulse": {
            "state": "drag",
            "leaders": [{"ts_code": "600001", "status": "跌停", "themes": ["消费"]}],
        },
    }
    ice = {"available": True, "level": "L2", "is_ice": False}
    # 同题材无新主线 → 严格拦截
    r_block = live_sim.market_climax_and_ebb_guard("20261127", gate, ice, rabbit_themes={"消费": ["000001", "000002"]})
    assert r_block["ebb_block"] is True
    # 新主线「算力」2只一字兔且与跌停高标「消费」零重叠 → 降级为 0.50× 高低切试错
    r_cut = live_sim.market_climax_and_ebb_guard("20261127", gate, ice, rabbit_themes={"算力": ["603324", "002771"]})
    assert r_cut["ebb_block"] is False
    assert r_cut["ebb_trial_mult"] == pytest.approx(0.50)
    assert "算力" in r_cut["high_low_cut_themes"]


# ---------------------------------------------------------------------------
# P1-4 / P2: 12维事前因子贝叶斯收缩引擎 + broken_at_entry 锁死 + 对称负向侦察蚁
# ---------------------------------------------------------------------------

def test_p1_4_12_factor_engine_pre_trade_lock_and_symmetric_scout() -> None:
    # 1) broken_at_entry 优先于事后 broken_max（消除训练-推理错位）
    rec = {
        "guitu_theme": "算力", "guitu_rabbits": ["兔1", "兔2"],
        "first_seal": "09:33:00", "turn_at_seal": 9.0,
        "broken_at_entry": 0, "broken_max": 5,
    }
    pk = evolution.make_path_key(rec)
    assert "0炸" in pk and "烂板" not in pk
    facs = evolution.extract_factors(rec)
    assert facs["seal_quality"] == "0炸"

    # 2) 对称负向侦察蚁反馈（未成交但次日大跌 <= -5% 记负反馈）
    rw_neg = evolution.compute_reward(None, filled=False, missed_next_ret=-0.08)
    assert rw_neg["kind"] == "scout_dodge"
    assert rw_neg["reward"] == pytest.approx(-0.04)

    # 3) 12维因果因子打分引擎
    orders = {
        "20261120|000001|B1": {
            "day": "20261120", "ts_code": "000001", "guitu_theme": "算力",
            "guitu_rabbits": ["兔1", "兔2"], "first_seal": "09:32:00",
            "turn_at_seal": 9.5, "broken_at_entry": 0, "status": "filled",
        },
        "20261121|000002|B1": {
            "day": "20261121", "ts_code": "000002", "guitu_theme": "算力",
            "guitu_rabbits": ["兔1", "兔2"], "first_seal": "09:34:00",
            "turn_at_seal": 10.2, "broken_at_entry": 0, "status": "filled",
        },
    }
    trades = [
        {"entry_date": "20261120", "exit_date": "20261121", "ts_code": "000001", "net_return": 0.14},
        {"entry_date": "20261121", "exit_date": "20261124", "ts_code": "000002", "net_return": 0.12},
    ]
    cands = {
        "000003": {
            "ts_code": "000003", "guitu_theme": "AI传媒",
            "guitu_rabbits": ["兔A", "兔B"], "first_seal": "09:35:00",
            "turn_at_seal": 11.0, "first": {"broken": 0, "turnover": 11.0},
        }
    }
    res = evolution.causal_factor_scores_before("20261125", orders, trades, cands)
    assert "000003" in res["by_code"]
    assert res["by_code"]["000003"]["size_mult"] > 1.0
    assert res["by_code"]["000003"]["n_matched"] >= 3
