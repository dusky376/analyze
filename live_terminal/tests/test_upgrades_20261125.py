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
    # 路径键已去板块化：绝不包含具体行业/板块名（不因轮动误伤）
    assert all("影视院线" not in k and "算力" not in k for k in p_no)
    nuc_key = next(k for k in p_no if "烂板(≥3炸)" in k)
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


# ---------------------------------------------------------------------------
# 7. 自适应普跌细分（不写死静态阈值）+ 踏空强势股反事实反思
# ---------------------------------------------------------------------------

def _make_hist_rows(n: int = 80, *, ld_base: int = 10, lu_base: int = 60, vol_base: float = 1.0):
    rows = []
    for i in range(n):
        d = f"2026-06-{i + 1:02d}" if i < 28 else f"2026-07-{i - 27:02d}" if i < 58 else f"2026-08-{i - 57:02d}"
        ds = 0.45 + (i % 10) * 0.045
        ld = max(1, ld_base + (i % 7) - 3)
        lu = max(10, lu_base + (i % 9) * 5 - 20)
        rows.append({
            "day": d,
            "down_share": round(min(0.92, ds), 4),
            "limit_down_n": ld,
            "limit_up_n": lu,
            "dt_ratio": round(ld / max(1, lu), 4),
            "broken_hi_n": i % 4,
            "vol_ratio_5d": round(vol_base + ((i % 5) - 2) * 0.08, 4),
        })
    return rows


def test_decline_regime_adapts_to_rolling_history_not_hardcoded():
    """同一组原始数字在不同滚动历史分布下，分位秩与阈值随市场自动漂移（绝非写死）。"""
    from backend import ice_gate
    rec = {
        "day": "2026-09-01",
        "down_share": 0.85,
        "limit_down_n": 25,
        "limit_up_n": 55,
        "dt_ratio": round(25 / 55, 4),
        "broken_hi_n": 3,
        "vol_ratio_5d": 0.90,
    }
    calm_hist = _make_hist_rows(80, ld_base=6, lu_base=80, vol_base=1.0)
    wild_hist = _make_hist_rows(80, ld_base=50, lu_base=40, vol_base=1.0)

    r_calm = ice_gate.classify_decline_regime(rec, day="2026-09-01", is_ice_flag=True, hist_rows=calm_hist)
    r_wild = ice_gate.classify_decline_regime(rec, day="2026-09-01", is_ice_flag=True, hist_rows=wild_hist)

    assert r_calm["active"] is True and r_wild["active"] is True
    # 同样 25 家跌停：平静市场里分位接近 1.0（恐慌），高波动市场里分位很低（抱团分化）
    assert r_calm["percentiles"]["limit_down_n"] > 0.90
    assert r_wild["percentiles"]["limit_down_n"] < 0.20
    assert r_calm["adaptive_thresholds"]["ld_P80"] < r_wild["adaptive_thresholds"]["ld_P80"]
    assert r_calm["regime"] == "panic_collapse"
    assert r_wild["regime"] == "narrow_siphon"


def test_decline_regime_external_shock_volume_drain_and_causal_trade_adaptation():
    from backend import ice_gate
    hist = _make_hist_rows(80, ld_base=12, lu_base=60, vol_base=1.05)

    # 1) 外盘冲击型普跌（外盘大跌 + 内资高标未崩）
    r_ext = ice_gate.classify_decline_regime(
        {"day": "2026-09-01", "down_share": 0.86, "limit_down_n": 10, "limit_up_n": 62,
         "dt_ratio": 0.16, "broken_hi_n": 1, "vol_ratio_5d": 1.02},
        day="2026-09-01", is_ice_flag=True, hist_rows=hist,
        emotion={"available": True, "blocked": False, "avg_ret": 1.2, "limit_down_share": 0.05},
        overseas={"available": True, "sentiment": {"score": -68.0, "risk_level": "risk_off"}},
    )
    assert r_ext["regime"] == "external_shock"

    # 2) 缩量失血阴跌型（量比跌至滚动低分位 + 涨停稀少 + 非跌停潮）
    r_vol = ice_gate.classify_decline_regime(
        {"day": "2026-09-01", "down_share": 0.84, "limit_down_n": 8, "limit_up_n": 18,
         "dt_ratio": 0.44, "broken_hi_n": 1, "vol_ratio_5d": 0.62},
        day="2026-09-01", is_ice_flag=True, hist_rows=hist,
    )
    assert r_vol["regime"] == "volume_drain"

    # 3) 严格因果实盘成交反馈：赢钱上调、亏钱下调，未来成交（exit_date >= day）绝不泄露
    rec_p = {"day": "2026-09-01", "down_share": 0.90, "limit_down_n": 65, "limit_up_n": 25,
             "dt_ratio": 2.6, "broken_hi_n": 6, "vol_ratio_5d": 1.0}
    trades_win = [
        {"entry_date": "2026-08-01", "exit_date": "2026-08-03", "net_return": 0.12, "decline_regime": "panic_collapse"},
        {"entry_date": "2026-08-10", "exit_date": "2026-08-12", "net_return": 0.10, "decline_regime": "panic_collapse"},
        # 未来成交：exit_date == day 不可计入
        {"entry_date": "2026-08-29", "exit_date": "2026-09-01", "net_return": -0.30, "decline_regime": "panic_collapse"},
    ]
    trades_loss = [
        {"entry_date": "2026-08-01", "exit_date": "2026-08-03", "net_return": -0.14, "decline_regime": "panic_collapse"},
        {"entry_date": "2026-08-10", "exit_date": "2026-08-12", "net_return": -0.12, "decline_regime": "panic_collapse"},
    ]
    r_w = ice_gate.classify_decline_regime(rec_p, day="2026-09-01", is_ice_flag=True, hist_rows=hist, causal_trades=trades_win)
    r_l = ice_gate.classify_decline_regime(rec_p, day="2026-09-01", is_ice_flag=True, hist_rows=hist, causal_trades=trades_loss)
    assert r_w["n_causal_trades"] == 2          # 严格排除了 2026-09-01 未来成交
    assert r_w["trade_adj"] > 0 and r_l["trade_adj"] < 0
    assert r_w["adapted_mult"] > r_l["adapted_mult"]


