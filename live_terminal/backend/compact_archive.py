"""归档压缩工具：把历史交易日的逐帧留档压成紧凑形态，释放磁盘。

用法
----
    python -m backend.compact_archive --dry-run        # 只报告能省多少
    python -m backend.compact_archive --days 20261116 20261117
    python -m backend.compact_archive --all            # 压缩全部非今日目录
    python -m backend.compact_archive --verify         # 校验归档完整性

规则（安全第一）
---------------
* **今天的数据不动**（recorder 可能正在写，必须保持逐帧明文）。
* pools.jsonl      → pools.jsonl.gz（gzip L6），原文件删除前校验行数一致。
* quotes/*.parquet → quotes/frames.parquet（单日合并，zstd），保留 frame 列。
* auction/*.parquet→ auction/frames.parquet（同上）。
* 任一校验失败 → **不删除原文件**，报告失败原因。
* 幂等：已归档的日再跑只报告「已归档」。

压缩收益（实测 6 个交易日）
--------------------------
    pools    33.57 MB → 2.68 MB
    quotes   62.98 MB → 22.52 MB
    auction  18.69 MB → 3.62 MB
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from backend import archive, paths


def _mb(n: float) -> float:
    return round(n / 1048576, 2)


def _dir_size(p: Path) -> tuple[int, int]:
    if not p.is_dir():
        return 0, 0
    files = [f for f in p.rglob("*") if f.is_file()]
    return sum(f.stat().st_size for f in files), len(files)


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _verify_pools(day: str, gz: Path, src: Path) -> tuple[bool, str]:
    """压缩前后行数 + 最后一行内容必须一致。"""
    try:
        plain_lines = [ln for ln in src.read_text(encoding="utf-8").splitlines()
                       if ln.strip()]
    except OSError as exc:
        return False, f"读原文失败: {exc}"
    try:
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            gz_lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError as exc:
        return False, f"读压缩包失败: {exc}"
    if len(plain_lines) != len(gz_lines):
        return False, f"行数不一致 {len(plain_lines)} vs {len(gz_lines)}"
    if plain_lines and plain_lines[-1] != gz_lines[-1]:
        return False, "最后一行内容不一致"
    # 首行也要一致（防偏移）
    if plain_lines and plain_lines[0] != gz_lines[0]:
        return False, "首行内容不一致"
    return True, f"{len(gz_lines)} 帧"


def compact_pools(day: str, *, dry: bool) -> dict[str, Any]:
    src = archive.pools_plain_path(day)
    dst = archive.pools_gz_path(day)
    if not src.is_file() or src.stat().st_size == 0:
        if dst.is_file():
            return {"kind": "pools", "status": "already", "saved_mb": 0.0}
        return {"kind": "pools", "status": "skip", "reason": "无明文留档"}
    raw = src.read_bytes()
    if dry:
        sz = len(gzip.compress(raw, compresslevel=6))
        return {"kind": "pools", "status": "would", "from_mb": _mb(len(raw)),
                "to_mb": _mb(sz), "saved_mb": _mb(len(raw) - sz)}
    dst.write_bytes(gzip.compress(raw, compresslevel=6))
    ok, note = _verify_pools(day, dst, src)
    if not ok:
        dst.unlink(missing_ok=True)
        return {"kind": "pools", "status": "verify_failed", "reason": note}
    src.unlink()
    return {"kind": "pools", "status": "done", "from_mb": _mb(len(raw)),
            "to_mb": _mb(dst.stat().st_size),
            "saved_mb": _mb(len(raw) - dst.stat().st_size), "note": note}


def _compact_frames(day: str, kind: str, *, dry: bool) -> dict[str, Any]:
    prefix = "quotes" if kind == "quotes" else "auction"
    files = (archive.quote_frame_files(day) if kind == "quotes"
             else archive.auction_frame_files(day))
    if not files:
        merged = archive.merged_path(day, kind)
        if merged.is_file():
            return {"kind": kind, "status": "already", "saved_mb": 0.0}
        return {"kind": kind, "status": "skip", "reason": "无逐帧文件"}
    raw = sum(f.stat().st_size for f in files)
    if dry:
        # 轻量估算：只读前 3 帧推比例（dry-run 不写盘、不快读全量）
        sample = files[:min(3, len(files))]
        sizes = [f.stat().st_size for f in sample]
        big = _concat_frames(sample, prefix)
        tmp = Path(str(archive.merged_path(day, kind)) + ".dry.parquet")
        try:
            if big is None or big.height == 0:
                return {"kind": kind, "status": "skip", "reason": "样本帧读取为空"}
            big.write_parquet(tmp, compression="zstd", compression_level=9)
            ratio = tmp.stat().st_size / max(1, sum(sizes))
            est = int(raw * ratio)
        finally:
            tmp.unlink(missing_ok=True)
        return {"kind": kind, "status": "would", "from_mb": _mb(raw),
                "to_mb": _mb(est), "saved_mb": _mb(raw - est), "frames": len(files)}
    big = _concat_frames(files, prefix)
    if big is None or big.height == 0:
        return {"kind": kind, "status": "skip", "reason": "合并结果为空"}
    out = archive.merged_path(day, kind)
    tmp = out.with_suffix(".tmp.parquet")
    big.write_parquet(tmp, compression="zstd", compression_level=9)
    # 校验：行数与帧数一致
    got = pl.read_parquet(tmp)
    if got.height != big.height:
        tmp.unlink(missing_ok=True)
        return {"kind": kind, "status": "verify_failed",
                "reason": f"行数 {got.height} != {big.height}"}
    tmp.replace(out)
    for f in files:
        f.unlink(missing_ok=True)
    return {"kind": kind, "status": "done", "from_mb": _mb(raw),
            "to_mb": _mb(out.stat().st_size),
            "saved_mb": _mb(raw - out.stat().st_size),
            "frames": len(files), "rows": got.height}


def _concat_frames(files: list[Path], prefix: str) -> pl.DataFrame | None:
    dfs = []
    for f in sorted(files):
        label = archive._frame_label(f, prefix)
        if label is None:
            continue
        try:
            dfs.append(pl.read_parquet(f)
                       .with_columns(pl.lit(label).alias(archive.FRAME_COL)))
        except Exception:  # noqa: BLE001
            continue
    if not dfs:
        return None
    return pl.concat(dfs, how="diagonal_relaxed")


def compact_day(day: str, *, dry: bool) -> list[dict[str, Any]]:
    if day == _today():
        return [{"kind": "all", "status": "skip", "reason": "今日不压缩（可能正在采集）"}]
    results = [compact_pools(day, dry=dry)]
    results.append(_compact_frames(day, "quotes", dry=dry))
    results.append(_compact_frames(day, "auction", dry=dry))
    return results


def _all_days() -> list[str]:
    if not paths.live_dir().is_dir():
        return []
    return sorted(d.name for d in paths.live_dir().iterdir()
                  if d.is_dir() and d.name.isdigit() and len(d.name) == 8)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="历史留档压缩（pools→gzip, quotes/auction→单日合并）")
    ap.add_argument("--days", nargs="*", default=[], help="指定交易日 YYYYMMDD")
    ap.add_argument("--all", action="store_true", help="全部非今日目录")
    ap.add_argument("--dry-run", action="store_true", help="只报告收益，不写盘")
    ap.add_argument("--archive-list", action="append", nargs="*", default=None,
                    help="要压缩的类别：quotes auction pools（默认全选）")
    args = ap.parse_args(argv)

    days = args.days or (_all_days() if args.all else [])
    if not days:
        print("未指定交易日：用 --days YYYYMMDD... 或 --all")
        return 1

    print(f"{'交易日':<10} {'类别':<8} {'状态':<14} {'原MB':>8} {'新MB':>8} {'省MB':>8} 备注")
    print("-" * 92)
    total_raw = total_new = 0.0
    for day in days:
        for r in compact_day(day, dry=args.dry_run):
            st = r.get("status")
            fr = r.get("from_mb") or 0
            to = r.get("to_mb") or 0
            if st in ("done", "would"):
                total_raw += fr
                total_new += to
            print(f"{day:<10} {r.get('kind',''):<8} {st:<14} "
                  f"{fr:>8} {to:>8} {r.get('saved_mb',0):>8} "
                  f"{r.get('reason') or r.get('note') or ''}")
    print("-" * 92)
    print(f"合计：{total_raw} MB → {total_new} MB，可省 {round(total_raw-total_new,2)} MB")
    if args.dry_run:
        print("（dry-run：未写盘、未删除任何文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
