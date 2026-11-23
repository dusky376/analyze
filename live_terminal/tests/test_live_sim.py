"""实时模拟账本 B1 的行为测试（全部用注入数据，不依赖真实行情文件）。

守住的硬边界：
- 不假设排到就成交；一字/未开板/开板次数缺失一律不建仓
- 断板结算只用真实日线收盘价，日线未落盘不猜价
- 单向状态迁移 + 幂等，重复 sync 不重复开仓/结算
- 成本三口径并列，不擅自裁定
"""
from __future__ import annotations

from typing import Any

import pytest

from backend import live_sim


def _frame(ts: str, rows: list[dict[str, Any]], *, full: bool = True,
           n: int | None = None) -> dict[str, Any]:
    zt: dict[str, Any] = {"n": n if n is not None else len(rows), "top": rows[:30],
                          "ok": True, "err": None}
    if full:
        zt["all"] = rows
        zt["all_complete"] = True
    return {"ts": ts, "zt": zt, "zb": {"n": 0, "top": []}, "dt": {"n": 0, "top": []}}


def _row(code: str, name: str, *, broken: int | None = 0,
         streak: int = 2, price: float = 10.0) -> dict[str, Any]:
    return {"ts_code": code, "name": name, "price": price, "pct": 10.0,
            "streak": streak, "industry": "测试行业", "turnover": 12.0,
            "seal_fund": 1_000_000, "broken": broken}

def _rabbit_row(code: str, name: str = "兔", *, streak: int = 2) -> dict[str, Any]:
    """一字板：当日 0 次开板 + 09:25 首封 + 换手 1%。"""
    r = _row(code, name, broken=0, streak=streak)
    r["first_seal"] = 92500
    r["last_seal"] = 92500
    r["turnover"] = 1.0
    return r

def _gt_frames(*, open_a: bool = True, open_b: bool = False) -> list[dict[str, Any]]:
    """兔子(000009) + 两只乌龟(000001/000002) 的两帧序列。

    第二帧里 open_a/open_b 决定该乌龟是否**在挂单之后**开板。
    逐帧推进口径下：只有后续帧开板（或离池）才成交。
    """
    r = _rabbit_row("000009")
    f1 = _frame("t1", [r, _row("000001", "甲", broken=0),
                       _row("000002", "乙", broken=0)])
    f2 = _frame("t2", [r, _row("000001", "甲", broken=1 if open_a else 0),
                       _row("000002", "乙", broken=1 if open_b else 0)])
    return [f1, f2]





@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(live_sim, "STATE_FILE", tmp_path / "paper" / "sim_state.json")
    monkeypatch.setattr(live_sim, "SNAPSHOT", tmp_path / "paper" / "live_sim.json")
    monkeypatch.setattr(live_sim, "LIVE_DIR", tmp_path / "live")
    monkeypatch.setattr(live_sim, "PANEL", tmp_path / "none.parquet")
    # 2026-11-16 新规则：选股按题材分组。测试里把题材标签打桩成统一一个题材 T，
    # 这样「兔子 + 乌龟」同题材，其余机制（幂等/成交/结算）仍可独立验证。
    monkeypatch.setattr(live_sim, "_theme_tags",
                        lambda day, **kw: {"000001": ["T"], "000002": ["T"],
                                           "000003": ["T"], "000009": ["T"]})
    # 情绪闸门在测试里一律放行（它依赖真实面板，单测不覆盖）
    # 排板模拟依赖「当日成交额」；单测里打桩为 None → 走二元回退路径
    # （与真实场景「缺队列数据时退回开板即成交并标 unverified」一致）
    try:
        from backend import fill_verify as _fv
        monkeypatch.setattr(_fv, "daily_amount", lambda *a, **k: None)
    except Exception:
        pass
    monkeypatch.setattr(live_sim, "emotion_gate",
                        lambda day, **kw: {"available": True, "blocked": False,
                                           "n_high": 0, "avg_ret": 0.0,
                                           "limit_down_share": 0.0})
    return tmp_path