def test_missed_winners_reflection_and_causal_theme_heat(monkeypatch):
    """未命中但走出来的二板牛股能被反事实扫描、正负样本对撞检验，并产出严格因果题材热度。"""
    import polars as pl
    # 构造 8 个交易日：
    # - 000001：第 3 天换手二板，随后 3/4/5 连板大涨 +33%（漏网牛股）
    # - 000002：第 3 天一字二板，随后 4 连板（物理不可买，须标 oneword_unbuyable）
    # - 000003：第 3 天换手二板，随后次日大跌 -8%（负样本，用于正负对撞防过拟合）
    days = [f"2026-11-{d:02d}" for d in (10, 11, 12, 13, 14, 15, 16, 17)]
    rows = []
    # 000001: 换手二板大牛股
    p1 = [10.0, 11.0, 12.10, 13.31, 14.64, 16.10, 15.80, 15.50]
    for i, d in enumerate(days):
        o = p1[i] * (0.98 if i >= 1 else 1.0)
        if i == 2:
            o = p1[i - 1] * 1.04  # 高开 +4%
        rows.append({"trade_date": d, "ts_code": "000001", "open": o, "high": p1[i],
                     "low": min(o, p1[i]) * 0.99, "close": p1[i], "turnover": 8.5, "amount": 5e8})
    # 000002: 一字二板
    p2 = [10.0, 11.0, 12.10, 13.31, 14.64, 14.20, 13.90, 13.80]
    for i, d in enumerate(days):
        is_ow = i == 2
        o = p2[i] if is_ow else p2[i] * 0.98
        rows.append({"trade_date": d, "ts_code": "000002", "open": o, "high": p2[i],
                     "low": p2[i] if is_ow else o * 0.99, "close": p2[i], "turnover": 1.2 if is_ow else 9.0, "amount": 2e8})
    # 000003: 换手二板但次日大亏
    p3 = [10.0, 11.0, 12.10, 11.10, 10.50, 10.40, 10.30, 10.20]
    for i, d in enumerate(days):
        o = p3[i - 1] * 1.04 if i == 2 else p3[i] * 0.99
        rows.append({"trade_date": d, "ts_code": "000003", "open": o, "high": max(o, p3[i]),
                     "low": min(o, p3[i]) * 0.98, "close": p3[i], "turnover": 9.5, "amount": 3e8})
    panel = pl.DataFrame(rows)

    monkeypatch.setattr(
        evolution, "_load_theme_map_for_day",
        lambda d8: ({"000001": ["算力"], "000002": ["医药"], "000003": ["化工"]},
                    {"000001": ["算力"], "000002": ["医药"], "000003": ["化工"]}),
    )
    monkeypatch.setattr(evolution, "_load_name_map", lambda: {"000001": "换手龙", "000002": "一字兔", "000003": "炸板坑"})

    res = evolution.analyze_missed_winners([], panel=panel, lookback_days=10)
    assert res["available"] is True
    assert res["n_missed_winners"] == 2
    assert res["n_buyable_winners"] == 1
    by_code = {w["ts_code"]: w for w in res["missed_winners"]}
    assert by_code["000001"]["miss_reason"] == "no_oneword_rabbit"
    assert by_code["000002"]["miss_reason"] == "oneword_unbuyable"
    # 正负样本对撞：pure_turnover_dragon 同时触发 000001(赢) 和 000003(亏)，必须统计 n_triggered==2 & n_losers_dragged==1
    prop = {p["key"]: p for p in res["factor_proposals"]}["pure_turnover_dragon"]
    assert prop["n_triggered"] == 2
    assert prop["n_winners_caught"] == 1
    assert prop["n_losers_dragged"] == 1
    # 因果题材热度：只有可买入的 000001（算力）产生热度反哺，一字不可买的 000002（医药）不产生虚高热度
    assert "算力" in res["theme_leader_heat"]
    assert "医药" not in res["theme_leader_heat"]
    assert res["theme_leader_heat"]["算力"]["catalyst_bonus"] > 0
    # 严格因果：as_of_day="20261113"（尚未走出来）时不可提前看到 11-14 后的连板
    res_early = evolution.causal_theme_leader_heat("20261113", [], panel=panel)
    assert "算力" not in res_early


# ---------------------------------------------------------------------------
# 8. 题材四层自适应识别（语义词根 + 共现自学习 + 历史记忆 + 新概念前缀）& 高标梯队不对称传导
# ---------------------------------------------------------------------------

