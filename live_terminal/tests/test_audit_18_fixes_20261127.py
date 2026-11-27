# -*- coding: utf-8 -*-
"""2026-11-27 深度审计 18 项缺陷全量修复回归测试套件。"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 1. 日线面板缺日自愈与 /api/health 连续性探针
# ---------------------------------------------------------------------------

def test_01_panel_backfill_and_health_continuity(tmp_path, monkeypatch) -> None:
    from backend import feeds, paths

    raw_p = tmp_path / "panel_raw.parquet"
    live_p = tmp_path / "panel_live.parquet"
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True)

    d_raw = pl.DataFrame({
        "trade_date": [date(2026, 11, 25)],
        "ts_code": ["000001"],
        "open": [10.0], "high": [10.5], "low": [9.9], "close": [10.2],
        "volume": [1000.0], "amount": [1e7], "turnover": [2.0], "pct_chg": [2.0],
    })
    d_raw.write_parquet(raw_p)

    # panel_live 故意缺 11-26，只有 11-27
    d_live = pl.DataFrame({
        "trade_date": [date(2026, 11, 27)],
        "ts_code": ["000001"],
        "open": [10.5], "high": [10.8], "low": [10.4], "close": [10.7],
        "volume": [1200.0], "amount": [1.2e7], "turnover": [2.5], "pct_chg": [3.0],
    })
    d_live.write_parquet(live_p)

    # 磁盘存在 20261126/quotes/frames.parquet
    q26_dir = live_dir / "20261126" / "quotes"
    q26_dir.mkdir(parents=True)
    pl.DataFrame({
        "ts_code": ["000001"],
        "open": [10.2], "high": [10.6], "low": [10.1], "price": [10.4],
        "volume": [1100.0], "amount": [1.1e7], "turnover": [2.1], "pct": [1.96],
        "quote_time": ["20261126150000"],
    }).write_parquet(q26_dir / "frames.parquet")

    monkeypatch.setattr(feeds, "_panel_path", lambda: raw_p)
    monkeypatch.setattr(paths, "panel_live_file", lambda: live_p)
    monkeypatch.setattr(paths, "live_dir", lambda: live_dir)
    monkeypatch.setattr(feeds, "_trading_days", lambda: ["2026-11-25", "2026-11-26", "2026-11-27"])
    feeds.clear_caches()

    # 修复前：build_panel() 停留在 11-25，panel_health() 必须直接报 continuity_broken=True
    ph_before = feeds.panel_health(today="20261127")
    assert ph_before["effective_last_trade_date"] == "2026-11-25"
    assert ph_before["continuity_broken"] is True
    assert "2026-11-26" in ph_before["gap_days"]

    # 自愈回填 2026-11-26 后，build_panel() 畅通到 11-27，panel_ok=True
    res = feeds.backfill_missing_live_bars(today="20261127")
    assert "2026-11-26" in res["backfilled"]
    ph_after = feeds.panel_health(today="20261127")
    assert ph_after["effective_last_trade_date"] == "2026-11-27"
    assert ph_after["continuity_broken"] is False
    assert ph_after["panel_ok"] is True


# ---------------------------------------------------------------------------
# 2. 东财涨停池全量翻页 + 单页 300 截断不冤判炸板
# ---------------------------------------------------------------------------

def test_02_em_pool_pagination_and_truncation_guard(monkeypatch) -> None:
    from backend import feeds, fill_verify, minute_flow, rabbit_state

    class _Resp:
        def __init__(self, data):
            self._d = data

        def json(self):
            return {"data": self._d}

    calls = []

    def fake_get(url, params=None, timeout=10, headers=None):
        pidx = params["Pageindex"]
        calls.append(pidx)
        if pidx == 0:
            pool = [{"c": f"{i + 1:06d}", "n": f"股{i}", "p": 10000, "zdp": 10.0, "lbc": 2, "fbt": 93000, "lbt": 93000, "fund": 1e8, "zbc": 0, "hs": 5.0, "hybk": "算力"} for i in range(300)]
            return _Resp({"tc": 350, "qdate": "20261127", "pool": pool})
        pool = [{"c": f"{i + 301:06d}", "n": f"股{i+300}", "p": 10000, "zdp": 10.0, "lbc": 2, "fbt": 142000, "lbt": 142000, "fund": 1e8, "zbc": 0, "hs": 5.0, "hybk": "算力"} for i in range(50)]
        return _Resp({"tc": 350, "qdate": "20261127", "pool": pool})

    monkeypatch.setattr(feeds.requests, "get", fake_get)
    feeds.clear_caches()
    df = feeds.zt_pool("20261127")
    assert df.height == 350
    assert calls == [0, 1]
    meta = feeds.last_pool_meta("zt", "20261127")
    assert meta["complete"] is True and meta["tc"] == 350

    # 历史截断帧(all_complete=False 且不在 zb 池)不冤判炸板离池、不冤判兔子 failed
    rows_300 = [{"ts_code": f"{i + 1:06d}", "name": "占位", "streak": 2, "broken": 0, "turnover": 4.0, "seal_fund": 1e8, "price": 10.0} for i in range(300)]
    turtle_row = {"ts_code": "000603", "name": "盛达资源", "streak": 2, "broken": 0, "turnover": 6.0, "seal_fund": 1.2e8, "price": 15.0}
    frames = [
        {"ts": "2026-11-27T14:10:00", "zt": {"n": 290, "tc": 290, "all_complete": True, "all": [turtle_row]}, "zb": {"n": 0, "all": []}},
        {"ts": "2026-11-27T14:15:00", "zt": {"n": 300, "tc": 410, "all_complete": False, "all": rows_300}, "zb": {"n": 0, "all": []}},
        {"ts": "2026-11-27T14:55:00", "zt": {"n": 410, "tc": 410, "all_complete": True, "all": rows_300 + [turtle_row]}, "zb": {"n": 0, "all": []}},
    ]
    monkeypatch.setattr(fill_verify, "daily_amount", lambda d, c: 5e8)
    monkeypatch.setattr(minute_flow, "amount_between", lambda c, d, t0, t1: {"ok": True, "amount": 0.05e8})
    sim = fill_verify.simulate_fill("20261127", "000603", "2026-11-27T14:10:00", frames, entry_price=15.0)
    assert sim["status"] == "queued_unfilled"
    assert len(sim["rounds"]) == 0, "页码溢出截断不应触发伪开板轮次"

    rs_trunc = rabbit_state.evaluate_rabbit_state(["000603"], frames[:2])
    assert rs_trunc["state"] != "failed"


# ---------------------------------------------------------------------------
# 3 & 4. 舆情否定窗口/法律常识/退市/海外央行 + 异动降温澄清
# ---------------------------------------------------------------------------

def test_03_04_news_semantics_and_clarification_cooling(monkeypatch) -> None:
    from backend import buy_signals, news_feed, theme_match

    # 1) 新风光：隔 8 个字「不以其他任何方式转让或减持」不应误判 danger
    r_xinfengguang = news_feed.classify_item({
        "id": "x1", "title": "新风光：控股股东等承诺24个月内不减持公司股份",
        "digest": "自本次发行结束之日起24个月内，不以其他任何方式转让或减持公司股票",
        "raw_stocks": ["1.688663"],
    })
    assert r_xinfengguang["sentiment"] != "danger"
    assert "减持" not in r_xinfengguang["danger_hits"]

    # 2) 罗普特/安联锐视：原告追讨维权，法院已立案 ≠ 证监会立案调查
    r_lpt = news_feed.classify_item({
        "id": "x2", "title": "罗普特：公司提起诉讼追讨票据利益2.23亿元，法院已立案尚未开庭",
        "digest": "公司作为原告提起诉讼", "raw_stocks": ["1.688619"],
    })
    r_alrs = news_feed.classify_item({
        "id": "x3", "title": "安联锐视：就货款被侵占提起诉讼，涉案金额1.75亿元",
        "digest": "货款被侵占", "raw_stocks": ["0.301042"],
    })
    assert r_lpt["sentiment"] != "danger" and "立案" not in r_lpt["danger_hits"]
    assert r_alrs["sentiment"] != "danger" and "资金占用" not in r_alrs["danger_hits"]

    # 3) *ST发展：面值退市警告必须标 danger，绝不可成 positive
    r_st = news_feed.classify_item({
        "id": "x4", "title": "*ST发展：股价存在低于面值退市风险",
        "digest": "此前签署战略合作框架协议，但目前股价存在低于面值退市风险",
        "raw_stocks": [],
    })
    assert r_st["sentiment"] == "danger"

    # 4) 英国央行 / 印尼央行：不可标 positive，更不可映射进 AI传媒
    r_boe = news_feed.classify_item({
        "id": "x5", "title": "英国央行：将逆周期缓冲资本率维持在2%不变，警惕AI投资风险",
        "digest": "英国央行发布金融稳定报告", "raw_stocks": [],
    })
    assert r_boe["sentiment"] != "positive"
    assert r_boe["themes"] == []

    # 5) 宏达高科 / 卡莱特：异动澄清降温识别 + 禁止注入 L2 题材 + 扣 -12 分
    r_hd = news_feed.classify_item({
        "id": "x6", "title": "2连板宏达高科：碳纤维复合材料项目处于建设阶段 尚未产生营业收入",
        "digest": "敬请广大投资者注意二级市场交易风险", "raw_stocks": ["0.002144"],
    })
    assert r_hd["is_clarification_cooling"] is True
    assert r_hd["sentiment"] == "negative"

    agg = news_feed.aggregate_news([r_hd])
    assert agg["stock_alerts"]["002144"]["is_clarification_cooling"] is True
    assert "化工周期" not in agg["catalyst_by_theme"]

    bq_clean = buy_signals.evaluate_turtle_quality({"ts_code": "002144", "first_seal": "09:35:00"})
    bq_cool = buy_signals.evaluate_turtle_quality(
        {"ts_code": "002144", "first_seal": "09:35:00"},
        news_alert=agg["stock_alerts"]["002144"],
    )
    assert bq_cool["raw_score"] == pytest.approx(bq_clean["raw_score"] - 12.0)

    # 验证 _news_tags_for_codes 隔离澄清公告
    live_root = ROOT / "tmp_test_news_iso"
    try:
        nf_dir = live_root / "live" / "20261126"
        nf_dir.mkdir(parents=True, exist_ok=True)
        (nf_dir / "news.jsonl").write_text(json.dumps(r_hd, ensure_ascii=False) + "\n", encoding="utf-8")
        from backend import paths
        monkeypatch.setattr(paths, "data_dir", lambda: live_root)
        ntags = theme_match._news_tags_for_codes(before_day="2026-11-27")
        assert "002144" not in ntags
    finally:
        import shutil
        shutil.rmtree(live_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. 外盘交易日历因果锚点休市静默（感恩节周五交易时段必静默 + 正常周一不误杀）+ A50 T+1 夜盘防污染
# ---------------------------------------------------------------------------

def test_05_overseas_stale_silence_and_a50_night_isolation(monkeypatch) -> None:
    from backend import feeds, overseas

    monkeypatch.setattr(feeds, "_trading_days", lambda: ["2026-11-25", "2026-11-26", "2026-11-27", "2026-11-30"])

    # 1) 感恩节次日周五 A股早盘 09:25 与尾盘 14:55：周三美股收盘（周四05:00）<= 周四15:00，全天必须判休市静默
    for hh, mm in ((9, 25), (14, 55)):
        now_fri = datetime(2026, 11, 27, hh, mm, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        meta_kweb = overseas._freshness_meta("2026-11-25 16:00:01", now_cn=now_fri, is_us_et=True)
        meta_spx = overseas._freshness_meta("2026-11-26 05:32:23", now_cn=now_fri, is_us_et=False)
        meta_kospi = overseas._freshness_meta("2026-11-27 09:20:00", now_cn=now_fri, is_us_et=False)
        assert meta_kweb["stale"] is True and meta_kweb["holiday_silent"] is True
        assert meta_spx["stale"] is True
        assert meta_kospi["stale"] is False

    # 2) 正常周一早盘 09:25：周五美股收盘（周六05:00）> 周五15:00，绝不被固定 36h 误判为静默
    now_mon = datetime(2026, 11, 30, 9, 25, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    meta_mon = overseas._freshness_meta("2026-11-27 16:00:01", now_cn=now_mon, is_us_et=True)
    assert meta_mon["stale"] is False

    indices = [
        {"key": "KOSPI", "name": "韩国综合", "pct": 4.34, "stale": False},
        {"key": "KWEB", "name": "中概互联", "pct": -5.0, "stale": True, "holiday_silent": True, "stale_hours": 28.4},
    ]
    sc = overseas.compute_overseas_score(indices)
    assert sc["score"] == pytest.approx(100.0)
    assert any("中概互联" in x for x in sc["silenced_indices"])

    mr = overseas.compute_market_reaction(
        [{"key": "A50", "pct": -0.36, "stale": False}],
        [{"key": "HSTECH", "pct": 6.45}, {"key": "SH", "pct": 9.64}],
        exclude_a50=True,
    )
    assert mr["a50_pct"] is None
    assert "T+1夜盘" in mr["note"]


# ---------------------------------------------------------------------------
# 6, 7, 8. 撤单释放槽位+raw_score 破平 / 细分题材纯度 / 先兔后龟+衰减冻结
# ---------------------------------------------------------------------------

def test_06_07_08_slot_release_purity_and_rabbit_gate(monkeypatch) -> None:
    from backend import buy_signals, live_sim, theme_match

    # 6) 002298 已撤单 + 603386 脏单 → 释放名额，002261 拓维信息（2.5亿封单+3.99亿机构）在黄金5分钟内进场
    groups = {
        "002298": {"ts_code": "002298", "name": "中电鑫龙", "guitu_theme": "算力", "guitu_rabbits": ["常山北明"], "first_seal": "09:30:51", "broken_max": 0, "turn_at_seal": 6.0, "seal_fund": 0.5e8, "first": {"seal_fund": 0.5e8}},
        "000889": {"ts_code": "000889", "name": "中嘉博创", "guitu_theme": "算力", "guitu_rabbits": ["常山北明"], "first_seal": "09:31:00", "broken_max": 0, "turn_at_seal": 7.0, "seal_fund": 0.4e8, "first": {"seal_fund": 0.4e8}},
        "002261": {"ts_code": "002261", "name": "拓维信息", "guitu_theme": "算力", "guitu_rabbits": ["常山北明"], "first_seal": "09:33:45", "broken_max": 0, "turn_at_seal": 8.0, "seal_fund": 2.53e8, "lhb_profile": {"net_amt_yi": 3.99, "tag": "机构大买"}, "first": {"seal_fund": 2.53e8}},
        "600520": {"ts_code": "600520", "name": "三佳科技", "guitu_theme": "算力", "guitu_rabbits": ["常山北明"], "first_seal": "09:34:00", "broken_max": 0, "turn_at_seal": 7.5, "seal_fund": 1.93e8, "first": {"seal_fund": 1.93e8}},
    }
    sel, sk = buy_signals.rank_and_focus_candidates(
        groups, max_per_theme=2, max_total=5,
        cancelled_codes={"002298"}, invalid_codes={"000889"},
    )
    assert set(sel.keys()) == {"002261", "600520"}
    assert sel["002261"]["buy_quality_pre"]["raw_score"] > sel["600520"]["buy_quality_pre"]["raw_score"]

    # 7) 细分题材纯度：豆浆机(九阳股份)不配工业机器人，装修装饰(金螳螂)不配华为昇腾
    p_robot = theme_match.check_pair_purity(
        ["人形机器人", "减速器"], ["小家电", "厨房小家电", "扫地机器人"],
        turtle_name="九阳股份", turtle_industry="小家电", axis_name="机器人",
    )
    assert p_robot["pure"] is False
    p_deco = theme_match.check_pair_purity(
        ["华为昇腾", "国产算力"], ["建筑装饰", "室内设计", "装配式建筑", "算力"],
        turtle_name="金螳螂", turtle_industry="装修装饰", axis_name="算力",
    )
    assert p_deco["pure"] is False
    p_true = theme_match.check_pair_purity(
        ["华为昇腾", "国产算力"], ["华为昇腾", "AI算力", "信创"],
        turtle_name="拓维信息", turtle_industry="软件开发", axis_name="算力",
    )
    assert p_true["pure"] is True

    # 8) 兔子衰减态(封单失血 >= 40%) 冻结后续新开仓 + 必须兔先于龟
    monkeypatch.setattr(
        live_sim, "_theme_tags",
        lambda day, **kw: {"000158": ["华为昇腾", "算力"], "002261": ["华为昇腾", "算力"]},
    )
    fr_decay = [
        {"ts": "2026-11-27T09:25:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "000158", "name": "常山北明", "streak": 2, "broken": 0, "turnover": 1.0, "seal_fund": 3.0e8, "first_seal": 92500},
        ]}},
        {"ts": "2026-11-27T10:30:00", "zt": {"all_complete": True, "all": [
            {"ts_code": "000158", "name": "常山北明", "streak": 2, "broken": 0, "turnover": 1.2, "seal_fund": 0.4e8, "first_seal": 92500},
            {"ts_code": "002261", "name": "拓维信息", "streak": 2, "broken": 0, "turnover": 6.0, "seal_fund": 2.0e8, "first_seal": 102800},
        ]}},
    ]
    picked_decay, notes_decay = live_sim.collect_guitu_targets(fr_decay, "20261127")
    assert picked_decay == {}
    assert any("冻结" in n for n in notes_decay)


# ---------------------------------------------------------------------------
# 9, 10, 11. 002298 厚封防洗盘 / 首封队列与分钟边界打折 / 扫板滑点与跟踪
# ---------------------------------------------------------------------------

def test_09_10_11_cancel_shield_queue_and_sweep(monkeypatch) -> None:
    from backend import fill_verify, live_sim, minute_flow

    # 9) 002298 中电鑫龙：峰值 1.23亿 -> 0.56亿，兔子稳封，剩余≥5000万 → 厚封护栏保护不洗飞
    ev = fill_verify.evaluate_cancel_trigger(
        seal_fund=0.56e8, peak_seal=1.23e8, prev_seal=1.23e8,
        interval_traded_amt=0.05e8, day_amount=5.0e8, broken=0,
        ts="2026-11-27T09:45:22", rabbit_holding=True, float_mv=30e8, market_zt_count=120,
        code="002298",
    )
    assert ev["should_cancel"] is False

    # 9b) 绝对巨封豁免：峰值 7.81亿 -> 2.72亿 (-65%) 且未新增炸板，剩余 >= 1.5亿 严禁洗飞
    ev_mega = fill_verify.evaluate_cancel_trigger(
        seal_fund=2.72e8, peak_seal=7.81e8, prev_seal=2.98e8,
        interval_traded_amt=0.05e8, day_amount=20.88e8, broken=2,
        ts="2026-11-27T14:40:43", rabbit_holding=True, float_mv=100e8, market_zt_count=300,
        code="002261",
    )
    assert ev_mega["should_cancel"] is False

    # 10) 队列时间优先铁律：绝不向下折算 S（seal_fund_at_entry_yi 保持真实 1.5亿），且水下回封分钟剔除水下点火量
    # 09:32 水下 7.68 (< 7.76)，09:32:52 重新封板挂单，前面真实 1.50亿；09:33 成交 0.49亿含水下扫货不得吃穿 1.50亿
    monkeypatch.setattr(fill_verify, "daily_amount", lambda d, c: 4.44e8)
    monkeypatch.setattr(minute_flow, "minute_series", lambda c, d: [
        ("0931", 7.76, 1.44e8),
        ("0932", 7.68, 2.55e8),
        ("0933", 7.76, 3.04e8),  # 增量 0.49亿，上一分钟0932低于涨停价，不能把水下点火当板上消费
        ("0934", 7.76, 3.13e8),
    ])
    monkeypatch.setattr(minute_flow, "amount_between", lambda c, d, t0, t1: {"ok": True, "amount": 0.58e8, "from_hhmm": "0932", "to_hhmm": "0934"})
    frames = [
        {"ts": "2026-11-27T09:32:52", "zt": {"all_complete": True, "all": [
            {"ts_code": "002298", "streak": 2, "broken": 1, "turnover": 6.0, "seal_fund": 1.50e8, "price": 7.76, "first_seal": 93051, "last_seal": 93206},
        ]}},
        {"ts": "2026-11-27T09:34:57", "zt": {"all_complete": True, "all": [
            {"ts_code": "002298", "streak": 2, "broken": 1, "turnover": 6.2, "seal_fund": 1.52e8, "price": 7.76, "first_seal": 93051, "last_seal": 93206},
        ]}},
    ]
    sim = fill_verify.simulate_fill("20261127", "002298", "2026-11-27T09:32:52", frames, entry_price=7.76, enable_cancel=True)
    assert sim["first_seal_audit"]["discount"] < 1.0
    assert sim["seal_fund_at_entry_yi"] == pytest.approx(1.5)
    assert sim["status"] == "queued_unfilled"
    assert (sim.get("sealed_traded_yi") or 0) < 0.58

    # 11) pre_seal_sweep 后续帧未封死涨停 -> sweep_failed_seal=True & broken_max>=1
    st = {
        "orders": {
            "20261127|000777|B1": {
                "id": "20261127|000777|B1", "day": "20261127", "ts_code": "000777",
                "exec_mode": "pre_seal_sweep", "entry_frame_ts": "2026-11-27T09:40:00",
                "status": "filled", "broken_max": None,
            }
        },
        "positions": {
            "20261127|000777|B1": {"id": "20261127|000777|B1", "ts_code": "000777", "status": "holding"}
        },
    }
    sw_frames = [
        {"ts": "2026-11-27T09:40:00", "zt": {"all_complete": True, "all": []}},
        {"ts": "2026-11-27T10:00:00", "zt": {"all_complete": True, "all": []}},
        {"ts": "2026-11-27T14:55:00", "zt": {"all_complete": True, "all": []}},
    ]
    live_sim._update_sweep_orders(st, sw_frames, "20261127")
    assert st["orders"]["20261127|000777|B1"]["sweep_failed_seal"] is True
    assert st["orders"]["20261127|000777|B1"]["broken_max"] >= 1
    assert st["positions"]["20261127|000777|B1"]["sweep_failed_seal"] is True


# ---------------------------------------------------------------------------
# 12, 13, 14. settle 兔子联动透传 / 早盘放量换手不误杀 / 真·分批止盈拆单
# ---------------------------------------------------------------------------

def test_12_13_14_sell_linkage_morning_guard_and_partial_tp(monkeypatch) -> None:
    from backend import live_sim, sell_rules

    # 13) 09:30~09:45 路径 B 红盘(≥+2%)且站在 VWAP 上方，即使放量 vol_surge=True 也不误杀
    pts = [
        {"t": "09:30", "price": 10.20, "avg_price": 10.20, "volume": 1000},
        {"t": "09:31", "price": 10.40, "avg_price": 10.30, "volume": 1000},
        {"t": "09:32", "price": 10.60, "avg_price": 10.40, "volume": 1000},
        {"t": "09:33", "price": 10.85, "avg_price": 10.50, "volume": 1200},
        # 正常爆量换手回踩 +5.5%(10.55) 仍稳在 VWAP(10.52) 上方且跌幅 ~2.76%
        {"t": "09:36", "price": 10.55, "avg_price": 10.52, "volume": 5000},
    ]
    sig = sell_rules.find_adaptive_signal(pts, code="001400", prev_close=10.0, open_price=10.20, entry_kind="sealed")
    assert sig is None

    # 12) settle() 完整透传 rabbit_strong / rabbit_nuclear / peer_turtle_strong 到 build_sell_plan
    called = {}

    def spy_plan(pos, daily, **kw):
        called.update(kw)
        return {"available": False, "reason": "spy"}

    monkeypatch.setattr(live_sim, "build_sell_plan", spy_plan)
    live_sim.settle(
        {"id": "20261126|001400|B1", "ts_code": "001400", "entry_date": "20261126", "entry_price": 10.0},
        [], rabbit_weak=False, rabbit_strong=True, rabbit_nuclear=False, peer_turtle_strong=True,
    )
    assert called["rabbit_strong"] is True
    assert called["peer_turtle_strong"] is True
    assert called["rabbit_nuclear"] is False

    # 14) 真·分批止盈拆单 + 同分钟不拆 + 全平合并为 1 条交割单：
    # 模拟盘中 10:15 兑现半仓 500 股（全平尚未触发 -> 余仓保持 holding），用 partial_realized_pnl 实时计入可用现金（不进顶层 trades），
    # 待断板全平时按股数加权合并写入 1 条最终交割单，杜绝双倍计数污染冷启动/胜率/四分类账本。
    monkeypatch.undo()
    pos = {
        "id": "20261126|002931|B1", "ts_code": "002931", "name": "锋龙股份",
        "entry_date": "20261126", "entry_price": 10.0, "qty": 1000, "notional": 10000.0,
        "broken_max": 0, "status": "holding",
    }
    daily = [{"date": "2026-11-26", "close": 10.0, "zt": True}]
    monkeypatch.setattr(
        live_sim, "build_sell_plan",
        lambda *a, **kw: {
            "available": True,
            "entry_kind": "sealed",
            "window": {"date": "2026-11-27", "intraday": True},
            "decision": {
                "action": "hold", "price": None, "kind": "pending",
                "partial_profit": {"action": "partial_sell", "t": "10:15", "price": 10.45, "detail": "冲高回落锁利"},
            },
        },
    )
    upd = live_sim.settle(pos, daily)
    assert upd["status"] == "holding"
    assert upd["partial_closed"] is True
    assert upd["qty"] == 500
    assert upd["notional"] == pytest.approx(5000.0)
    assert upd["partial_leg"]["id"] == "20261126|002931|B1|PARTIAL"
    assert upd["partial_leg"]["qty"] == 500
    assert upd["partial_leg"]["gross_return"] == pytest.approx(0.045)
    assert upd["partial_realized_pnl"] == pytest.approx(upd["partial_leg"]["realized_pnl"])
    snap_mid = live_sim._account_snapshot({"orders": {}, "trades": []}, [upd], "20261127", px_map={"002931": 10.45}, px_stale=False)
    assert snap_mid["realized_pnl"] == pytest.approx(upd["partial_realized_pnl"])

    # 同分钟已触发全平信号时绝不拆单
    pos_same_min = {
        "id": "20261126|002932|B1", "ts_code": "002932", "name": "测试股",
        "entry_date": "20261126", "entry_price": 10.0, "qty": 1000, "notional": 10000.0,
        "broken_max": 0, "status": "holding",
    }
    plan_same_min = {
        "available": True,
        "window": {"intraday": True, "date": "2026-11-27"},
        "decision": {
            "action": "sell", "price": 10.42, "signal_time": "10:40", "reason": "急杀破均线",
            "partial_profit": {"action": "partial_sell", "t": "10:40", "price": 10.42, "detail": "冲高回落"},
        },
    }
    p_ns, c_ns = live_sim._apply_partial_take_profit(pos_same_min, plan_same_min, daily)
    assert c_ns is None and not p_ns.get("partial_closed")

    # 余仓断板收盘平仓后，两段合并为 1 条交割单（保留 legs）
    monkeypatch.undo()
    daily_close = daily + [{"date": "2026-11-27", "close": 10.20, "zt": False}]
    closed_merged = live_sim.settle(upd, daily_close)
    assert closed_merged["status"] == "closed"
    assert closed_merged["id"] == "20261126|002931|B1"
    assert closed_merged["qty"] == 1000
    assert closed_merged["notional"] == pytest.approx(10000.0)
    assert len(closed_merged["legs"]) == 2
    assert closed_merged["exit_price"] == pytest.approx((10.45 * 500 + 10.20 * 500) / 1000, rel=1e-3)


# ---------------------------------------------------------------------------
# 15, 16, 17, 18. 脏单冲销 / B2 卫星仓 / 同源回测 / 前端动态参数与模块
# ---------------------------------------------------------------------------

def test_15_16_17_18_cleanup_b2_backtest_and_frontend() -> None:
    from backend import evolution, guitu_backtest, live_sim, position_sizing

    # 15) 脏单冲销 + clean_trades 过滤
    st = {
        "orders": {
            "20261127|603386|B1": {"id": "20261127|603386|B1", "ts_code": "603386", "invalid_historical_bug": True, "status": "filled"}
        },
        "positions": {
            "20261127|603386|B1": {"id": "20261127|603386|B1", "ts_code": "603386", "status": "holding", "qty": 3000, "entry_price": 15.97, "notional": 47910.0, "invalid_historical_bug": True}
        },
        "trades": [
            {"id": "20261127|603386|B1", "ts_code": "603386", "entry_date": "20261127", "exit_date": "20261128", "net_return": -0.09, "notional": 47910.0},
            {"id": "20261126|000001|B1", "ts_code": "000001", "entry_date": "20261125", "exit_date": "20261126", "net_return": 0.05, "notional": 100000.0},
        ],
    }
    n_void = live_sim.sanitize_invalid_positions(st)
    assert n_void == 1
    assert "20261127|603386|B1" not in st["positions"]
    assert "20261127|603386|B1" in st["voided_positions"]
    assert len(st["trades"]) == 1
    cb = position_sizing.account_circuit_breaker(st["trades"], "20261129")
    assert cb["mode"] == "normal"

    # 16) live_sim 源码已接入 B2_SAT 0.3× 卫星仓
    ls_src = (ROOT / "backend" / "live_sim.py").read_text(encoding="utf-8")
    assert "evaluate_b2_turnover_challenger" in ls_src
    assert "B2_SAT" in ls_src

    # 17) guitu_backtest 提供同源实盘口径 replay_live_aligned
    assert hasattr(guitu_backtest, "replay_live_aligned")

    # 18) 前端文案 & 后端口径统一，不含 10% 挥发硬编码、不含过时 10 因子与错误排序
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for elem_id in ("macroAlertBanner", "lhbBox", "rabbitStatesWrap", "skippedWrap", "t1RelayWrap", "fillCatWrap", "cancelCfWrap"):
        assert elem_id in html and elem_id in js
    assert "每日自然挥发 10%" not in html
    assert "次日按 10% 自然挥发" not in js
    assert "10 因子独立信息素画像" not in html
    assert "不自动下单" not in js
    assert 'k.includes("同源实盘")' in js
    assert "rho_evaporation || 0.1" not in js
    assert evolution.RHO == pytest.approx(0.03)


def test_19_round2_process_guard_rabbit_purity_backtest_b2_and_macro_filter(tmp_path, monkeypatch) -> None:
    from backend import feeds, guitu_backtest, live_sim, process_guard, rabbit_state, theme_match

    # 1) process_guard 乐观锁拦截旧版僵尸进程写盘
    monkeypatch.setattr(process_guard, "source_build", lambda: process_guard.PROCESS_START_BUILD + 60.0)
    target = tmp_path / "sim_state.json"
    assert process_guard.can_write("test.write", target, force_check=True) is False
    st_pg = process_guard.status()
    assert st_pg["stale"] is True and st_pg["stale_write_blocked"] is True
    monkeypatch.undo()

    # 2) 兔子绝对巨封豁免(9.4亿->4.25亿 br=0)、忽略 14:57 后帧、早盘空帧返回 pending、因果帧切片不穿越
    fr_giant = [
        {"ts": "2026-11-27T09:15:03", "zt": {"all_complete": True, "all": []}},
        {"ts": "2026-11-27T09:30:47", "zt": {"all_complete": True, "all": [
            {"ts_code": "000158", "name": "常山北明", "streak": 2, "broken": 0, "seal_fund": 9.40e8, "first_seal": 92500},
        ]}},
        {"ts": "2026-11-27T09:33:54", "zt": {"all_complete": True, "all": [
            {"ts_code": "000158", "name": "常山北明", "streak": 2, "broken": 0, "seal_fund": 9.08e8, "first_seal": 92500},
            {"ts_code": "002261", "name": "拓维信息", "streak": 2, "broken": 0, "seal_fund": 2.53e8, "first_seal": 93345, "turnover": 4.0},
        ]}},
        {"ts": "2026-11-27T14:55:30", "zt": {"all_complete": True, "all": [
            {"ts_code": "000158", "name": "常山北明", "streak": 2, "broken": 0, "seal_fund": 4.25e8, "first_seal": 92500},
            {"ts_code": "002261", "name": "拓维信息", "streak": 2, "broken": 0, "seal_fund": 2.48e8, "first_seal": 93345, "turnover": 8.0},
        ]}},
        {"ts": "2026-11-27T15:05:42", "zt": {"all_complete": True, "all": [
            {"ts_code": "000158", "name": "常山北明", "streak": 2, "broken": 0, "seal_fund": 0.01e8, "first_seal": 92500},
        ]}},
    ]
    assert rabbit_state.evaluate_rabbit_state(["000158"], fr_giant[:1])["state"] == "pending"
    rs_158 = rabbit_state.evaluate_rabbit_state(["000158"], fr_giant)
    assert rs_158["state"] == "anchored"
    assert rs_158["rabbits"][0]["latest_seal_yi"] == pytest.approx(4.25)

    # 3) 题材纯度校验：拦截东易日盛/大胜达/泰嘉股份，放行三佳科技/快克智能/拓维信息
    r_tags = ["华为昇腾", "算力", "词元工厂"]
    assert theme_match.check_pair_purity(r_tags, ["算力运营", "资产注入", "AI家装"], turtle_name="东易日盛", turtle_industry="装修装饰", axis_name="算力")["pure"] is False
    assert theme_match.check_pair_purity(r_tags, ["芯瞳半导体（GPU芯片）", "纸包装"], turtle_name="大胜达", turtle_industry="包装印刷", axis_name="算力")["pure"] is False
    assert theme_match.check_pair_purity(r_tags, ["服务器电源", "商业航天", "锯切龙头"], turtle_name="泰嘉股份", turtle_industry="通用设备", axis_name="算力")["pure"] is False
    assert theme_match.check_pair_purity(r_tags, ["精密减速器", "机器人", "商业航天", "液冷"], turtle_name="飞亚达", turtle_industry="饰品", axis_name="算力")["pure"] is True
    assert theme_match.check_pair_purity(r_tags, ["精密减速器", "机器人", "商业航天", "国企改革", "液冷"], turtle_name="边角股", turtle_industry="通用设备", axis_name="算力")["pure"] is False
    assert theme_match.check_pair_purity(r_tags, ["先进封装", "半导体封装装备"], turtle_name="三佳科技", turtle_industry="半导体", axis_name="算力")["pure"] is True
    assert theme_match.check_pair_purity(r_tags, ["光模块检测", "先进封装"], turtle_name="快克智能", turtle_industry="自动化设", axis_name="算力")["pure"] is True
    assert theme_match.check_pair_purity(r_tags, ["华为昇腾", "算力", "机器人"], turtle_name="拓维信息", turtle_industry="软件开发", axis_name="算力")["pure"] is True

    # 4) B2_SAT 高潮/防守期硬拦截 + guitu_rabbits 不再塞文字描述
    ls_src = (ROOT / "backend" / "live_sim.py").read_text(encoding="utf-8")
    assert 'climax_ebb.get("climax_active")' in ls_src
    assert 'cb.get("mode") != "normal"' in ls_src
    assert '_bg_sat["guitu_rabbits"] = []' in ls_src
    assert "b2_anchor_type" in ls_src

    # 5) guitu_backtest.replay_live_aligned 不得注入假 "09:40:00" 或跨题材兔子
    gb_src = (ROOT / "backend" / "guitu_backtest.py").read_text(encoding="utf-8")
    assert '"09:40:00"' not in gb_src
    assert "day_rabbits[:2]" not in gb_src
    assert "same_theme_rabbits" in gb_src

    # 6) /api/macro-intel 过滤脏单和已冲销持仓
    import app as app_mod
    app_src = Path(app_mod.__file__).read_text(encoding="utf-8")
    assert "voided_positions" in app_src and "is_invalid_record" in app_src

    # 7) 实盘 sync 盘后(>=14:57)禁止补生成新幻影委托
    assert 'hhmm_last >= "14:57"' in ls_src and "禁止新增尾盘/盘后委托" in ls_src