# --- collect_two_board -----------------------------------------------------

def test_collect_uses_full_frame_and_marks_truncated() -> None:
    rows = [_row("000001", "甲"), _row("000002", "乙", streak=3)]
    g, notes = live_sim.collect_two_board([_frame("2026-11-13T10:00:00", rows)])
    assert set(g) == {"000001"}
    assert notes == []
    g2, notes2 = live_sim.collect_two_board([
        _frame("2026-11-13T10:00:00", rows, full=False, n=41)])
    assert set(g2) == {"000001"}
    assert "top-30 截断" in notes2[0]


def test_collect_excludes_st_and_non_two_board() -> None:
    rows = [_row("000001", "正常"), _row("000002", "*ST测试"),
            _row("000003", "三板", streak=3), _row("000004", "退市股")]
    g, _ = live_sim.collect_two_board([_frame("t", rows)])
    assert set(g) == {"000001"}


def test_build_order_only_queues_no_future_data() -> None:
    """build_order 只挂单，不得用全天 broken_max 判成交（那是有未来函数）。

    2026-11-16：旧实现 `build_order` 拿 `broken_max`（收盘后才知的全天最大开板
    次数）直接判 filled，等于 09:30 挂单时就知道下午会不会炸板。
    broken_max 仍保留为**事后复盘证据**，但不参与成交判定。
    """
    frames = [_frame("t1", [_row("000001", "甲", broken=0)]),
              _frame("t2", [_row("000001", "甲", broken=3)]),
              _frame("t3", [_row("000001", "甲", broken=1)])]
    g, _ = live_sim.collect_two_board(frames)
    assert g["000001"]["broken_max"] == 3        # 证据仍在
    assert g["000001"]["n_frames"] == 3
    order = live_sim.build_order("20261113", "000001", g["000001"])
    assert order["status"] == "queued"           # 只挂单，不判成交
    assert order["broken_at_entry"] == 0         # 挂单时刻的开板数
    assert order["entry_price"] == 10.0          # 首帧涨停价


def test_advance_fills_uses_only_later_frames(monkeypatch) -> None:
    from backend import fill_verify as _fv
    monkeypatch.setattr(_fv, "daily_amount", lambda *a, **k: None)
    """成交发生在「后续帧开板」那一刻，而不是挂单时就知道。"""
    frames = [_frame("t1", [_row("000001", "甲", broken=0)]),
              _frame("t2", [_row("000001", "甲", broken=0)]),
              _frame("t3", [_row("000001", "甲", broken=1)])]
    g, _ = live_sim.collect_two_board(frames)
    state = {"orders": {"20261113|000001|B1":
                        live_sim.build_order("20261113", "000001", g["000001"])},
             "positions": {}, "days": []}
    n = live_sim.advance_fills(state, frames, "20261113")
    o = state["orders"]["20261113|000001|B1"]
    assert n == 1
    assert o["status"] == "filled"
    assert o["fill_frame_ts"] == "t3"
    assert o["fill_price"] == 10.0


def test_advance_fills_ignores_open_before_entry() -> None:
    """挂单前已开板、挂单后没再开 → 不该成交（旧逻辑会误判 filled）。"""
    frames = [_frame("t1", [_row("000001", "甲", broken=2)]),
              _frame("t2", [_row("000001", "甲", broken=2)])]
    g, _ = live_sim.collect_two_board(frames)
    state = {"orders": {"20261113|000001|B1":
                        live_sim.build_order("20261113", "000001", g["000001"])},
             "positions": {}, "days": []}
    live_sim.advance_fills(state, frames, "20261113")
    assert state["orders"]["20261113|000001|B1"]["status"] == "queued_unfilled"