def test_theme_semantic_root_and_emerging_prefix_clustering():
    from backend import theme_match as TM
    # 1) 语义词根自动覆盖文字微调变体（无需写死每条 exact 标签）
    assert "robot" in TM.map_to_axes(["按摩机器人"])
    assert "compute" in TM.map_to_axes(["数据中心EPC", "东数西算"])
    assert "medical" in TM.map_to_axes(["阿尔茨海默病", "流感", "中成药", "AI医疗"])
    assert "consumer" in TM.map_to_axes(["免税重组", "饲料主业", "养殖业"])
    assert "finance" in TM.map_to_axes(["跨境支付", "稳定币"])
    assert "auto_drive" in TM.map_to_axes(["车路云", "汽车内饰"])
    # 占位词/纯业绩/非题材绝不误配
    assert TM.map_to_axes(["其它", "三季报增长", "地方国资", "蹦床"]) == []

    # 2) Day-1 新概念前缀聚类：未进轴但共享专有二字中文前缀的票自动成组；泛化词「智能XX」不硬凑
    out = TM.group_by_theme([
        {"code": "000001", "name": "甲", "tags": ["脑电波传感"], "industry": "", "streak": 2},
        {"code": "000002", "name": "乙", "tags": ["脑电波芯片" if False else "脑电波头环"], "industry": "", "streak": 2},
        {"code": "000003", "name": "丙", "tags": ["智能门锁"], "industry": "", "streak": 2},
        {"code": "000004", "name": "丁", "tags": ["智能马桶"], "industry": "", "streak": 2},
    ])
    by_th = {g["theme"]: set(g["codes"]) for g in out["groups"]}
    assert by_th.get("脑电概念") == {"000001", "000002"}
    assert "智能概念" not in by_th


def test_theme_historical_memory_rescues_placeholder_and_missing(monkeypatch):
    from backend import theme_match as TM
    monkeypatch.setattr(
        TM, "historical_stock_tags",
        lambda before_day=None, **kw: {"002579": ["AI算力PCB", "存储芯片"], "000001": ["人形机器人"]},
    )
    enriched = TM.enrich_stock_tags(
        {"002579": ["其它"], "000002": ["算力"]},
        before_day="2026-11-25",
        codes=["002579", "000001", "000002"],
    )
    assert "AI算力PCB" in enriched["002579"]
    assert "人形机器人" in enriched["000001"]
    assert enriched["000002"] == ["算力"]


def test_high_board_pulse_asymmetric_drag_vs_uncertain_lead(monkeypatch):
    import polars as pl
    from backend import live_sim

    empty_p = pl.DataFrame(schema={"trade_date": pl.Utf8, "ts_code": pl.Utf8, "close": pl.Float64})
    monkeypatch.setattr(live_sim, "_theme_tags", lambda day, codes=None: {"000001": ["算力"], "000002": ["算力"]})

    # 1) 两只 ≥3 板高标跌停 + 近 60 日高标跌停日二板胜率低 → 高标核按钮拖累，动态降仓 ×0.70
    monkeypatch.setattr(
        live_sim, "_rolling_high_board_spillover",
        lambda p, d, lookback_days=60: {"available": True, "drag_b2_win_rate": 0.35, "lead_b2_win_rate": 0.42, "lead_n": 10},
    )
    pulse_drag = live_sim._build_high_board_pulse([
        {"ts_code": "000001", "prev_streak": 5, "pct": -10.0, "is_zt": False, "is_dt": True},
        {"ts_code": "000002", "prev_streak": 3, "pct": -10.0, "is_zt": False, "is_dt": True},
    ], "20261125", empty_p)
    assert pulse_drag["state"] == "drag"
    assert pulse_drag["size_mult"] == pytest.approx(0.70)

    # 2) 单高标跌停（非最高空间板跌停）→ 温和预警 ×0.85（不绝对一刀切空仓）
    pulse_warn = live_sim._build_high_board_pulse([
        {"ts_code": "000001", "prev_streak": 4, "pct": 3.5, "is_zt": False, "is_dt": False},
        {"ts_code": "000002", "prev_streak": 3, "pct": -10.0, "is_zt": False, "is_dt": True},
    ], "20261125", empty_p)
    assert pulse_warn["state"] == "warn"
    assert pulse_warn["size_mult"] == pytest.approx(0.85)

    # 3) 高标强势晋级但近 60 日传导胜率仅 42%（<50%）→ 不盲目加分（lead_themes 为空）
    pulse_lead_weak = live_sim._build_high_board_pulse([
        {"ts_code": "000001", "prev_streak": 4, "pct": 10.0, "is_zt": True, "is_dt": False},
    ], "20261125", empty_p)
    assert pulse_lead_weak["state"] == "lead"
    assert pulse_lead_weak["lead_themes"] == []

    # 4) 近 60 日传导胜率 62%（>=50%）→ 同题材给引领加分
    monkeypatch.setattr(
        live_sim, "_rolling_high_board_spillover",
        lambda p, d, lookback_days=60: {"available": True, "drag_b2_win_rate": 0.40, "lead_b2_win_rate": 0.62, "lead_n": 10},
    )
    pulse_lead_ok = live_sim._build_high_board_pulse([
        {"ts_code": "000001", "prev_streak": 4, "pct": 10.0, "is_zt": True, "is_dt": False},
    ], "20261125", empty_p)
    assert "算力" in pulse_lead_ok["lead_themes"]


# ---------------------------------------------------------------------------
# 9. 排板封板期 1 分钟累计成交额吃穿判定 + B2 纯换手影子挑战者（含关联产业链有兔）
# ---------------------------------------------------------------------------

def test_fill_verify_sealed_period_minute_volume_catches_replenish(monkeypatch):
    """全天未开板但封板期涨停价成交额已吃穿挂单前封单（即使主力补单使收盘封单未降），必须判成交。"""
    from backend import fill_verify, minute_flow

    frames = [
        {"ts": "2026-11-25T09:35:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "002771", "streak": 2, "broken": 0, "turnover": 6.0, "seal_fund": 1.0e8, "price": 20.0},
        ]}},
        {"ts": "2026-11-25T15:00:00", "zt": {"all_complete": True, "all": [
            # 收盘封单因新补单仍剩 0.9 亿（净差值仅 0.1 亿），但封板期间真实成交 1.5 亿已吃穿 1.0 亿队列
            {"ts_code": "002771", "streak": 2, "broken": 0, "turnover": 9.0, "seal_fund": 0.9e8, "price": 20.0},
        ]}},
    ]
    monkeypatch.setattr(fill_verify, "daily_amount", lambda day, code: 5.0e8)
    monkeypatch.setattr(minute_flow, "amount_between", lambda c, d, t0, t1: {"ok": True, "amount": 1.5e8})

    res = fill_verify.simulate_fill("20261125", "002771", "2026-11-25T09:35:00", frames, entry_price=20.0)
    assert res["status"] == "filled"
    assert res["confidence"] == "edge"
    assert res["sealed_traded_yi"] == pytest.approx(1.5)
    assert "封板期" in res["detail"]


