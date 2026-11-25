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


# ---------------------------------------------------------------------------
# 5. 审核回归测试（dict 形态账本 / K 线买卖点标记 / 盘中兜底门控 / 反馈接入）
# ---------------------------------------------------------------------------

def test_evolution_handles_dict_orders_and_idle_evaporation():
    """真实 sim_state.json 的 orders 是 dict，必须正常演化且空仓日自然挥发。"""
    orders_dict = {
        "20261116|600715|B1": {
            "day": "20261116", "ts_code": "600715", "name": "文投",
            "industry": "影视院线", "turnover": 28.0, "broken_max": 8,
            "guitu": {"evidence": {"first_seal": 93024}}, "status": "filled",
        },
        "20261116|002771|B1": {
            "day": "20261116", "ts_code": "002771", "name": "真视通",
            "industry": "算力", "turnover": 6.0, "broken_max": 0,
            "guitu": {"evidence": {"first_seal": 93200}}, "status": "queued_unfilled",
            "missed_next_day": "20261117", "missed_next_ret": 0.10,
        },
    }
    trades = [
        {"entry_date": "20261116", "exit_date": "20261118", "ts_code": "600715",
         "name": "文投", "net_return": -0.26, "exit_blocked": True},
    ]
    res_no_idle = evolution.evolve_from_records(orders_dict, trades)
    res_idle = evolution.evolve_from_records(
        orders_dict, trades,
        all_days=["20261117", "20261118", "20261119", "20261120", "20261121"],
    )
    assert res_no_idle["available"] is True
    assert res_no_idle["n_paths"] == 2
    # 经过 3 个空仓日自然挥发，核按钮惩罚后的路径应更接近 1.0
    p_no = {p["path"]: p["pheromone"] for p in res_no_idle["paths"]}
    p_id = {p["path"]: p["pheromone"] for p in res_idle["paths"]}
    nuc_key = next(k for k in p_no if "影视院线" in k)
    assert p_id[nuc_key] > p_no[nuc_key]
    # 因果查询：20261118 之前只能看到 11-17 侦察蚁，看不到 11-18 才结算的核按钮
    m_before = evolution.causal_multipliers_before("20261118", orders_dict, trades)
    assert nuc_key not in m_before
    m_after = evolution.causal_multipliers_before("20261119", orders_dict, trades)
    assert nuc_key in m_after and m_after[nuc_key]["size_mult"] < 1.0