def test_advance_fills_fills_when_leaves_pool(monkeypatch) -> None:
    from backend import fill_verify as _fv
    monkeypatch.setattr(_fv, "daily_amount", lambda *a, **k: None)
    """炸板离开涨停池 = 涨停被打开 → 涨停价委托成交。"""
    frames = [_frame("t1", [_row("000001", "甲", broken=0)]),
              _frame("t2", [])]
    g, _ = live_sim.collect_two_board(frames)
    state = {"orders": {"20261113|000001|B1":
                        live_sim.build_order("20261113", "000001", g["000001"])},
             "positions": {}, "days": []}
    n = live_sim.advance_fills(state, frames, "20261113")
    o = state["orders"]["20261113|000001|B1"]
    assert n == 1 and o["status"] == "filled"
    assert o["fill_frame_ts"] == "t2"


def test_missing_broken_is_unknown_not_filled() -> None:
    g, _ = live_sim.collect_two_board([
        _frame("t", [_row("000001", "甲", broken=None)])])
    order = live_sim.build_order("20261113", "000001", g["000001"])
    assert order["status"] == "unknown"
    assert order["queue"]["filled"] is None


def test_never_open_board_is_not_filled() -> None:
    """全天不开板且始终在池 → 排板未成交。"""
    frames = [_frame("t1", [_row("000001", "甲", broken=0)]),
              _frame("t2", [_row("000001", "甲", broken=0)]),
              _frame("t3", [_row("000001", "甲", broken=0)])]
    g, _ = live_sim.collect_two_board(frames)
    state = {"orders": {"20261113|000001|B1":
                        live_sim.build_order("20261113", "000001", g["000001"])},
             "positions": {}, "days": []}
    live_sim.advance_fills(state, frames, "20261113")
    assert state["orders"]["20261113|000001|B1"]["status"] == "queued_unfilled"


def _daily(*spec: tuple[str, float, bool]) -> list[dict[str, Any]]:
    return [{"date": d, "close": c, "zt": z} for d, c, z in spec]


def test_find_exit_first_non_zt_day() -> None:
    daily = _daily(("20261113", 10.0, True), ("20261114", 11.0, True),
                   ("20261117", 10.4, False), ("20261118", 9.0, False))
    row, status = live_sim.find_exit("20261113", daily)
    assert status == "closed" and row["date"] == "20261117" and row["close"] == 10.4


def test_find_exit_pending_and_censored() -> None:
    assert live_sim.find_exit("20261113", _daily(("20261113", 10.0, True)))[1] == \
        "pending_daily"
    assert live_sim.find_exit("20261113", _daily(
        ("20261113", 10.0, True), ("20261114", 11.0, True)))[1] == "censored"


def test_settle_uses_real_close_and_all_three_costs() -> None:
    pos = {"id": "x", "entry_date": "20261113", "entry_price": 10.0,
           "status": "holding"}
    out = live_sim.settle(pos, _daily(
        ("20261113", 10.0, True), ("20261114", 11.0, True),
        ("20261117", 11.5, False)))
    assert out["status"] == "closed"
    assert out["exit_date"] == "20261117" and out["exit_price"] == 11.5
    assert out["gross_return"] == pytest.approx(0.15)
    assert out["board_days"] == 1
    assert set(out["net_by_cost"]) == set(live_sim.COST_VARIANTS)
    assert out["net_by_cost"]["0.36%_papertrade"] == pytest.approx(0.1464)
    assert pos["status"] == "holding"          # 不改入参


def test_settle_refuses_bad_price() -> None:
    out = live_sim.settle({"id": "x", "entry_date": "20261113",
                          "entry_price": 0, "status": "holding"},
                         _daily(("20261114", 11.0, False)))
    assert out["status"] == "holding" and "拒绝结算" in out["state_note"]


# --- sync 端到端（幂等 / 单向迁移 / 无数据不编造） ----------------------------