def test_b2_turnover_challenger_chain_rabbit_and_graduation_gate(monkeypatch):
    """B2 纯换手影子挑战者能识别「关联产业链有兔」，独立跑信息素并严格执行毕业门禁。"""
    import polars as pl

    days = [f"2026-11-{d:02d}" for d in (10, 11, 12, 13, 14, 15, 16)]
    rows = []
    # 000010：第 3 天一字二板（算力）
    p_rab = [10.0, 11.0, 12.10, 13.31, 13.0, 12.8, 12.6]
    for i, d in enumerate(days):
        ow = i == 2
        o = p_rab[i] if ow else p_rab[i] * 0.98
        rows.append({"trade_date": d, "ts_code": "000010", "open": o, "high": p_rab[i],
                     "low": p_rab[i] if ow else o * 0.99, "close": p_rab[i], "turnover": 1.0 if ow else 8.0})
    # 000020：第 3 天纯换手二板（AI传媒，与算力属关联产业链），随后 3 板大赚
    p_tur = [10.0, 11.0, 12.10, 13.31, 14.00, 13.80, 13.50]
    for i, d in enumerate(days):
        o = p_tur[i - 1] * 1.04 if i == 2 else p_tur[i] * 0.98
        rows.append({"trade_date": d, "ts_code": "000020", "open": o, "high": p_tur[i],
                     "low": min(o, p_tur[i]) * 0.99, "close": p_tur[i], "turnover": 8.0})
    panel = pl.DataFrame(rows)

    monkeypatch.setattr(
        evolution, "_load_theme_map_for_day",
        lambda d8: ({"000010": ["算力"], "000020": ["AI传媒"]},
                    {"000010": ["算力"], "000020": ["AI传媒"]}),
    )
    monkeypatch.setattr(evolution, "_load_name_map", lambda: {"000010": "算力兔", "000020": "传媒龙"})

    b2 = evolution.evaluate_b2_turnover_challenger([], panel=panel, lookback_days=10)
    assert b2["available"] is True
    assert b2["summary"]["n_trades"] == 1
    tr = b2["recent_trades"][0]
    assert tr["ts_code"] == "000020"
    assert tr["alt_anchor"] == "关联产业链有兔"
    assert tr["net_ret"] > 0.10
    # 仅 1 笔样本远未达 min_samples=10，绝不可提前毕业解锁实盘仓位
    assert b2["graduated_paths"] == []
    assert b2["paths"][0]["status"] == "promising"


def test_evolution_sector_agnostic_and_unthemed_market_scouts(monkeypatch):
    """1) 路径特征去板块化：不同板块同形态归入同一路径；2) 无题材分类错过的市场二板因子汇入蚂蚁进化。"""
    import polars as pl

    # 1) 不同板块（电力 vs 机器人）但同为「单兔锚定 | 早盘 | 中换手 | 烂板」应汇聚到同一路径
    k1 = evolution.make_path_key({
        "guitu_theme": "电力", "guitu_rabbits": ["兔A"],
        "first_seal": "09:35:00", "turn_at_seal": 12.0, "broken_max": 4,
    })
    k2 = evolution.make_path_key({
        "guitu_theme": "机器人", "guitu_rabbits": ["兔B"],
        "first_seal": "09:38:00", "turn_at_seal": 15.0, "broken_max": 5,
    })
    assert k1 == k2
    assert "电力" not in k1 and "机器人" not in k1
    assert k1.startswith("单兔锚定 |")

    # 2) 无题材分类（not my_axes）错过的市场二板（一赢一亏）应全量进入侦察蚁样本并汇入主进化
    days = [f"2026-11-{d:02d}" for d in (10, 11, 12, 13, 14, 15, 16)]
    rows = []
    # 000030：无题材换手二板，弱转强高开，次日连板大赚
    p_win = [10.0, 11.0, 12.10, 13.31, 14.00, 13.80, 13.50]
    for i, d in enumerate(days):
        o = p_win[i - 1] * 1.04 if i == 2 else p_win[i] * 0.98
        rows.append({"trade_date": d, "ts_code": "000030", "open": o, "high": p_win[i],
                     "low": min(o, p_win[i]) * 0.99, "close": p_win[i], "turnover": 8.0})
    # 000031：无题材换手二板，平开高换手，次日断板亏损
    p_loss = [10.0, 11.0, 12.10, 11.20, 10.90, 10.80, 10.70]
    for i, d in enumerate(days):
        o = p_loss[i - 1] * 1.005 if i == 2 else p_loss[i] * 0.99
        rows.append({"trade_date": d, "ts_code": "000031", "open": o, "high": max(o, p_loss[i]),
                     "low": min(o, p_loss[i]) * 0.98, "close": p_loss[i], "turnover": 22.0})
    panel = pl.DataFrame(rows)

    # 题材映射为空（模拟无题材分类盲区）
    monkeypatch.setattr(evolution, "_load_theme_map_for_day", lambda d8: ({}, {}))
    monkeypatch.setattr(evolution, "_load_name_map", lambda: {"000030": "无题材强龙", "000031": "无题材弱板"})

    scouts = evolution.collect_unthemed_market_scouts([], panel=panel, lookback_days=10)
    assert len(scouts) == 2
    assert all(s["anchor_bucket"] == "无题材·市场自选" for s in scouts)

    evo = evolution.evolve_from_records([], [], market_scouts=scouts)
    assert evo["available"] is True
    assert evo["n_paths"] == 2
    by_p = {p["path"]: p for p in evo["paths"]}
    win_p = next(v for k, v in by_p.items() if "弱转强高开" in k)
    loss_p = next(v for k, v in by_p.items() if "高换手" in k)
    assert win_p["pheromone"] > 1.0 and win_p["source"] == "market_scout" and win_p["scout_n"] == 1
    assert loss_p["pheromone"] < 1.0 and loss_p["source"] == "market_scout" and loss_p["scout_n"] == 1


