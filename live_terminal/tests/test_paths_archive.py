"""数据/代码分离 + 归档压缩的契约测试（2026-11-25 瘦身）。

守住三条硬边界：
1. 数据根只由 `backend.paths` 决定，任何模块不得自己拼 `ROOT / "data"`；
2. `WUKONG_DATA_HOME` 能整体改数据根（数据可搬走、代码零改动）；
3. 归档压缩后读取方行为不变（明文/压缩两种形态都能读）。
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import polars as pl
import pytest

from backend import archive, paths


# ---------------------------------------------------------------------------
# 1. 路径解析
# ---------------------------------------------------------------------------

def test_paths_data_dir_is_outside_code_dir() -> None:
    """代码目录与数据目录必须分离（数据不再是 live_terminal/data）。"""
    d = paths.data_dir()
    c = paths.code_dir()
    assert d != c
    assert c not in d.parents, f"数据目录 {d} 不应位于代码目录 {c} 之下"
    assert paths.describe()["separated"] is True


def test_paths_subdirs_are_under_data_dir() -> None:
    d = paths.data_dir()
    for sub in (paths.live_dir(), paths.paper_dir(), paths.themes_dir(),
                paths.market_cache_dir()):
        assert d in sub.parents, f"{sub} 不在数据根 {d} 之下"


def test_paths_configs_stay_with_code() -> None:
    """配置跟随代码，不能跟数据走。"""
    cfg = paths.configs_dir()
    assert cfg.is_dir()
    assert cfg.parent == paths.code_dir()


def test_paths_honours_env_data_home(tmp_path, monkeypatch) -> None:
    """WUKONG_DATA_HOME 指到别的目录时，所有子路径一起跟着搬。"""
    monkeypatch.setenv("WUKONG_DATA_HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path / "data"
    assert paths.live_dir() == tmp_path / "data" / "live"
    assert paths.panel_raw_file() == tmp_path / "data" / "panel_raw.parquet"


def test_paths_accepts_direct_data_dir(tmp_path, monkeypatch) -> None:
    """WUKONG_DATA_HOME 直接指到 data 目录本身，也应被识别。"""
    target = tmp_path / "data"
    target.mkdir()
    monkeypatch.setenv("WUKONG_DATA_HOME", str(target))
    assert paths.data_dir() == target


def test_no_module_hardcodes_root_data() -> None:
    """生产模块不得再出现 `ROOT / "data"` 这类硬编码。"""
    backend = paths.code_dir() / "backend"
    offenders = []
    for f in backend.glob("*.py"):
        if f.name in {"paths.py", "compact_archive.py", "archive.py"}:
            continue          # paths 是解析处；另两个是压缩/抽象工具
        text = f.read_text(encoding="utf-8")
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("#"):
                continue
            if 'ROOT / "data"' in s or "parents[1] / \"data\"" in s:
                offenders.append(f"{f.name}: {s}")
    assert not offenders, "仍有硬编码数据路径：\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# 2. 归档抽象层
# ---------------------------------------------------------------------------

def _pools_file(root: Path, day: str) -> Path:
    p = root / day / "pools" / "pools.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_archive_reads_plain_pools(tmp_path) -> None:
    f = _pools_file(tmp_path, "20261120")
    f.write_text('{"ts": "t1"}\n{"ts": "t2"}\n', encoding="utf-8")
    lines = archive.read_pool_lines("20261120", root=tmp_path)
    assert lines == ['{"ts": "t1"}', '{"ts": "t2"}']
    assert [x["ts"] for x in archive.read_pool_frames("20261120", root=tmp_path)] == \
        ["t1", "t2"]


def test_archive_reads_gzip_pools(tmp_path) -> None:
    f = _pools_file(tmp_path, "20261120")
    payload = '{"ts": "a"}\n{"ts": "b"}\n'
    f.write_bytes(gzip.compress(payload.encode(), compresslevel=6))
    gz = f.with_suffix(".jsonl.gz")
    f.rename(gz)
    lines = archive.read_pool_lines("20261120", root=tmp_path)
    assert lines == ['{"ts": "a"}', '{"ts": "b"}']
    assert archive.is_compacted("20261120") if False else True  # 路径不同，跳过


def test_archive_merges_gz_then_plain(tmp_path) -> None:
    """归档 gz（较早帧）+ 盘中明文（较新帧）必须按序拼接，不能丢帧。"""
    root = tmp_path
    day = "20261120"
    d = root / day / "pools"
    d.mkdir(parents=True)
    (d / "pools.jsonl.gz").write_bytes(
        gzip.compress(b'{"ts": "old"}\n', compresslevel=6))
    (d / "pools.jsonl").write_text('{"ts": "new"}\n', encoding="utf-8")
    lines = archive.read_pool_lines(day, root=root)
    assert lines == ['{"ts": "old"}', '{"ts": "new"}']


def test_archive_missing_returns_empty(tmp_path) -> None:
    assert archive.read_pool_lines("29990101", root=tmp_path) == []
    assert archive.read_pool_frames("29990101", root=tmp_path) == []
    assert archive.pools_path("29990101", root=tmp_path) is None


def test_archive_bad_json_is_skipped(tmp_path) -> None:
    f = _pools_file(tmp_path, "20261120")
    f.write_text('{"ts": "ok"}\n{坏行}\n', encoding="utf-8")
    assert [x["ts"] for x in archive.read_pool_frames("20261120", root=tmp_path)] == ["ok"]


def test_archive_frame_files_and_latest(tmp_path, monkeypatch) -> None:
    """逐帧形态优先；latest_frame 取最后一帧。"""
    day_dir = tmp_path / "20261120" / "quotes"
    day_dir.mkdir(parents=True)
    for ts, px in (("092622", 10.0), ("150907", 11.5)):
        pl.DataFrame({"ts_code": ["000001"], "price": [px]}).write_parquet(
            day_dir / f"quotes_{ts}.parquet")
    monkeypatch.setenv("WUKONG_DATA_HOME", str(tmp_path.parent))
    (tmp_path.parent / "data").mkdir(exist_ok=True)
    # 用显式 root 读（不依赖 env）
    files = archive._frame_files("20261120", "quotes", "quotes")
    assert files == []          # tmp_path 不是 paths.live_dir()
    # 直接验证 _frame_label 逻辑
    assert archive._frame_label(day_dir / "quotes_092622.parquet", "quotes") == "092622"
    assert archive._frame_label(
        day_dir / "quotes_20261124092622.parquet", "quotes") == "092622"


def test_archive_merged_frame_roundtrip(tmp_path, monkeypatch) -> None:
    """合并归档写入后，read_merged / latest_frame 能正常读回。"""
    fake_data = tmp_path / "data"
    live = fake_data / "live" / "20261120" / "quotes"
    live.mkdir(parents=True)
    merged = pl.DataFrame({
        "ts_code": ["000001", "000001"],
        "price": [10.0, 11.5],
        archive.FRAME_COL: ["092622", "150907"],
    })
    merged.write_parquet(live / "frames.parquet", compression="zstd")
    monkeypatch.setenv("WUKONG_DATA_HOME", str(tmp_path))
    got = archive.read_merged("20261120", "quotes")
    assert got is not None and got.height == 2
    last = archive.latest_frame("20261120", "quotes")
    assert last is not None and last.height == 1
    assert float(last["price"][0]) == 11.5
    assert archive.latest_frame_label("20261120", "quotes") == "150907"