def test_sync_idempotent_and_no_fake_data(isolated) -> None:
    """逐帧推进口径：只有「挂单后开板」的乌龟成交。"""
    frames = _gt_frames(open_a=True, open_b=False)
    daily = {"000001": _daily(("20261113", 10.0, True))}
    r1 = live_sim.sync("20261113", frames=frames, daily=daily)
    assert r1["n_today"] == 2
    assert r1["n_filled_today"] == 1 and r1["n_unfilled_today"] == 1
    assert r1["n_holding"] == 1
    assert r1["summary"]["n_trades_closed"] == 0

    r2 = live_sim.sync("20261113", frames=frames, daily=daily)
    assert r2["summary"]["n_orders_all"] == 2
    assert r2["summary"]["n_filled_all"] == 1
    assert r2["n_holding"] == 1                  # 没有重复建仓


def test_sync_upgrades_unfilled_to_filled_once_opened(isolated) -> None:
    """首遍只有挂单；第二遍看到后续帧开板 → 成交并建仓。"""
    live_sim.sync("20261113",
                  frames=[_frame("t1", [_rabbit_row("000009"),
                                        _row("000001", "甲", broken=0)])],
                  daily={})
    r = live_sim.sync("20261113",
                      frames=[_frame("t1", [_rabbit_row("000009"),
                                            _row("000001", "甲", broken=0)]),
                              _frame("t2", [_rabbit_row("000009"),
                                            _row("000001", "甲", broken=1)])],
                      daily={"000001": _daily(("20261113", 10.0, True))})
    assert r["n_filled_today"] == 1 and r["n_holding"] == 1
    o = r["today_orders"][0]
    assert o["fill_frame_ts"] == "t2"
    assert "开板" in (o.get("fill_reason") or "")


def test_sync_filled_never_reverts(isolated) -> None:
    """已成交状态不得因后续帧 broken 回落而回退。"""
    live_sim.sync("20261113", frames=_gt_frames(open_a=True), daily={})
    f3 = _frame("t3", [_rabbit_row("000009"), _row("000001", "甲", broken=0),
                       _row("000002", "乙", broken=0)])
    r = live_sim.sync("20261113", frames=_gt_frames(open_a=True) + [f3], daily={})
    o = [x for x in r["today_orders"] if x["ts_code"] == "000001"][0]
    assert o["status"] == "filled"


def test_sync_settles_only_after_real_daily_lands(isolated) -> None:
    frames = _gt_frames(open_a=True, open_b=False)
    live_sim.sync("20261113", frames=frames, daily={"000001": []})
    # 2026-11-17 修复：退出日若收盘跌停封死，find_exit 会顺延（不可成交不假装成交）。
    # 原断言用 9.5（相对 11.0 = −13.6%）在主板涨跌停制度下是不可能的收盘价。
    # 这里用 9.95（相对前收 11.0 为 −9.55%，未触及跌停价 9.9），
    # 保持「真实日线到了才结算」的原意；跌停顺延路径见 quant-paper 新增用例。
    r = live_sim.sync("20261113", frames=frames, daily={"000001": _daily(
        ("20261113", 10.0, True), ("20261114", 11.0, True),
        ("20261117", 9.95, False))})
    assert r["summary"]["n_trades_closed"] == 1
    assert r["summary"]["gross"]["n"] == 1
    t = r["trades"][0]
    assert t["exit_date"] == "20261117" and t["exit_price"] == 9.95
    assert t["gross_return"] == pytest.approx(-0.005, abs=1e-9)


def test_sync_empty_frames_creates_nothing(isolated) -> None:
    r = live_sim.sync("20261113", frames=[], daily={})
    assert r["n_today"] == 0 and r["summary"]["n_orders_all"] == 0
    assert "无涨停池留档帧" in r["notes"][0]


def test_unknown_order_never_creates_position(isolated) -> None:
    r = live_sim.sync("20261113",
                      frames=[_frame("t1", [_rabbit_row("000009"),
                                            _row("000001", "甲", broken=None)])],
                      daily={})
    assert r["n_unknown_today"] == 1 and r["n_holding"] == 0