def test_fill_verify_cancel_success_vs_too_late(monkeypatch):
    """排板买单主动撤单：主力撤单/兔子炸板/14:30 仍在涨停且未吃穿→撤单成功；已吃穿或已开板→保守判来不及。"""
    from backend import fill_verify, minute_flow

    monkeypatch.setattr(fill_verify, "daily_amount", lambda day, code: 3.0e8)

    # 1) 主力主动撤单：封单 1.0 亿→0.45 亿，期间仅成交 0.05 亿（推断撤单 0.50 亿 ≥ 30%）且未吃穿 → 撤单成功
    frames_wd = [
        {"ts": "2026-11-26T09:35:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "002771", "streak": 2, "broken": 0, "turnover": 6.0, "seal_fund": 1.0e8, "price": 20.0},
        ]}},
        {"ts": "2026-11-26T09:40:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "002771", "streak": 2, "broken": 0, "turnover": 6.2, "seal_fund": 0.45e8, "price": 20.0},
        ]}},
    ]
    monkeypatch.setattr(minute_flow, "amount_between", lambda c, d, t0, t1: {"ok": True, "amount": 0.05e8})
    r1 = fill_verify.simulate_fill("20261126", "002771", "2026-11-26T09:35:00", frames_wd, entry_price=20.0, enable_cancel=True)
    assert r1["status"] == "cancelled"
    assert r1["cancel_kind"] == "main_withdraw"
    assert r1["inferred_withdraw_yi"] == pytest.approx(0.50, rel=1e-2)

    # 2) 兔子炸板不写死一票撤单：
    # 2a) 卡位保护：兔子 603324 炸板，但乌龟 002771 自身封单稳健（1.0→0.95亿，仅降5%）→ 不撤单，继续排队
    frames_takeover = [
        {"ts": "2026-11-26T09:35:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "002771", "streak": 2, "broken": 0, "turnover": 6.0, "seal_fund": 1.0e8, "price": 20.0},
            {"ts_code": "603324", "streak": 2, "broken": 0, "turnover": 1.0, "seal_fund": 2.0e8, "price": 30.0},
        ]}},
        {"ts": "2026-11-26T09:45:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "002771", "streak": 2, "broken": 0, "turnover": 6.5, "seal_fund": 0.95e8, "price": 20.0},
        ]}},
    ]
    r2_keep = fill_verify.simulate_fill(
        "20261126", "002771", "2026-11-26T09:35:00", frames_takeover,
        entry_price=20.0, enable_cancel=True, rabbit_codes=["603324"],
    )
    assert r2_keep["status"] == "queued_unfilled"

    # 2b) 联动拖累：兔子炸板 且 乌龟自身也同步失血（1.0→0.60亿，-40% ≥ 动态联动阈值35%）→ 确认受拖累才撤单
    frames_drag = [
        {"ts": "2026-11-26T09:35:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "002771", "streak": 2, "broken": 0, "turnover": 6.0, "seal_fund": 1.0e8, "price": 20.0},
            {"ts_code": "603324", "streak": 2, "broken": 0, "turnover": 1.0, "seal_fund": 2.0e8, "price": 30.0},
        ]}},
        {"ts": "2026-11-26T09:45:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "002771", "streak": 2, "broken": 0, "turnover": 6.5, "seal_fund": 0.60e8, "price": 20.0},
        ]}},
    ]
    monkeypatch.setattr(minute_flow, "amount_between", lambda c, d, t0, t1: {"ok": True, "amount": 0.35e8})
    r2_drag = fill_verify.simulate_fill(
        "20261126", "002771", "2026-11-26T09:35:00", frames_drag,
        entry_price=20.0, enable_cancel=True, rabbit_codes=["603324"],
    )
    assert r2_drag["status"] == "cancelled"
    assert r2_drag["cancel_kind"] == "rabbit_drag_bleed"

    # 3) 封单与兔子都健康时，即使排到 14:50 也绝不因时间久而误撤单（强板往往下午才能排进）
    frames_hard = [
        {"ts": "2026-11-26T09:35:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "002771", "streak": 2, "broken": 0, "turnover": 6.0, "seal_fund": 1.0e8, "price": 20.0},
            {"ts_code": "603324", "streak": 2, "broken": 0, "turnover": 1.0, "seal_fund": 2.0e8, "price": 30.0},
        ]}},
        {"ts": "2026-11-26T14:50:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "002771", "streak": 2, "broken": 0, "turnover": 7.0, "seal_fund": 0.90e8, "price": 20.0},
            {"ts_code": "603324", "streak": 2, "broken": 0, "turnover": 1.2, "seal_fund": 1.90e8, "price": 30.0},
        ]}},
    ]
    r3 = fill_verify.simulate_fill(
        "20261126", "002771", "2026-11-26T09:35:00", frames_hard,
        entry_price=20.0, enable_cancel=True, rabbit_codes=["603324"],
    )
    assert r3["status"] == "queued_unfilled"

    # 4) 先判成交、再判撤单：累计成交 1.3 亿已吃穿 1.0 亿队列 → 即使封单骤降也必须判 filled（绝不事后作弊）
    monkeypatch.setattr(minute_flow, "amount_between", lambda c, d, t0, t1: {"ok": True, "amount": 1.3e8})
    r4 = fill_verify.simulate_fill("20261126", "002771", "2026-11-26T09:35:00", frames_wd, entry_price=20.0, enable_cancel=True)
    assert r4["status"] == "filled"

    # 5) 盘中高频盯盘：卖一有单(已开板) → too_late=True；仍在涨停且封单骤降 → should_cancel=True
    o_mock = {"ts_code": "002771", "entry_price": 20.0, "seal_fund_at_entry": 1.0e8}
    q_open = {"price": 19.90, "bid1": 19.90, "bid1_vol": 100, "ask1_vol": 500}
    assert fill_verify.check_live_quote_cancel(o_mock, q_open)["too_late"] is True
    q_bleed = {"price": 20.0, "bid1": 20.0, "bid1_vol": 15000, "ask1_vol": 0, "amount": 2.0e8}  # 3000万 < 50% of 1亿
    res_bleed = fill_verify.check_live_quote_cancel(o_mock, q_bleed, ts="10:15")
    assert res_bleed["too_late"] is False and res_bleed["should_cancel"] is True

    # 6) 撤单阈值全自适应：放量强势+蚂蚁正反馈 vs 缩量冰点拖累，所有阈值自动漂移而非写死
    th_bull = fill_verify.adaptive_cancel_thresholds(
        code="002771", market_vol_ratio=1.5, ice_level="L0",
        market_drag_sensitive=False, hist_turnovers=[12, 16, 20, 24, 28, 30],
        hist_seals_yuan=[0.4e8, 0.5e8, 0.8e8, 1.0e8, 1.5e8],
        hist_broken_counts=[0, 1, 2, 3, 4], evo_high_turn_delta=0.20, evo_rotten_delta=0.15,
    )
    th_bear = fill_verify.adaptive_cancel_thresholds(
        code="002771", market_vol_ratio=0.7, ice_level="L5",
        market_drag_sensitive=True, hist_turnovers=[5, 7, 9, 12, 15, 16],
        hist_seals_yuan=[0.10e8, 0.12e8, 0.18e8, 0.25e8, 0.35e8],
        hist_broken_counts=[0, 0, 1, 1, 2], evo_high_turn_delta=-0.20, evo_rotten_delta=-0.20,
    )
    assert th_bull["bleed_ratio"] > th_bear["bleed_ratio"]
    assert th_bull["withdraw_ratio"] > th_bear["withdraw_ratio"]
    assert th_bull["max_turnover"] > th_bear["max_turnover"]
    assert th_bull["max_broken"] > th_bear["max_broken"]
    assert th_bull["min_seal_yuan"] > th_bear["min_seal_yuan"]


