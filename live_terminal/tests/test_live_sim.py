"""实时模拟账本 B1 的行为测试（全部用注入数据，不依赖真实行情文件）。

守住的硬边界：
- 不假设排到就成交；一字/未开板/开板次数缺失一律不建仓
- 断板结算只用真实日线收盘价，日线未落盘不猜价
- 单向状态迁移 + 幂等，重复 sync 不重复开仓/结算
- 成本单一口径：万0.84免五（用户 2026-11-23 裁定）
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


def test_settle_uses_real_close_and_single_cost_wan084() -> None:
    pos = {"id": "x", "entry_date": "20261113", "entry_price": 10.0,
           "status": "holding"}
    out = live_sim.settle(pos, _daily(
        ("20261113", 10.0, True), ("20261114", 11.0, True),
        ("20261117", 11.5, False)))
    assert out["status"] == "closed"
    assert out["exit_date"] == "20261117" and out["exit_price"] == 11.5
    assert out["gross_return"] == pytest.approx(0.15)
    assert out["board_days"] == 1
    # 单一口径（万0.84免五）：净收益 = 毛收益 − 0.003688
    from backend import costs
    assert out["net_return"] == pytest.approx(
        0.15 - costs.DEFAULT.round_trip_rate(), abs=1e-6)
    assert "net_by_cost" not in out          # 三口径已移除
    assert pos["status"] == "holding"          # 不改入参


def test_settle_refuses_bad_price() -> None:
    out = live_sim.settle({"id": "x", "entry_date": "20261113",
                          "entry_price": 0, "status": "holding"},
                         _daily(("20261114", 11.0, False)))
    assert out["status"] == "holding" and "拒绝结算" in out["state_note"]


# --- 账户面板：今日盈亏归属日（审计 P0，2026-11-25） -------------------------

def test_account_today_realized_attributed_to_exit_date() -> None:
    """今日实现盈亏按**卖出日**归属，不是买入日。

    T+1 下当天买入不可能当天卖出。旧实现用 `entry_date == day` 判定，
    导致「昨日买入、今日平仓」的全部收益漏计——2026-11-25 实测
    20 笔平仓净亏 -23,145，面板却显示 today_pnl = 0。
    这里用构造账本锁定正确语义，避免再次回归。
    """
    day = "20261125"
    state = {
        "trades": [
            # 昨日买入 / 今日卖出 → 应计入今日
            {"entry_date": "20261124", "exit_date": "20261125",
             "net_return": -0.05, "notional": 100_000.0},
            # 今日买入 / 未来卖出 → 不应计入今日
            {"entry_date": "20261125", "exit_date": "20261126",
             "net_return": 0.10, "notional": 100_000.0},
            # 更早平仓 → 只进累计，不进今日
            {"entry_date": "20261113", "exit_date": "2026-11-20",
             "net_return": 0.08, "notional": 100_000.0},
        ]
    }
    # 空持仓 → 不依赖 quotes 留档，today_float 必为 0
    out = live_sim._account_snapshot(state, [], day)
    assert out["today_realized"] == pytest.approx(-5_000.0)
    assert out["today_float"] == 0.0
    assert out["today_pnl"] == pytest.approx(-5_000.0)
    # 累计实现盈亏仍包含全部三笔（不受归属日改动影响）
    assert out["realized_pnl"] == pytest.approx(-5_000.0 + 10_000.0 + 8_000.0)

    # exit_date 带横线也要能识别（历史账本两种格式并存）
    state["trades"][0]["exit_date"] = "2026-11-25"
    out2 = live_sim._account_snapshot(state, [], day)
    assert out2["today_realized"] == pytest.approx(-5_000.0)


def test_account_today_realized_zero_when_no_exit_today() -> None:
    """当日没有平仓时 today_realized 必须为 0（防止误用 entry_date 反向误计）。"""
    state = {"trades": [
        {"entry_date": "20261124", "exit_date": "20261125",
         "net_return": -0.05, "notional": 100_000.0},
    ]}
    out = live_sim._account_snapshot(state, [], "20261126")
    assert out["today_realized"] == 0.0
    assert out["today_pnl"] == 0.0


# --- 盘中平仓：跌停顺延留痕（审计 P2） --------------------------------------

def test_close_by_plan_records_limit_down_from_daily() -> None:
    """盘中信号卖出也须按真实日线留痕卖出日跌停状态，不再硬编码 False。"""
    pos = {"ts_code": "000001", "entry_price": 10.0, "qty": 1000,
           "notional": 10_000.0, "status": "holding"}
    plan = {"entry_kind": "broke_and_stayed",
            "window": {"date": "20261125", "intraday": True}}
    dec = {"action": "sell", "price": 9.0, "kind": "signal",
           "reason": "测试快跌信号"}
    # 卖出日跌停：前收 10.0、跌停价 9.0 → 收盘 9.0 触及跌停
    daily = [{"date": "20261124", "close": 10.0, "zt": True},
             {"date": "20261125", "close": 9.0, "zt": False}]
    out = live_sim._close_by_plan(pos, plan, dec, daily)
    assert out["status"] == "closed"
    assert out["break_is_limit_down"] is True
    assert out["exit_blocked"] is True
    assert out["exit_blocked_days"] == 1
    assert out["exit_blocked_dates"] == ["20261125"]
    assert "跌停" in out["exit_blocked_note"]
    # 卖出价不被改写（仍是信号价）
    assert out["exit_price"] == 9.0


def test_close_by_plan_unknown_limit_down_without_daily() -> None:
    """日线未落盘 → 跌停状态留 None，不再假装"未受阻"。"""
    pos = {"ts_code": "000001", "entry_price": 10.0, "qty": 1000,
           "notional": 10_000.0, "status": "holding"}
    plan = {"entry_kind": "sealed",
            "window": {"date": "20261125", "intraday": True}}
    dec = {"action": "sell", "price": 9.5, "kind": "fallback", "reason": "兜底"}
    out = live_sim._close_by_plan(pos, plan, dec, [])
    assert out["break_is_limit_down"] is None
    assert out["exit_blocked"] is False
    assert "不可判定" in out["exit_blocked_note"]
    assert out["exit_env"]["rule"] == "sealed_intraday_sell"


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


# ---------------------------------------------------------------------------
# 条件 C：昨日涨停溢价（2026-11-23 用户裁定：用真实日线算回来）
# ---------------------------------------------------------------------------

def test_emotion_gate_premium_is_computed_from_real_panel() -> None:
    """条件 C 不再是恒 None：昨日涨停股今日平均涨幅必须算出来。"""
    g = live_sim.emotion_gate("20261118")
    assert g.get("available") is True
    assert g.get("premium") is not None, "条件 C 溢价必须由真实日线算出"
    assert isinstance(g["premium"], (int, float))
    assert g.get("n_premium", 0) > 0


def test_emotion_gate_premium_matches_manual_recompute() -> None:
    """抽样核对：溢价 = 昨日涨停股今日涨跌幅的算术平均。"""
    from backend import feature_engine
    import polars as pl

    day, prev = "2026-11-18", "2026-11-17"
    p = live_sim.feeds.build_panel() if hasattr(live_sim, "feeds") else None
    if p is None:
        from backend import feeds
        p = feeds.build_panel()
    vals = []
    for (code,), grp in p.group_by("ts_code", maintain_order=True):
        ds = [str(x) for x in grp["trade_date"].to_list()]
        if prev not in ds or day not in ds:
            continue
        jp, jt = ds.index(prev), ds.index(day)
        if jt < 1:
            continue
        c = grp["close"].to_numpy().astype(float)
        _, st = feature_engine.zt_flags(code, c)
        if int(st[jp]) < 1:
            continue
        vals.append(float(c[jt] / c[jt - 1] - 1.0) * 100.0)
    assert vals, "样本为空，无法核对"
    g = live_sim.emotion_gate("20261118")
    assert g["premium"] == pytest.approx(sum(vals) / len(vals), abs=0.01)