def test_sync_refreshes_turnover_with_role_evidence(isolated) -> None:
    """同一委托单的「换手%」列与龟兔判定证据必须来自同一末帧。

    13:36 实测：老订单 turnover 停在首帧 2.45%，而角色证据用末帧 2.82%，
    页面同一行出现两个换手率。此处守住两者一致。
    """
    r1 = {**_row("000001", "甲", broken=1), "turnover": 12.4,
          "first_seal": 100000, "last_seal": 100000, "seal_fund": 5e8}
    live_sim.sync("20261113",
                  frames=[_frame("t1", [_rabbit_row("000009"), r1])], daily={})
    r2 = {**r1, "turnover": 12.8}
    out = live_sim.sync("20261113",
                        frames=[_frame("t1", [_rabbit_row("000009"), r1]),
                                _frame("t2", [_rabbit_row("000009"), r2])],
                        daily={})
    o = out["today_orders"][0]
    assert o["turnover"] == pytest.approx(12.8)
    assert o["guitu"]["evidence"]["turnover"] == round(12.8, 2)
    assert o["guitu"]["role"] == "turtle"   # 换手 12.8% 已非一字，角色应为乌龟


# --- 2026-11-16 新规则：题材内有一字板 → 买该题材其他二板 -----------------

def test_no_rabbit_means_no_order(isolated) -> None:
    """同题材里没有一字板 → 一只都不买（旧规则会全买，这是本次修复的核心）。"""
    frames = [_frame("t1", [_row("000001", "甲", broken=0),
                            _row("000002", "乙", broken=2)])]
    r = live_sim.sync("20261113", frames=frames, daily={})
    assert r["n_today"] == 0
    assert any("没有" in n and "一字板" in n for n in r["notes"])


def test_rabbit_itself_is_not_bought(isolated) -> None:
    """一字板本身买不进，也不该出现在候选里；只买同题材其他二板。"""
    frames = [_frame("t1", [_rabbit_row("000009"),
                            _row("000001", "甲", broken=2)])]
    r = live_sim.sync("20261113", frames=frames, daily={})
    assert r["n_today"] == 1
    assert r["today_orders"][0]["ts_code"] == "000001"


def test_rabbit_unlocks_all_other_two_boards_in_theme(isolated) -> None:
    """有几个题材就买几个：同题材下的其他二板全部纳入。"""
    frames = [_frame("t1", [_rabbit_row("000009"),
                            _row("000001", "甲", broken=2),
                            _row("000002", "乙", broken=1)])]
    r = live_sim.sync("20261113", frames=frames, daily={})
    assert r["n_today"] == 2
    assert {o["ts_code"] for o in r["today_orders"]} == {"000001", "000002"}


def test_oneword_requires_low_turnover_and_early_seal() -> None:
    """一字板判定：必须 0 开板 + 首封≤09:30 + 换手<5%。"""
    base = {"broken_max": 0, "broken_seen": True, "first_seal": 92500,
            "turnover": 1.0}
    assert live_sim.is_oneword(dict(base)) is True
    assert live_sim.is_oneword({**base, "turnover": 9.0}) is False      # 换手高
    assert live_sim.is_oneword({**base, "first_seal": 103000}) is False  # 封得晚
    assert live_sim.is_oneword({**base, "broken_max": 1}) is False       # 开过板
    assert live_sim.is_oneword({**base, "turnover": None}) is False      # 不可判定


# --- 排板队列模拟（fill_verify.simulate_fill）--------------------------------
#
# 独立验证「逐轮开板累积消耗队列」算法，显式控制换手/封单/成交额，
# 不依赖真实面板（避免 000001 恰好是真实股票带来的干扰）。

def _qframes(rows_by_ts):
    """rows_by_ts: [(ts, [(code, turnover, seal, broken)])] → 帧序列。"""
    out = []
    for ts, rows in rows_by_ts:
        rr = []
        for code, to, seal, br in rows:
            rr.append({"ts_code": code, "name": "甲", "price": 10.0,
                       "pct": 10.0, "streak": 2, "industry": "T",
                       "turnover": to, "seal_fund": seal, "broken": br,
                       "first_seal": 100000, "last_seal": 100000})
        out.append(_frame(ts, rr))
    return out