def test_evolution_10_factor_decomposition_and_shrinkage():
    """10 因子独立信息素分解：覆盖竞价/上板/换手/封板质量/20日基因/60日突破/锚定/板块梯队/高标/溢价与小样本收缩。"""
    orders = [
        {
            "day": "20261116", "ts_code": "002771", "name": "强龙", "status": "filled",
            "guitu_rabbits": ["兔A", "兔B"], "first_seal": "09:32:00", "turn_at_seal": 10.0,
            "broken_max": 0, "open_gap": 4.5, "gene_20d": 2, "breakout_60d": True,
            "board_zt_count": 7, "high_board_state": "高标晋级引领", "zt_premium": 3.2,
        },
        {
            "day": "20261118", "ts_code": "600715", "name": "烂板", "status": "filled",
            "guitu_rabbits": ["兔C"], "first_seal": "13:20:00", "turn_at_seal": 26.0,
            "broken_max": 6, "open_gap": -2.5, "gene_20d": 0, "breakout_60d": False,
            "board_zt_count": 1, "high_board_state": "高标核按钮", "zt_premium": -2.8,
        },
    ]
    trades = [
        {"entry_date": "20261116", "exit_date": "20261118", "ts_code": "002771", "net_return": 0.22, "exit_blocked": False},
        {"entry_date": "20261118", "exit_date": "20261120", "ts_code": "600715", "net_return": -0.24, "exit_blocked": True},
    ]
    res = evolution.evolve_from_records(orders, trades)
    fp = res["factor_pheromones"]
    assert fp["n_dims"] == 10
    assert fp["prior_m"] == pytest.approx(15.0)

    # 小样本收缩校验：1 笔样本的 shrink_ratio 必须等于 1/(1+15) = 0.0625
    row_auc_hi = next(r for r in fp["rows"] if r["dim"] == "auction" and "弱转强" in r["bucket"])
    assert row_auc_hi["samples"] == 1
    assert row_auc_hi["shrink_ratio"] == pytest.approx(1.0 / 16.0, rel=2e-2)
    assert abs(row_auc_hi["shrunk_delta"]) < abs(row_auc_hi["pheromone"] - 1.0)

    # 综合合成仓位系数：强特征 > 1.0，弱特征 < 1.0，且严格钳位在 [0.30, 1.25]
    sc_good = evolution.score_candidate_factors(fp["lookup"], evolution.extract_factors(orders[0]))
    sc_bad = evolution.score_candidate_factors(fp["lookup"], evolution.extract_factors(orders[1]))
    assert sc_good["n_matched"] == 10
    assert 1.0 < sc_good["size_mult"] <= 1.25
    assert 0.30 <= sc_bad["size_mult"] < 1.0


