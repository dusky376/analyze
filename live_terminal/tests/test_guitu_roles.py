"""龟兔角色识别 · 真实数据 + 边界测试（2026-11-13 用户裁定）

> 悟空·二板的核心就是二板，乌龟和兔子

重点守住三条：① 角色是标注不是过滤；② 一字兔子按 Q037 买不进；
③ 缺字段不猜、不同题材不硬凑配对。
"""
from __future__ import annotations

from backend import guitu_cases, guitu_roles, live_sim

# 2026-11-13 13:18 真实留档帧（data/live/20261113/pools/pools.jsonl 尾帧）
REAL_ROWS = [
    {"ts_code": "001358", "name": "兴欣新材", "price": 26.64, "pct": 9.9917,
     "streak": 2, "industry": "化学制品", "turnover": 2.7519,
     "seal_fund": 514846931, "broken": 0,
     "first_seal": 92500, "last_seal": 92500, "trade_date": "20261113"},
    {"ts_code": "002187", "name": "广百股份", "price": 6.97, "pct": 9.9369,
     "streak": 2, "industry": "一般零售", "turnover": 15.4081,
     "seal_fund": 40708285, "broken": 5,
     "first_seal": 92500, "last_seal": 94206, "trade_date": "20261113"},
    {"ts_code": "600467", "name": "好当家", "price": 2.73, "pct": 10.0806,
     "streak": 2, "industry": "渔业", "turnover": 17.5283,
     "seal_fund": 74933040, "broken": 5,
     "first_seal": 92502, "last_seal": 101529, "trade_date": "20261113"},
    {"ts_code": "002205", "name": "国统股份", "price": 12.93, "pct": 10.0426,
     "streak": 2, "industry": "水泥", "turnover": 12.1045,
     "seal_fund": 22952560, "broken": 2,
     "first_seal": 93654, "last_seal": 101912, "trade_date": "20261113"},
]


def test_role_vocabulary_matches_casebook() -> None:
    """两处角色中文名不得分叉（案例库是用户手工标注，实时账本是自动代理）。"""
    for k in ("rabbit", "turtle", "heavy"):
        assert guitu_roles.ROLE_CN[k] == guitu_cases.ROLE_CN[k]


def test_real_frame_roles() -> None:
    got = {r["ts_code"]: guitu_roles.classify_role(r) for r in REAL_ROWS}
    assert got["001358"]["role"] == "rabbit"       # 09:25 一字、0 开板、换手 2.75%
    assert got["001358"]["role_cn"] == "一字兔子"
    for code in ("002187", "600467", "002205"):
        assert got[code]["role"] == "turtle", code
    # 判定依据不得含糊，且必须声明是代理
    for v in got.values():
        assert v["reason"] and v["proxy_note"] and v["is_filter"] is False
        assert v["role_basis"].startswith("当日全帧累计")


def test_missing_fields_never_guessed() -> None:
    assert guitu_roles.classify_role({})["role"] == "unknown"
    assert guitu_roles.classify_role({"broken": None, "first_seal": None,
                                      "turnover": None})["role"] == "unknown"
    # 只有换手、无开板次数与首封：不足以区分一字/厂字
    assert guitu_roles.classify_role({"turnover": 3.0})["role"] == "unknown"
    # heavy 绝不自动推断（无流通市值）
    assert all(guitu_roles.classify_role(r)["role"] != "heavy" for r in REAL_ROWS)
    assert guitu_roles.HEAVY_NOTE


def test_hhmmss_formats() -> None:
    for v in (92500, "92500", "09:25:00", "925"):
        row = {**REAL_ROWS[0], "first_seal": v}
        assert guitu_roles.classify_role(row)["role"] == "rabbit", v


def test_old_frame_without_first_seal_uses_real_last_seal() -> None:
    """老留档帧无 first_seal 列：末封 09:25 + 0 开板仍是真实的一字证据。"""
    row = {**REAL_ROWS[0], "first_seal": None, "last_seal": 92500}
    g = guitu_roles.classify_role(row)
    assert g["role"] == "rabbit"
    assert "last_seal" in g["evidence"]["seal_source"]
    # 末封在盘中则如实判换手/回封，不误判兔子
    late = {**REAL_ROWS[0], "first_seal": None, "last_seal": 101529,
            "broken": 0, "turnover": 3.0}
    assert guitu_roles.classify_role(late)["role"] == "turtle"


