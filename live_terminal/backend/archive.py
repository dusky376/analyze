"""逐日留档的读写抽象层（支持压缩归档，对调用方透明）。

为什么要这一层
--------------
`data/live/<day>/` 里有三类落盘：

    pools/pools.jsonl        涨停池逐帧 JSONL（每帧含全量 all + 冗余 top）
    quotes/quotes_HHMMSS.parquet   全市场行情快照，每 5 分钟一帧
    auction/auction_HHMMSS.parquet 集合竞价快照，每 15 秒一帧

原始形态体积很大：一个交易日的 pools 可到 9 MB、quotes 到 11 MB。
历史日不再需要逐帧独立文件，可以合并/压缩归档；但**盘中必须继续逐帧写**
（recorder 依赖帧序，读取方要「最新帧」）。

因此本模块统一两种形态，调用方不用关心：

    逐帧形态（盘中）    quotes/quotes_*.parquet、pools/pools.jsonl
    归档形态（盘后）    quotes/frames.parquet（含 frame 列）、pools/pools.jsonl.gz

读取优先级：**逐帧形态优先**（盘中更新），没有才用归档形态。

压缩收益（实测 6 个交易日）
--------------------------
    pools.jsonl  → gzip(L6)      33.57 MB → 2.68 MB  (8%)
    quotes 每帧  → 单日合并 zstd9 62.98 MB → 22.52 MB (36%)
    auction 每帧 → 单日合并 zstd9 18.69 MB → 3.62 MB  (19%)
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import polars as pl

from backend import paths

#: 归档形态文件名
POOLS_GZ = "pools.jsonl.gz"
QUOTES_MERGED = "frames.parquet"
AUCTION_MERGED = "frames.parquet"

#: 合并归档里用于区分帧的列名（值为 HHMMSS 字符串）
FRAME_COL = "frame"


# ---------------------------------------------------------------------------
# 涨停池 JSONL
# ---------------------------------------------------------------------------

def pools_plain_path(day: str, root: Path | None = None) -> Path:
    """盘中逐帧追加的明文 JSONL 路径。

    Args:
        root: 可选的留档根（默认 ``paths.live_dir()``）；测试用 monkeypatch
              把模块级 LIVE 指到临时目录时，可显式传入保持隔离。
    """
    base = root if root is not None else paths.live_dir()
    return base / str(day) / "pools" / "pools.jsonl"


def pools_gz_path(day: str, root: Path | None = None) -> Path:
    """盘后压缩归档路径。"""
    base = root if root is not None else paths.live_dir()
    return base / str(day) / "pools" / POOLS_GZ


def pools_path(day: str, root: Path | None = None) -> Path | None:
    """当前可读的涨停池文件（明文优先，其次 gz）；都没有返回 None。"""
    for p in (pools_plain_path(day, root), pools_gz_path(day, root)):
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def read_pool_lines(day: str, root: Path | None = None) -> list[str]:
    """读涨停池的原始行（不解析 JSON），明文/gz 都支持。

    **两种形态同时存在时按时间序拼接**：归档的 ``.gz``（当日较早帧）在前，
    盘中追加的明文 ``.jsonl``（当日较新帧）在后。

    为什么需要拼接：若一个交易日在盘中先被压缩归档（例如重启前手动归档），
    之后 recorder 继续追加明文帧，只看其中一种就会丢帧——那会让"当日帧数"
    与选股/成交判定都失真。拼接保证帧序完整。

    读取方需要「最后一帧」时可用 ``read_pool_lines(day)[-1]``。
    """
    gz = pools_gz_path(day, root)
    plain = pools_plain_path(day, root)
    lines: list[str] = []
    if gz.is_file() and gz.stat().st_size > 0:
        try:
            with gzip.open(gz, "rt", encoding="utf-8") as f:
                lines.extend(ln for ln in f.read().splitlines() if ln.strip())
        except OSError:
            pass
    if plain.is_file() and plain.stat().st_size > 0:
        try:
            lines.extend(ln for ln in plain.read_text(encoding="utf-8").splitlines()
                         if ln.strip())
        except OSError:
            pass
    return lines


def read_pool_frames(day: str, root: Path | None = None) -> list[dict[str, Any]]:
    """读涨停池全部帧（已解析）。坏行跳过，不抛错。"""
    out: list[dict[str, Any]] = []
    for line in read_pool_lines(day, root):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# quotes / auction 逐帧 parquet
# ---------------------------------------------------------------------------

def _frame_files(day: str, kind: str, prefix: str) -> list[Path]:
    d = paths.live_dir() / str(day) / kind
    if not d.is_dir():
        return []
    return sorted(d.glob(f"{prefix}_*.parquet"))


def quote_frame_files(day: str) -> list[Path]:
    """逐帧行情文件（盘中）；已归档的日期返回空列表。"""
    return _frame_files(day, "quotes", "quotes")


def auction_frame_files(day: str) -> list[Path]:
    return _frame_files(day, "auction", "auction")


def merged_path(day: str, kind: str) -> Path:
    return paths.live_dir() / str(day) / kind / (
        QUOTES_MERGED if kind == "quotes" else AUCTION_MERGED)


def read_merged(day: str, kind: str) -> pl.DataFrame | None:
    """读合并归档表（含 FRAME_COL 列）；不存在返回 None。"""
    p = merged_path(day, kind)
    if not p.is_file():
        return None
    try:
        return pl.read_parquet(p)
    except Exception:  # noqa: BLE001
        return None


def _frame_label(path: Path, prefix: str) -> str | None:
    """quotes_092622.parquet → '092622'；quotes_20261124092622 → '092622'。"""
    stem = path.stem.replace(f"{prefix}_", "")
    if len(stem) == 6 and stem.isdigit():
        return stem
    if len(stem) >= 14 and stem.isdigit():
        return stem[8:14]
    return None


def read_frames(day: str, kind: str) -> pl.DataFrame | None:
    """整日所有帧（逐帧优先，否则合并归档）。

    Returns:
        含 ``FRAME_COL``（HHMMSS）的长表；无数据返回 None。
    """
    prefix = "quotes" if kind == "quotes" else "auction"
    files = _frame_files(day, kind, prefix)
    if files:
        dfs = []
        for f in files:
            label = _frame_label(f, prefix)
            if label is None:
                continue
            try:
                df = pl.read_parquet(f)
            except Exception:  # noqa: BLE001
                continue
            dfs.append(df.with_columns(pl.lit(label).alias(FRAME_COL)))
        if dfs:
            return pl.concat(dfs, how="diagonal_relaxed")
        return None
    return read_merged(day, kind)


def latest_frame(day: str, kind: str) -> pl.DataFrame | None:
    """当日**最新一帧**行情（逐帧优先，否则合并归档取最大 frame）。"""
    prefix = "quotes" if kind == "quotes" else "auction"
    files = _frame_files(day, kind, prefix)
    if files:
        try:
            return pl.read_parquet(files[-1])
        except Exception:  # noqa: BLE001
            return None
    merged = read_merged(day, kind)
    if merged is None or merged.height == 0:
        return None
    if FRAME_COL not in merged.columns:
        return merged
    last = merged[FRAME_COL].max()
    return merged.filter(pl.col(FRAME_COL) == last).drop(FRAME_COL)


def latest_frame_label(day: str, kind: str) -> str | None:
    """最新帧的时刻标签（HHMMSS）；用于 as_of 展示。"""
    prefix = "quotes" if kind == "quotes" else "auction"
    files = _frame_files(day, kind, prefix)
    if files:
        return _frame_label(files[-1], prefix)
    merged = read_merged(day, kind)
    if merged is None or merged.height == 0 or FRAME_COL not in merged.columns:
        return None
    return str(merged[FRAME_COL].max())


# ---------------------------------------------------------------------------
# 归档状态判断（供 data_health / 工具使用）
# ---------------------------------------------------------------------------

def is_compacted(day: str) -> dict[str, bool]:
    """该日是否已归档（pools 用 gz、quotes/auction 用合并文件）。"""
    return {
        "pools": pools_gz_path(day).is_file() and not pools_plain_path(day).is_file(),
        "quotes": merged_path(day, "quotes").is_file() and not quote_frame_files(day),
        "auction": (merged_path(day, "auction").is_file()
                    and not auction_frame_files(day)),
    }
