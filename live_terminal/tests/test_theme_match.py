# -*- coding: utf-8 -*-
"""题材多标签匹配层的契约测试（多标签交集 + 三级匹配级别）。

与模块约定对齐（以 backend/theme_match.py 为准）：
  * ``map_to_axes`` 返回板块 key（英文），按标签出现顺序保序去重
  * ``group_by_theme`` 的组名 ``theme`` 是**中文展示名**（如「算力」）
  * ``coverage_stats`` 的 pct 是 **0~100 百分数**（1 位小数）
  * ``common_themes`` level: axis > tag > industry（industry 是弱匹配，
    reason 必带「非严格题材」警示；是否采用由调用方决定）

用户 2026-11-23 裁定：同题材/同板块 = 同一个概念；行业兜底不作硬凑依据。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import theme_match as TM  # noqa: E402


# ---------------------------------------------------------------------------
# map_to_axes
# ---------------------------------------------------------------------------

def test_map_to_axes_single_and_multi():
    assert TM.map_to_axes(["算力"]) == ["compute"]
    got = TM.map_to_axes(["液冷算力", "华为算力", "多媒体视讯"])
    assert got == ["compute"]
    # 多板块命中：'AI传媒'+'算力' 两个轴，去重保序
    got2 = TM.map_to_axes(["AI传媒", "算力", "AI传媒"])
    assert "ai_media" in got2 and "compute" in got2
    assert len(got2) == len(set(got2))


def test_map_to_axes_unmapped_returns_empty():
    assert TM.map_to_axes(["蹦床"]) == []
    assert TM.map_to_axes([]) == []
    assert TM.map_to_axes([None, "  ", ""]) == []


# ---------------------------------------------------------------------------
# common_themes：axis > tag > industry（industry 需调用方决定是否采用）
# ---------------------------------------------------------------------------

def test_common_themes_axis_level():
    r = TM.common_themes(["液冷算力"], ["算力", "LED显示控制"])
    assert r["matched"] is True
    assert r["level"] == "axis"
    assert r["shared"] == ["算力"]   # shared 用中文板块名


def test_common_themes_tag_level_when_axis_misses():
    r = TM.common_themes(["蹦床"], ["蹦床", "健身器材"])
    assert r["matched"] is True
    assert r["level"] == "tag"
    assert "蹦床" in (r["shared"] or [])


def test_common_themes_industry_level_flagged_weak():
    """industry 级存在，但 reason 必须带「非严格题材」警示，由调用方决定采用。"""
    r = TM.common_themes(["氦气开采"], ["精酿啤酒"], industry_a="环保设备",
                         industry_b="环保设备")
    assert r["level"] == "industry"
    assert r["matched"] is True
    assert "非严格题材" in r["reason"]
    assert "不要当题材用" in r["reason"]


def test_common_themes_unrelated():
    r = TM.common_themes(["蹦床"], ["光刻胶"], industry_a="食品",
                         industry_b="半导体")
    assert r["matched"] is False
    assert r["level"] is None


def test_common_themes_axis_beats_tag():
    r = TM.common_themes(["算力", "蹦床"], ["算力", "蹦床"])
    assert r["matched"] is True
    assert r["level"] == "axis"


def test_common_themes_empty_tags_never_industry():
    """无标签票即使行业相同也不配（模块 docstring 有测试锁）。"""
    r = TM.common_themes([], [], industry_a="IT", industry_b="IT")
    assert r["matched"] is False
    assert r["level"] is None


# ---------------------------------------------------------------------------
# group_by_theme
# ---------------------------------------------------------------------------

def _item(code, tags, industry="测试行业", streak=2):
    return {"code": code, "name": code, "tags": tags,
            "industry": industry, "streak": streak}


def test_group_by_theme_multi_label_membership():
    """多标签：一只票可同时出现在多个板块的组里（旧单板块口径丢的东西）。"""
    items = [_item("000001", ["算力", "AI传媒"]),
             _item("000002", ["算力"]),
             _item("000003", ["AI传媒"])]
    out = TM.group_by_theme(items)
    by_theme = {g["theme"]: set(g["codes"]) for g in out["groups"]}
    assert by_theme.get("算力") == {"000001", "000002"}
    assert by_theme.get("AI传媒") == {"000001", "000003"}


def test_group_by_theme_unlabeled_not_forced():
    """无标签票不硬塞进组（用户裁定：不硬凑）。"""
    items = [_item("000001", ["算力"]), _item("000002", [])]
    out = TM.group_by_theme(items)
    all_codes = {c for g in out["groups"] for c in g["codes"]}
    assert "000002" not in all_codes
    assert "000002" in [u["code"] for u in (out["unlabeled"] or [])]


def test_group_by_theme_tag_level_grouping():
    """轴未收录的共享 T 标签会以 tag 级建组（如 蹦床）。"""
    items = [_item("000001", ["蹦床"]), _item("000002", ["蹦床", "健身器材"])]
    out = TM.group_by_theme(items)
    tag_group = next((g for g in out["groups"]
                      if g["theme"] == "蹦床" and g["level"] == "tag"), None)
    assert tag_group is not None
    assert set(tag_group["codes"]) == {"000001", "000002"}


# ---------------------------------------------------------------------------
# coverage_stats
# ---------------------------------------------------------------------------

def test_coverage_stats_self_consistent():
    items = [_item("000001", ["算力"]),
             _item("000002", ["蹦床"]),      # 有标签但轴未收录
             _item("000003", []),            # 无标签
             _item("000004", ["AI传媒"])]
    s = TM.coverage_stats(items)
    assert s["n"] == 4
    assert s["n_tagged"] == 3
    assert s["n_axis"] == 2
    assert s["tagged_pct"] == pytest.approx(75.0)     # 0~100 百分数
    assert s["axis_pct"] == pytest.approx(50.0)
    assert "000003" in [x["code"] for x in s["unlabeled"]]
    assert "000002" in [x["code"] for x in s["unmapped"]]


# ---------------------------------------------------------------------------
# 真实数据验证（依赖本地 parquet，缺失则跳过）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_items():
    try:
        items, _ = TM._items_from_parquet(None)
    except Exception:  # noqa: BLE001
        pytest.skip("本地无 themes.parquet")
    if not items:
        pytest.skip("parquet 为空")
    return items


#: 覆盖率标定用「已定格的历史日」，不用「最新一天」。
#: 2026-11-24 起题材池改为盘中实时采集，最新一天可能是只含部分涨停的盘中快照
#: （09:53 实测 53 行，收盘 100+ 行），拿它标定轴覆盖率会把「样本不全」
#: 误判成「扩轴失效」。标定只关心轴映射，固定一个完整交易日即可。
CALIB_DAY = "2026-11-18"


@pytest.fixture(scope="module")
def calib_items():
    try:
        items, _ = TM._items_from_parquet(CALIB_DAY)
    except Exception:  # noqa: BLE001
        pytest.skip("本地无 themes.parquet")
    if not items:
        pytest.skip(f"parquet 无 {CALIB_DAY}")
    return items


def test_real_coverage_improved(calib_items):
    """扩轴后股票层覆盖率必须显著高于旧轴的 53.2%。"""
    s = TM.coverage_stats(calib_items)
    assert s["axis_pct"] >= 65.0, f"扩轴后股票层覆盖率仅 {s['axis_pct']}%，低于预期"
    print(f"\n[real] day={CALIB_DAY} n={s['n']} "
          f"tagged={s['tagged_pct']}% axis={s['axis_pct']}%")


def test_real_grouping_finds_compute_pair(real_items):
    """11-18 的算力题材必须能凑出「一字兔+其他二板」的组。"""
    day = [it for it in real_items if it.get("date") == "2026-11-18"]
    if not day:
        pytest.skip("无 2026-11-18 数据")
    out = TM.group_by_theme(day)
    comp = next((g for g in out["groups"] if g["theme"] == "算力"), None)
    assert comp is not None, "算力组应存在"
    codes = set(comp["codes"])
    # 盛剑科技/全筑股份（兔子）与 真视通/华胜天成/卡莱特/双环（乌龟）都该在同组
    assert {"603324", "603030", "002771", "600410", "301391"} & codes