def test_survival_guards_circuit_breaker_climax_ebb_sell_and_deviation():
    """老股民存活套件：账户连亏熔断 + 高潮防摊大饼/退潮初期拦截 + 强票早盘防洗飞/核按钮竞价抢跑 + 监管红线/回封雷达。"""
    from backend import live_sim

    # 1) 账户级连亏熔断（严格因果）
    t_def = [
        {"exit_date": "20261118", "net_return": -0.08, "notional": 100_000.0},
        {"exit_date": "20261119", "net_return": -0.07, "notional": 100_000.0},
        # 未来成交（exit_date == day）绝不计入
        {"exit_date": "20261120", "net_return": -0.30, "notional": 300_000.0},
    ]
    cb_def = live_sim.account_circuit_breaker(t_def, "20261120")
    assert cb_def["mode"] == "defense" and cb_def["size_mult"] == pytest.approx(0.50)

    t_halt = t_def[:2] + [{"exit_date": "20261120", "net_return": -0.10, "notional": 100_000.0}]
    cb_halt = live_sim.account_circuit_breaker(t_halt, "20261121")
    assert cb_halt["mode"] == "halt" and cb_halt["block_new_orders"] is True

    # 2) 情绪高潮自适应收缩 + 退潮初期拦截
    guard_climax = live_sim.market_climax_and_ebb_guard(
        "20261124",
        {"available": True, "premium": 2.5, "high_board_pulse": {"state": "lead"}},
        {"available": True, "level": "L0", "metrics": {"limit_up_n": 105}},
        hist_zt_counts=[40, 45, 50, 55, 60, 62, 65, 70, 75, 80, 90],
    )
    assert guard_climax["climax_active"] is True
    assert guard_climax["max_total"] == 2 and guard_climax["max_per_theme"] == 1

    guard_ebb = live_sim.market_climax_and_ebb_guard(
        "20261125",
        {"available": True, "premium": -1.2, "high_board_pulse": {"state": "drag"}},
        {"available": True, "level": "L3", "metrics": {"limit_up_n": 35}},
    )
    assert guard_ebb["ebb_block"] is True

    # 3) 卖点：强票高开且在线上 09:38 抖动 -2.3% 不洗飞；极端核按钮低开 -6% 09:30 直接抢跑
    pts_strong = [
        {"t": "09:31", "price": 10.50, "avg_price": 10.40},
        {"t": "09:36", "price": 10.75, "avg_price": 10.48},
        {"t": "09:38", "price": 10.50, "avg_price": 10.49},  # 距 10.75 跌 -2.3%，但在均价线上且高开 +5%
    ]
    assert sell_rules.find_adaptive_signal(
        pts_strong, code="002931", prev_close=10.0, open_price=10.50, entry_kind="sealed"
    ) is None

    pts_nuc = [{"t": "09:30", "price": 9.40, "avg_price": 9.40}]
    sig_nuc = sell_rules.find_adaptive_signal(
        pts_nuc, code="002931", prev_close=10.0, open_price=9.40, entry_kind="sealed"
    )
    assert sig_nuc is not None and sig_nuc["kind"] == "weak_open_exit" and sig_nuc["t"] == "09:30"

    # 4) 监管严重异动红线 + 回封/弱转强买点雷达
    dev = buy_signals.check_abnormal_deviation([10.0] + [10.0 * (1.07 ** i) for i in range(1, 11)])
    assert dev["near_severe"] is True
    bq = buy_signals.evaluate_turtle_quality(
        {"ts_code": "002771", "broken_max": 0, "turn_at_seal": 8.0, "first_seal": "09:32:00", "abnormal_deviation": dev},
        rabbit_count=2,
    )
    assert bq["reject"] is True

    radar = buy_signals.scan_reseal_and_weak_to_strong([
        {"ts_code": "002771", "name": "回封龙", "broken": 1, "sealed_now": True, "seal_fund_yi": 0.85,
         "prev_sealed": True, "open_gap": 4.2, "auction_vol_ratio": 0.08},
    ])
    assert len(radar["reseal_confirm"]) == 1 and len(radar["weak_to_strong_auction"]) == 1