def test_mixed_old_new_frames_aggregate_seal_fields() -> None:
    """同一天里老 top-30 帧 + 新全量帧，封板时刻必须跨帧聚合。"""
    old_cols = {k: v for k, v in REAL_ROWS[0].items()
                if k not in ("first_seal", "last_seal")}
    frames = [
        {"ts": "2026-11-13T10:00:00",
         "zt": {"n": 1, "top": [old_cols], "all_complete": False}},
        {"ts": "2026-11-13T13:18:12",
         "zt": {"n": 1, "top": [], "all": [REAL_ROWS[0]], "all_complete": True}},
    ]
    groups, notes = live_sim.collect_two_board(frames)
    assert notes and "截断" in notes[0]
    g = groups["001358"]
    assert g["first_seal"] == 92500 and g["last_seal"] == 92500
    assert g["n_frames"] == 2
    o = live_sim.build_order("20261113", "001358", g)
    assert o["guitu"]["role"] == "rabbit"
    assert o["guitu"]["evidence"]["seal_source"].startswith("first_seal")


def test_late_first_seal_is_turtle_even_without_broken() -> None:
    row = {**REAL_ROWS[0], "first_seal": 103000, "broken": 0, "turnover": 3.0}
    g = guitu_roles.classify_role(row)
    assert g["role"] == "turtle" and "晚于 09:30" in g["reason"]


def test_role_is_not_a_filter_in_build_order() -> None:
    """角色标注不得改变 Q037 成交判定：一字兔子仍买不进，乌龟仍可成交。"""
    frames = [{"ts": "2026-11-13T13:18:12",
               "zt": {"n": len(REAL_ROWS), "all": REAL_ROWS, "all_complete": True}}]
    groups, notes = live_sim.collect_two_board(frames)
    assert notes == []
    assert len(groups) == 4
    for code, g in groups.items():
        o = live_sim.build_order("20261113", code, g)
        assert o["guitu"]["role"] in ("rabbit", "turtle")
        assert o["queue"]["filled"] is (True if code != "001358" else False)
        assert o["guitu"]["is_filter"] is False
    assert groups["001358"]["n_frames"] == 1


def test_pair_groups_no_forced_matching() -> None:
    """今日 4 只二板行业各不相同 → 0 组真配对，不硬凑。"""
    orders = []
    for r in REAL_ROWS:
        g = guitu_roles.classify_role(r)
        orders.append({"day": "20261113", "industry": r["industry"],
                       "ts_code": r["ts_code"], "name": r["name"],
                       "status": "queued_unfilled" if g["role"] == "rabbit" else "filled",
                       "guitu": g})
    out = guitu_roles.pair_groups(orders)
    assert out["n_pairs"] == 0
    assert out["n_unpaired_groups"] == 4
    assert all("未同时出现" in x["unpaired_reason"] for x in out["unpaired"])


def test_pair_groups_true_pair_and_missing_industry() -> None:
    rabbit = {**REAL_ROWS[0], "industry": "机器人"}
    turtle = {**REAL_ROWS[1], "industry": "机器人"}
    orphan = {**REAL_ROWS[2], "industry": ""}
    orders = []
    for r, st in ((rabbit, "queued_unfilled"), (turtle, "filled"), (orphan, "filled")):
        g = guitu_roles.classify_role(r)
        orders.append({"day": "20261113", "industry": r["industry"],
                       "ts_code": r["ts_code"], "name": r["name"],
                       "status": st, "guitu": g})
    out = guitu_roles.pair_groups(orders)
    assert out["n_pairs"] == 1
    p = out["pairs"][0]
    assert p["industry"] == "机器人" and len(p["rabbits"]) == 1 and len(p["turtles"]) == 1
    # 2026-11-24：分组口径从 industry 改为 guitu_theme（题材优先），文案同步
    assert any(("题材字段缺失" in x["unpaired_reason"]
                or "行业字段缺失" in x["unpaired_reason"])
               for x in out["unpaired"])


def test_summarize_roles_is_descriptive() -> None:
    orders = []
    for r in REAL_ROWS:
        g = guitu_roles.classify_role(r)
        orders.append({"guitu": g, "status": "queued_unfilled" if g["role"] == "rabbit" else "filled"})
    s = guitu_roles.summarize_roles(orders, [])
    assert s["rabbit"]["n_orders"] == 1 and s["rabbit"]["n_filled"] == 0
    assert s["turtle"]["n_orders"] == 3 and s["turtle"]["n_filled"] == 3
    assert s["rabbit"]["win_rate"] is None       # 0 笔结算不编造胜率
    assert "机械结果" in s["turtle"]["note"]


def test_core_definition_enforces_no_new_filter() -> None:
    c = guitu_roles.CORE_DEFINITION
    assert c["core"] == "二板 + 乌龟和兔子"
    assert c["board"].startswith("只收 streak 1→2")
    assert "没有" in c["no_new_filter"] and "待检验假设" in c["research_status"]
    # 输出到页面/周报的数据字段应是纯文本，不能带 Markdown 星号
    for key in ("no_new_filter", "research_status", "execution"):
        assert "**" not in c[key]
    assert "**" not in guitu_roles.PROXY_NOTE
    assert "**" not in guitu_roles.pair_groups([])["note"]
