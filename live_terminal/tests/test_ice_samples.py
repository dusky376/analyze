# -*- coding: utf-8 -*-
"""冰点样本库自动更新（做法 B）的契约测试。

关键：所有写操作必须重定向到临时文件，**绝不能污染** data/ice_samples.json。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import ice_samples as IS  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """把样本库指向临时文件，保证测试零污染。"""
    p = tmp_path / "ice_samples.json"
    monkeypatch.setattr(IS, "STORE", p)
    return p


def _rec(ld=80, ds=0.9, dt=2.0, bh=3):
    return {"day": "2026-11-16", "down_share": ds, "limit_down_n": ld,
            "limit_up_n": 33, "dt_ratio": dt, "broken_hi_n": bh,
            "up": 1135, "down": 4344}


def _dims(s="L5", lv_at=None):
    return {"down_share": "L3", "limit_down_n": lv_at or "L5",
            "dt_ratio": "L5", "broken_hi_n": "L0"}


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def test_load_missing_returns_empty(store):
    d = IS.load()
    assert d["version"] == IS.STORE_VERSION
    assert d["auto_samples"] == {} and d["seeds"] == []
    assert d["drift_history"] == []


def test_save_and_load_roundtrip(store):
    d = IS.load()
    d["seeds"] = [{"date": "2026-09-02", "status": "anchor"}]
    IS.save(d)
    assert store.is_file()
    again = IS.load()
    assert again["seeds"][0]["date"] == "2026-09-02"
    assert again["updated_at"] is not None


def test_load_corrupt_degrades(store):
    store.write_text("{not json", encoding="utf-8")
    assert IS.load()["auto_samples"] == {}


# ---------------------------------------------------------------------------
# 命中路径（ice_type）
# ---------------------------------------------------------------------------

def test_ice_type_multi_dim():
    rec = _rec(ld=30)                    # 未达 40
    dims = {"down_share": "L5", "limit_down_n": "L3",
            "dt_ratio": "L5", "broken_hi_n": "L4"}
    assert IS.ice_type(dims, rec) == "multi_dim"


def test_ice_type_abs_limit_down():
    rec = _rec(ld=45)
    dims = {"down_share": "L0", "limit_down_n": "L0",
            "dt_ratio": "L0", "broken_hi_n": "L0"}
    assert IS.ice_type(dims, rec) == "abs_limit_down"


def test_ice_type_both():
    rec = _rec(ld=80)
    assert IS.ice_type(_dims(), rec) == "both"


def test_ice_type_none_when_not_ice():
    rec = _rec(ld=5, ds=0.2, dt=0.1, bh=0)
    dims = {"down_share": "L0", "limit_down_n": "L0",
            "dt_ratio": "L0", "broken_hi_n": "L0"}
    assert IS.ice_type(dims, rec) is None


# ---------------------------------------------------------------------------
# 记录：命中即新增（做法 B）
# ---------------------------------------------------------------------------

def test_record_ice_day_creates_auto_sample(store):
    d = IS.load()
    new = IS.record_day(d, day="2026-11-16", metrics=_rec(), dims=_dims(),
                        level="L5", is_ice=True, size_factor=0.3,
                        itype="both")
    assert new is True
    assert "2026-11-16" in d["auto_samples"]
    s = d["auto_samples"]["2026-11-16"]
    assert s["source"] == "auto"
    assert s["severity_level"] == "L5"
    assert s["ice_type"] == "both"
    assert s["size_factor"] == pytest.approx(0.3)
    assert s["metrics"]["limit_down_n"] == 80
    assert s["detected_at"]
    # history 也必有记录
    assert "2026-11-16" in d["history"]


def test_record_non_ice_day_only_history(store):
    d = IS.load()
    new = IS.record_day(d, day="2026-11-23", metrics=_rec(ld=7, ds=0.14, dt=0.05, bh=0),
                        dims={"down_share": "L0", "limit_down_n": "L0",
                              "dt_ratio": "L0", "broken_hi_n": "L0"},
                        level="L0", is_ice=False, size_factor=1.0, itype=None)
    assert new is False
    assert d["auto_samples"] == {}
    assert "2026-11-23" in d["history"]


def test_record_is_idempotent(store):
    d = IS.load()
    assert IS.record_day(d, day="2026-11-16", metrics=_rec(), dims=_dims(),
                         level="L5", is_ice=True, size_factor=0.3,
                         itype="both") is True
    # 重复写同一天：更新但不重复新增
    assert IS.record_day(d, day="2026-11-16", metrics=_rec(), dims=_dims(),
                         level="L5", is_ice=True, size_factor=0.3,
                         itype="both") is False
    assert len(d["auto_samples"]) == 1


# ---------------------------------------------------------------------------
# 校准与漂移
# ---------------------------------------------------------------------------

def test_calibrate_rows_counts_sources(store):
    d = IS.load()
    d["seeds"] = [{"date": "2026-11-16", "status": "anchor"},
                  {"date": "2026-08-10", "status": "excluded_mislabeled"}]
    IS.record_day(d, day="2026-11-16", metrics=_rec(), dims=_dims(),
                  level="L5", is_ice=True, size_factor=0.3, itype="both")
    IS.record_day(d, day="2026-11-23", metrics=_rec(ld=7), dims=_dims(s="L0", lv_at="L0"),
                  level="L0", is_ice=False, size_factor=1.0, itype=None)
    rows, counted = IS.calibrate_rows(d)
    assert len(rows) == 2
    assert counted["anchor"] == 1        # 11-16 是 anchor
    assert counted["auto"] == 1          # 11-23 是 auto


def test_drift_metrics(store):
    d = IS.load()
    d["seeds"] = [{"date": "2026-09-02", "status": "anchor"},
                  {"date": "2026-09-11", "status": "anchor"},
                  {"date": "2026-08-10", "status": "excluded_mislabeled"}]
    for day in ("2026-09-02", "2026-10-22"):
        IS.record_day(d, day=day, metrics=_rec(), dims=_dims(),
                      level="L5", is_ice=True, size_factor=0.3, itype="both")
    dr = IS.drift(d)
    assert dr["anchor_n"] == 2            # 只算 status=anchor
    assert dr["auto_n"] == 2
    assert dr["overlap_n"] == 1           # 09-02 重合
    assert dr["anchor_hit_rate"] == pytest.approx(0.5)
    assert dr["overlap_dates"] == ["2026-09-02"]


def test_drift_no_anchor_returns_none(store):
    d = IS.load()
    assert IS.drift(d)["anchor_hit_rate"] is None


# ---------------------------------------------------------------------------
# 并发与一致性（2026-11-23 审计 P0-4 / P0-5 回归）
# ---------------------------------------------------------------------------

def test_concurrent_writes_do_not_lose_data(store):
    """并发 load→record→save 必须不丢数据、不抛 PermissionError。

    修复前实测：8 线程 × 15 轮 = 120 次写入只落盘 1 条 + 118 次 PermissionError。
    """
    import threading
    errors: list[str] = []

    def worker(i: int) -> None:
        for _ in range(10):
            try:
                with IS.locked_update() as st:
                    IS.record_day(st, day=f"2026-02-{i:02d}", metrics=_rec(),
                                  dims=_dims(), level="L5", is_ice=True,
                                  size_factor=0.3, itype="both")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(1, 7)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errors, f"并发写入报错：{errors[:3]}"
    final = IS.load()
    assert len(final["auto_samples"]) == 6, "6 天数据必须全部落盘"


def test_non_ice_revokes_previous_auto_sample(store):
    """P0-5：先判冰、后判非冰，必须撤销 auto 记录并留档 superseded。

    修复前：history 说非冰、auto_samples 说冰 → 样本库自相矛盾，
    且 drift() 仍算该日为算法命中，虚高人工锚点重合率。
    """
    d = IS.load()
    IS.record_day(d, day="2026-11-16", metrics=_rec(), dims=_dims(),
                  level="L5", is_ice=True, size_factor=0.3, itype="both")
    assert "2026-11-16" in d["auto_samples"]

    IS.record_day(d, day="2026-11-16", metrics=_rec(), dims=_dims(),
                  level="L0", is_ice=False, size_factor=1.0, itype=None)
    assert "2026-11-16" not in d["auto_samples"], "非冰点必须撤销 auto 记录"
    assert d["history"]["2026-11-16"]["is_ice"] is False
    assert d["history"]["2026-11-16"]["severity_level"] == "L0"
    sup = d.get("superseded_samples") or {}
    assert "2026-11-16" in sup and sup["2026-11-16"]["superseded"] is True


def test_corrupt_store_is_backed_up_not_silently_overwritten(store):
    """P0-9：坏文件必须先备份，避免下一次 save 覆盖真实样本库。"""
    store.write_text('{"seeds": [broken json', encoding="utf-8")
    d = IS.load()
    assert d["seeds"] == []                      # 降级为空结构
    backups = list(store.parent.glob("ice_samples.corrupt.*.json"))
    assert backups, "坏文件必须被备份，否则下一次 save 会覆盖真实库"


def test_locked_update_returns_saved_state(store):
    with IS.locked_update() as st:
        st["seeds"] = [{"date": "2026-09-02", "status": "anchor"}]
    again = IS.load()
    assert again["seeds"][0]["date"] == "2026-09-02"
    assert again["updated_at"] is not None


# ---------------------------------------------------------------------------
# 生产样本库完整性（只读断言，确保迁移没丢人工种子）
# ---------------------------------------------------------------------------

def test_production_store_keeps_user_anchors():
    """生产样本库必须保留用户 15 个 anchor 种子（迁移不可丢）。

    2026-11-25 数据/代码分离后，样本库位置由 `paths.data_dir()` 解析，
    这里不再硬编码 `live_terminal/data`。
    """
    from backend import paths
    store = paths.data_dir() / "ice_samples.json"
    if not store.is_file():
        pytest.skip(f"生产样本库不存在：{store}")
    real = json.loads(store.read_text(encoding="utf-8"))
    anchors = [s for s in real.get("seeds") or []
               if isinstance(s, dict) and s.get("status") == "anchor"]
    assert len(anchors) == 15, f"人工锚点应为 15 个，实际 {len(anchors)}"
    assert "2026-09-02" in {s["date"] for s in anchors}