def test_queue_fill_when_consumption_exceeds_queue(monkeypatch) -> None:
    """开板期间成交额 ≥ 队列 → 排到。"""
    from backend import fill_verify as fv
    monkeypatch.setattr(fv, "daily_amount", lambda *a, **k: 2.0e8)
    fv._AMT_CACHE.clear()
    # 挂单时封单 1 亿、换手 10%；开板后换手 20%
    frames = _qframes([
        ("t1", [("000001", 10.0, 1.0e8, 0)]),
        ("t2", [("000001", 20.0, 1.0e8, 1)]),
    ])
    r = fv.simulate_fill("20261113", "000001", "t1", frames)
    assert r["status"] == "filled"
    assert r["fill_round"] == 1
    assert r["rounds"][0]["amount_yi"] == pytest.approx(1.0)   # 2亿×(10/20)


def test_queue_unfilled_when_consumption_below_queue(monkeypatch) -> None:
    """开板期间成交额 < 队列 → 排不到。"""
    from backend import fill_verify as fv
    monkeypatch.setattr(fv, "daily_amount", lambda *a, **k: 1.0e8)
    fv._AMT_CACHE.clear()
    frames = _qframes([
        ("t1", [("000001", 10.0, 1.0e8, 0)]),
        ("t2", [("000001", 20.0, 1.0e8, 1)]),
    ])
    r = fv.simulate_fill("20261113", "000001", "t1", frames)
    assert r["status"] == "queued_unfilled"


def test_queue_accumulates_across_rounds(monkeypatch) -> None:
    """多轮开板：每轮用「开板前」的封单当队列，未吃穿就继续排。

    新模型（2026-11-16）：队列大小取**开板前最后一帧**的封单额，
    因为排队期间封单一直在被吃掉，不能拿挂单时的值。
    """
    from backend import fill_verify as fv
    monkeypatch.setattr(fv, "daily_amount", lambda *a, **k: 4.0e8)
    fv._AMT_CACHE.clear()
    # 封单逐轮衰减：3.0 → 2.0 → 1.5 → 0.5 亿；换手 0/5/10/30
    frames = _qframes([
        ("t1", [("000001", 0.0, 3.0e8, 0)]),
        ("t2", [("000001", 5.0, 2.0e8, 1)]),    # 队列 3.0亿，吃 0.67亿
        ("t3", [("000001", 10.0, 1.5e8, 2)]),   # 队列 2.0亿，吃 0.67亿
        ("t4", [("000001", 30.0, 0.5e8, 3)]),   # 队列 1.5亿，吃 2.67亿 → 吃穿
    ])
    r = fv.simulate_fill("20261113", "000001", "t1", frames)
    assert r["status"] == "filled"
    assert len(r["rounds"]) == 3
    assert r["rounds"][0]["filled"] is False
    assert r["rounds"][-1]["filled"] is True
    # 每轮队列取的应是「开板前」的封单，逐轮递减
    qs = [x["queue_yi"] for x in r["rounds"]]
    assert qs == [3.0, 2.0, 1.5]


def test_queue_never_opened_means_unfilled(monkeypatch) -> None:
    """全程不开板且不离池 → 队列不可能被吃穿。"""
    from backend import fill_verify as fv
    monkeypatch.setattr(fv, "daily_amount", lambda *a, **k: 5.0e8)
    fv._AMT_CACHE.clear()
    frames = _qframes([
        ("t1", [("000001", 5.0, 1.0e8, 0)]),
        ("t2", [("000001", 10.0, 1.0e8, 0)]),
    ])
    r = fv.simulate_fill("20261113", "000001", "t1", frames)
    assert r["status"] == "queued_unfilled"
    assert r["detail"].startswith("全天未开板")