def test_chart_trade_markers_with_dict_positions(tmp_path, monkeypatch):
    """positions 为 dict 时，charts._trade_markers 必须正常返回买卖点标记。"""
    from backend import charts, live_sim
    sf = tmp_path / "sim_state.json"
    sf.write_text(json.dumps({
        "positions": {
            "20261118|000707|B1": {
                "ts_code": "000707", "entry_date": "20261118", "entry_price": 5.05,
                "exit_date": "2026-11-23", "exit_price": 3.93, "exit_reason": "断板",
            }
        },
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(live_sim, "STATE_FILE", sf)
    markers = charts._trade_markers("000707")
    kinds = {m["kind"] for m in markers}
    assert kinds == {"buy", "sell"}


def test_intraday_fallback_time_gate_and_adaptive_plan(monkeypatch):
    """盘中未到时间边界（A<10:00 / B<14:55）绝不提前兜底平仓，但真实信号可立即触发。"""
    from backend import live_sim
    monkeypatch.setattr(live_sim, "_cn_today_str", lambda: "20261125")
    monkeypatch.setattr(live_sim, "_live_price", lambda code: 10.05)
    monkeypatch.setattr(
        live_sim, "_minute_points",
        lambda day, code, live=False: [
            {"t": "09:31", "price": 10.00},
            {"t": "09:35", "price": 10.05},
        ],
    )
    daily = [{"date": "2026-11-24", "close": 10.00, "zt": False}]
    pos = {"ts_code": "002931", "entry_date": "20261124", "entry_price": 10.00, "broken_max": 2}

    # 09:35 无信号且未到 10:00 → 保持持有（pending），不可提前兜底卖出
    p_early = live_sim.build_sell_plan(pos, daily, code="002931", now_hm="09:35")
    assert p_early["engine"] == "adaptive"
    assert p_early["decision"]["action"] == "hold"
    assert p_early["decision"]["kind"] == "pending"

    # 10:01 无信号且已到 10:00 → 触发时间边界兜底卖出
    p_due = live_sim.build_sell_plan(pos, daily, code="002931", now_hm="10:01")
    assert p_due["decision"]["action"] == "sell"
    assert p_due["decision"]["kind"] == "fallback"


def test_sealed_censored_with_today_bar_stays_holding(monkeypatch):
    """今日日线已落盘且仍涨停（censored），绝不可把今日收盘当昨收误判为断板卖出。"""
    from backend import live_sim
    monkeypatch.setattr(live_sim, "_cn_today_str", lambda: "20261125")
    monkeypatch.setattr(live_sim, "_live_price", lambda code: 38.56)
    daily = [
        {"date": "2026-11-24", "close": 35.05, "zt": True},
        {"date": "2026-11-25", "close": 38.56, "zt": True},
    ]
    pos = {"ts_code": "002242", "entry_date": "20261124", "entry_price": 35.05, "broken_max": 1}
    plan = live_sim.build_sell_plan(pos, daily, code="002242", now_hm="22:00")
    assert plan["decision"]["action"] == "hold"
    assert plan["decision"]["kind"] == "pending"


def test_plan_orders_applies_causal_evo_multiplier():
    from backend import live_sim
    cands = [
        {"ts_code": "002931", "first": {"price": 10.0}},
        {"ts_code": "600715", "first": {"price": 10.0}},
    ]
    plan = live_sim.plan_orders(
        cands, size_factor=1.0, capital=1_000_000.0,
        evo_mults={"002931": 1.20, "600715": 0.50},
    )
    by_c = {x["code"]: x for x in plan}
    assert by_c["002931"]["size_yuan"] == pytest.approx(120_000.0)
    assert by_c["600715"]["size_yuan"] == pytest.approx(50_000.0)
    assert "信息素反馈" in by_c["600715"]["budget_reason"]


# ---------------------------------------------------------------------------
# 6. 外盘日韩美股风偏 / 国内 7×24 快讯映射 / 买卖点聚焦与防洗盘增强
# ---------------------------------------------------------------------------

def test_overseas_score_and_us_theme_mapping():
    from backend import overseas
    sina_sample = (
        'var hq_str_znb_KOSPI="首尔综合指数,4697.51,-308.52,-6.16,2:27 AM,1759127220,2026-11-25,14:32:30";\n'
        'var hq_str_znb_NKY="日经225,57011.40,-1633.23,-2.78,2:12 AM,1759126320,2026-11-25,14:30:01";\n'
        'var hq_str_hf_CHA50CFD="12366.84,,12368,12370,12462,12342,23:12:35,12455.00,12459,445830,17,3,2026-11-25,富时中国A50期货";\n'
    )
    parsed = overseas._parse_sina_global(sina_sample)
    assert parsed["znb_KOSPI"]["pct"] == pytest.approx(-6.16)
    assert parsed["znb_NKY"]["pct"] == pytest.approx(-2.78)
    assert parsed["hf_CHA50CFD"]["pct"] < 0

    sent = overseas.compute_overseas_score([
        {"key": "KOSPI", "pct": -6.16},
        {"key": "N225", "pct": -2.78},
        {"key": "A50", "pct": -0.71},
        {"key": "IXIC", "pct": -3.34},
        {"key": "SOX", "pct": -6.09},
    ])
    assert sent["risk_level"] == "risk_off"
    assert sent["size_mult"] == pytest.approx(0.70)
    assert sent["score"] < -50

    cats = overseas.compute_theme_catalysts(
        {"usNVDA": {"price": 160.0, "pct": 6.5}, "usTSLA": {"price": 410.0, "pct": 4.8}},
        [{"key": "SOX", "pct": 3.2}],
    )
    by_th = {c["theme"]: c for c in cats}
    assert by_th["算力"]["direction"] == "bullish"
    assert by_th["算力"]["catalyst_score"] == 2
    assert by_th["机器人"]["direction"] == "bullish"


def test_news_feed_theme_mapping_danger_and_causality():
    from backend import news_feed
    it_pos = news_feed.classify_item({
        "id": "1", "source": "同花顺7×24", "published_at": "2026-11-25T08:45:00",
        "title": "工信部重磅发文支持人形机器人与算力数据中心量产突破",
        "digest": "加快具身智能与液冷算力建设", "important": True,
        "raw_stocks": ["0.002771"],
    })
    assert it_pos["sentiment"] == "positive"
    assert "机器人" in it_pos["themes"] and "算力" in it_pos["themes"]
    assert "002771" in it_pos["codes"]

    it_bad = news_feed.classify_item({
        "id": "2", "source": "东财7×24", "published_at": "2026-11-25T10:15:00",
        "title": "某公司(600715)收到证监会立案调查及股东拟减持公告",
        "digest": "立案调查", "important": True, "raw_stocks": ["1.600715"],
    })
    assert it_bad["sentiment"] == "danger"
    assert "立案" in it_bad["danger_hits"]

    # 严格因果：cutoff=09:25 时不可看到 10:15 的快讯
    agg_pre = news_feed.aggregate_news([it_pos, it_bad], cutoff_ts="2026-11-25T09:25:00")
    assert agg_pre["n_total"] == 1
    assert "600715" not in agg_pre["stock_alerts"]

    agg_all = news_feed.aggregate_news([it_pos, it_bad])
    assert agg_all["stock_alerts"]["600715"]["risk_level"] == "danger"

    # 否定词保护：“拟增持…承诺不减持”绝不可误判为 danger 雷区
    it_negated = news_feed.classify_item({
        "id": "3", "source": "东财7×24", "published_at": "2026-11-25T11:00:00",
        "title": "白银有色：股东拟增持不低于1000万元，并承诺法定期限内不减持",
        "digest": "增持且不减持", "important": False, "raw_stocks": ["1.601212"],
    })
    assert it_negated["sentiment"] == "positive"
    assert it_negated["danger_hits"] == []


def test_buy_focus_ranking_and_news_danger_reject():
    groups = {
        f"00000{i}": {
            "ts_code": f"00000{i}", "name": f"票{i}", "guitu_theme": "算力" if i <= 3 else "机器人",
            "first_seal": f"09:3{i}:00", "turn_at_seal": 6.0 + i, "broken_max": 0,
            "guitu_rabbits": ["兔A"], "rabbit_max_seal_yi": 1.5,
        }
        for i in range(1, 7)
    }
    # 000001 命中硬利空雷区，应直接被回避
    sel, skipped = buy_signals.rank_and_focus_candidates(
        groups, max_per_theme=2, max_total=3,
        catalyst_by_theme={"算力": 2.0},
        stock_alerts={"000001": {"risk_level": "danger", "danger_tags": ["立案", "减持"]}},
    )
    assert "000001" not in sel
    assert len(sel) == 3
    assert any(s["ts_code"] == "000001" and "消息雷区" in s["reason"] for s in skipped)


def test_sell_early_washout_guard_weak_open_and_partial_profit():
    # 1) 09:30-09:35 洗盘容忍：普通 -2.2% 抖动不应在 09:32 误杀
    pts_wash = [
        {"t": "09:30", "price": 10.00, "avg_price": 10.00},
        {"t": "09:32", "price": 9.78, "avg_price": 9.92},
    ]
    assert sell_rules.find_adaptive_signal(
        pts_wash, code="002931", prev_close=10.00, open_price=10.00, entry_kind="sealed"
    ) is None

    # 2) 路径 B 次日大幅低开 -4% 且 09:35 后反抽无力 → 触发 weak_open_exit
    pts_weak = [
        {"t": "09:30", "price": 9.60, "avg_price": 9.60},
        {"t": "09:36", "price": 9.52, "avg_price": 9.58},
    ]
    sig_w = sell_rules.find_adaptive_signal(
        pts_weak, code="002931", prev_close=10.00, open_price=9.60, entry_kind="sealed"
    )
    assert sig_w is not None and sig_w["kind"] == "weak_open_exit"

    # 3) 冲高 +7% 未封板回落破均价线 → 产出 50% 分批锁利建议
    pts_tp = [
        {"t": "09:31", "price": 10.20, "avg_price": 10.20},
        {"t": "09:50", "price": 10.75, "avg_price": 10.55},
        {"t": "10:05", "price": 10.48, "avg_price": 10.52},
    ]
    part = sell_rules.find_partial_profit_signal(pts_tp, code="002931", prev_close=10.00)
    assert part is not None and part["ratio"] == pytest.approx(0.50)