def test_circuit_breaker_auto_thaw_and_lhb_active_seats():
    """1) 熔断防永久死锁：转盈后自动解冻为防守试错；2) 龙虎榜活人情绪 + 个股席位画像 + 第11维蚂蚁因子。"""
    from backend import lhb, live_sim

    # 1) 前期连亏造成 >10% 回撤，但最近一天转盈 → 自动解冻为 defense（不再永久卡死在 halt）
    trades_thaw = [
        {"exit_date": "20261120", "net_return": -0.25, "notional": 400_000.0},
        {"exit_date": "20261125", "net_return": -0.08, "notional": 300_000.0},
        {"exit_date": "20261126", "net_return": +0.16, "notional": 50_000.0},
    ]
    cb = live_sim.account_circuit_breaker(trades_thaw, "20261127")
    assert cb["mode"] == "defense"
    assert cb["block_new_orders"] is False
    assert cb["size_mult"] == pytest.approx(0.50)

    # 2) 龙虎榜席位分类 & 个股画像（拉萨接盘 vs 机构合力）
    assert lhb.classify_seat("东方财富证券股份有限公司拉萨团结路第一证券营业部") == "lasa"
    assert lhb.classify_seat("机构专用") == "inst"
    p_lasa = lhb.classify_stock_lhb(
        {"ts_code": "000001", "name": "散户接盘股", "day": "2026-11-26", "net_amt": 5e6, "deal_net_ratio": 1.0},
        [
            {"seat": "东方财富证券股份有限公司拉萨东环路第一证券营业部", "buy": 2e7},
            {"seat": "东方财富证券股份有限公司拉萨团结路第二证券营业部", "buy": 1.5e7},
            {"seat": "国泰君安南京太平南路", "buy": 1e7},
        ],
    )
    assert p_lasa["tag"] == "拉萨散户接盘" and p_lasa["score_delta"] < 0

    p_inst = lhb.classify_stock_lhb(
        {"ts_code": "000002", "name": "机构龙", "day": "2026-11-26", "net_amt": 8e7, "deal_net_ratio": 12.0},
        [
            {"seat": "机构专用", "buy": 6e7},
            {"seat": "深股通专用", "buy": 3e7},
            {"seat": "中信建投北京朝阳门", "buy": 2e7},
        ],
    )
    assert p_inst["tag"] == "机构合力" and p_inst["score_delta"] > 0

    # 3) 全市场「龙虎榜活人数量」情绪：活人充足 vs 拉萨主导
    hot_seats = [{"seat": f"游资营业部{i}", "buy": 1.5e7} for i in range(140)]
    sent_hot = lhb.compute_market_lhb_sentiment([{"net_amt": 2e8}], hot_seats)
    assert sent_hot["state"] == "hot" and sent_hot["active_seat_count"] == 140

    cold_seats = [{"seat": f"东方财富证券股份有限公司拉萨第{i}营业部", "buy": 2e7} for i in range(15)] + [
        {"seat": f"游资营业部{i}", "buy": 5e6} for i in range(10)
    ]
    sent_cold = lhb.compute_market_lhb_sentiment([{"net_amt": -1e8}], cold_seats)
    assert sent_cold["state"] == "cold" and sent_cold["size_mult"] < 1.0

    # 4) 买点评分仅把龙虎榜作客观参考备注（不一概写死肯定/否定），并验证活人主攻板块/游资溢价与第11维因子
    bq_inst = buy_signals.evaluate_turtle_quality(
        {"ts_code": "000002", "broken_max": 0, "turn_at_seal": 8.0, "first_seal": "09:35:00", "lhb_profile": p_inst},
        rabbit_count=1,
    )
    bq_lasa = buy_signals.evaluate_turtle_quality(
        {"ts_code": "000001", "broken_max": 0, "turn_at_seal": 8.0, "first_seal": "09:35:00", "lhb_profile": p_lasa},
        rabbit_count=1,
    )
    assert bq_inst["score"] == bq_lasa["score"]
    assert any("龙虎榜参考(机构合力" in r for r in bq_inst["reasons"])
    assert any("龙虎榜参考(拉萨散户接盘" in r for r in bq_lasa["reasons"])

    prem = lhb.compute_youzi_premium({"000001": p_lasa, "000002": p_inst}, {"000001": -4.0, "000002": 6.5})
    assert prem["available"] is True and prem["avg_pct"] == pytest.approx(6.5) and prem["lasa_avg_pct"] == pytest.approx(-4.0)

    facs = evolution.extract_factors({"ts_code": "000002", "lhb_profile": p_inst})
    assert facs.get("lhb") == "机构合力"

    # 5) 撤单反事实对照：主力假撤单频繁把好票洗飞 → 自动产出正反馈放宽 withdraw_ratio
    fake_cancels = [
        {"day": "20261120", "ts_code": "002298", "name": "洗盘龙1", "status": "cancelled",
         "cancel_kind": "main_withdraw", "missed_next_day": "20261121", "missed_next_ret": 0.10},
        {"day": "20261124", "ts_code": "002299", "name": "洗盘龙2", "status": "cancelled",
         "cancel_kind": "main_withdraw", "missed_next_day": "20261125", "missed_next_ret": 0.08},
    ]
    cf = evolution.evaluate_cancel_counterfactual(fake_cancels, as_of_day="20261127")
    assert cf["n_verified"] == 2
    assert cf["threshold_adj_by_kind"]["main_withdraw"] > 0
    from backend import fill_verify
    th_base = fill_verify.adaptive_cancel_thresholds(code="002298")
    th_relaxed = fill_verify.adaptive_cancel_thresholds(code="002298", cancel_feedback=cf["threshold_adj_by_kind"])
    assert th_relaxed["withdraw_ratio"] > th_base["withdraw_ratio"]


def test_plan_orders_compound_elastic_sizing():
    """复利弹性仓位：A/B/C质量分级 + 20cm等风险×0.65 + 真主线顺风松绑（硬顶15%/18%）。"""
    from backend import live_sim
    cands = [
        {"ts_code": "002298", "first": {"price": 10.0}, "buy_quality_pre": {"grade": "A"}},
        {"ts_code": "300324", "first": {"price": 10.0}, "buy_quality_pre": {"grade": "A"}},  # 20cm
        {"ts_code": "000889", "first": {"price": 10.0}, "buy_quality_pre": {"grade": "B"}},
        {"ts_code": "600715", "first": {"price": 10.0}, "buy_quality_pre": {"grade": "C"}},
    ]
    plan = live_sim.plan_orders(
        cands, size_factor=1.0, capital=1_000_000.0,
        mainline_boost_codes={"002298"},
    )
    by_c = {x["code"]: x for x in plan}
    # 002298 A级 + 真主线顺风：14% * 1.2 = 16.8%（≤18%软顶）
    assert by_c["002298"]["size_yuan"] == pytest.approx(168_000.0)
    assert by_c["002298"]["mainline_boost"] is True
    # 300324 20cm A级：14% * 0.65 = 9.1%
    assert by_c["300324"]["size_yuan"] == pytest.approx(91_000.0)
    # 000889 主板 B级：10%；600715 主板 C级：6%
    assert by_c["000889"]["size_yuan"] == pytest.approx(100_000.0)
    assert by_c["600715"]["size_yuan"] == pytest.approx(60_000.0)










