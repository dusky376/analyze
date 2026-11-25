"""冰点样本库自动更新（用户 2026-11-23 裁定「做法 B」）。

裁定原文
--------
「并且现在还需要与时俱进的更新冰点数据库，如果冰点命中就要新增样本」

因此：**冰点命中即新增样本，并参与分位校准**。为了不变成黑箱，样本带
`source` 标记（`anchor` = 用户人工标注 / `auto` = 算法自动命中），全部可审计、
可回溯、可删除。

三种样本（严格区分，绝不混淆）
------------------------------
1. ``seeds``        用户人工标注的锚点（17 个，含 2 个已确认标错的）。**不可被自动写入覆盖**。
2. ``auto_samples`` 算法判定的冰点命中日。**用户裁定：参与校准**。
   每条含：等级、四维档位、原始指标、命中路径、仓位系数、检测时间。
3. ``history``      全部交易日的四维指标（不止冰点日）。供分位滚动计算与漂移监控。

命中路径（``ice_type``）
------------------------
冰点有两条判据，性质不同，必须分开记（否则复盘时会把"普跌"和"恐慌"混为一谈）：
    ``multi_dim``    多维确认：S>=L4 且 至少 2 维>=L3
    ``abs_limit_down``  跌停绝对量 >= 40 家
两者可同时成立，此时记 ``both``。

漂移监控
--------
用户人工种子（anchor）是"人认为的冰点"，auto 是"算法认为的冰点"。两者的
重合率就是漂移指标：重合率低 = 形态漂移或判据失准。本模块提供
:func:`drift`，并在样本库中记录每次重算的重合率历史。

诚实边界
--------
自动命中参与校准意味着**算法会用自己判定的结果反过来调整自己**（自我印证）。
这正是备份 `strategy_freeze.py` 当初冻结学习的原因。用户已明确选择做法 B，
因此本实现必须：
  * 保留 `source` 区分，任何时候能剔除全部 auto 样本回到纯人工口径；
  * 记录 ``calibrated_by`` 字段（本次分位用了多少 anchor / auto）；
  * 不静默丢弃任何样本（只标记 ``superseded`` 而非删除）。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
from backend import paths  # noqa: E402

# 数据与代码分离：默认落在数据根；WUKONG_ICE_SAMPLES 可显式覆盖。
STORE = Path(os.environ.get("WUKONG_ICE_SAMPLES", str(paths.data_dir() / "ice_samples.json")))

#: 样本库结构版本
STORE_VERSION = 2

#: 写锁：`app.py` 是 threaded=True，多个请求会同时 load-modify-save。
#: 2026-11-23 审计实测：无锁时 120 次并发写只落盘 1 条、118 次 PermissionError。
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 读写（原子写，进程被杀不留半个文件）
# ---------------------------------------------------------------------------

def _empty() -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "updated_at": None,
        "feature_note": None,
        "seeds": [],
        "auto_samples": {},
        "history": {},
        "bounds_snapshot": None,
        "drift_history": [],
    }


def load() -> dict[str, Any]:
    """读样本库；文件缺失/损坏时返回空结构（不抛异常，不阻塞主流程）。

    2026-11-23 审计：损坏文件若只静默返回空结构，下一次 save() 会用空结构
    覆盖真实样本库（anchors 永久丢失）。因此读到坏文件时先备份为
    ``ice_samples.corrupt.<ts>.json`` 再降级。
    """
    if not STORE.is_file():
        return _empty()
    try:
        with _LOCK:                       # 与 save() 的 replace 互斥
            raw = STORE.read_text(encoding="utf-8")
        d = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        _backup_corrupt()
        return _empty()
    if not isinstance(d, dict):
        _backup_corrupt()
        return _empty()
    base = _empty()
    for k, v in base.items():
        if k not in d:
            d[k] = v
        elif isinstance(v, dict) and not isinstance(d[k], dict):
            d[k] = v
        elif isinstance(v, list) and not isinstance(d[k], list):
            d[k] = v
        # 标量（version/updated_at 等）不强制类型，避免把 None 兜底覆盖真实值
    return d


def _backup_corrupt() -> None:
    """把损坏的样本库另存一份，避免下一次 save() 覆盖掉人工锚点。"""
    try:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        STORE.replace(STORE.with_name(f"{STORE.stem}.corrupt.{ts}.json"))
    except OSError:
        pass


def save(data: dict[str, Any], *, only_if_changed: bool = True) -> bool:
    """原子写回。返回是否真的写了盘。

    Args:
        only_if_changed: True 时，若内容与磁盘上的（去掉 updated_at 后）完全一致
                         就跳过写入。避免每次 sync 都刷新时间戳、造成无意义的
                         git diff 与磁盘写（用户要求"精简"）。

    并发安全：整体持 `_LOCK`；临时文件名带 pid+uuid，
    避免多线程写同一个 tmp（2026-11-23 审计实测的 PermissionError 根因）。
    """
    with _LOCK:
        data["version"] = STORE_VERSION
        if only_if_changed and STORE.is_file():
            try:
                old = json.loads(STORE.read_text(encoding="utf-8"))
                a = {k: v for k, v in old.items() if k != "updated_at"}
                b = {k: v for k, v in data.items() if k != "updated_at"}
                if a == b:
                    return False
            except (OSError, json.JSONDecodeError):
                pass
        data["updated_at"] = _now()
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_name(f"{STORE.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp.json")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE)
    return True


class locked_update:
    """把「load → 修改 → save」作为一个原子临界区。

    2026-11-23 审计 P0-4：`assess(persist=True)` 是无锁的
    load-modify-save，并发时互相覆盖。Windows 上 `Path.replace`
    与另一线程的读取还会撞 `PermissionError (WinError 5/32)`。
    用法::

        with ice_samples.locked_update() as st:
            ice_samples.record_day(st, ...)

    （退出时自动 save；`st` 是可变 dict，原地修改即可。）
    """

    def __enter__(self) -> dict[str, Any]:
        _LOCK.acquire()
        try:
            self._data = load()
            return self._data
        except Exception:
            _LOCK.release()
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                save(self._data, only_if_changed=False)
        finally:
            _LOCK.release()
        return False


# ---------------------------------------------------------------------------
# 写入样本
# ---------------------------------------------------------------------------

def ice_type(dims: dict[str, Any], rec: dict[str, Any], *,
             abs_limit_down: int = 40) -> str | None:
    """判定命中路径：multi_dim / abs_limit_down / both；不命中返回 None。"""
    from backend import ice_gate

    s = ice_gate.severity(dims)
    n_ge3 = sum(1 for v in dims.values()
                if v in ice_gate.LEVEL_ORDER and ice_gate.LEVEL_ORDER.index(v) >= 3)
    by_multi = bool(s and ice_gate.LEVEL_ORDER.index(s) >= 4 and n_ge3 >= 2)
    by_abs = (rec.get("limit_down_n") or 0) >= abs_limit_down
    if by_multi and by_abs:
        return "both"
    if by_multi:
        return "multi_dim"
    if by_abs:
        return "abs_limit_down"
    return None


def record_day(data: dict[str, Any], *, day: str, metrics: dict[str, Any],
               dims: dict[str, Any], level: str | None, is_ice: bool,
               size_factor: float, itype: str | None,
               source: str = "auto") -> bool:
    """写入一天的记录；返回是否新增了 auto 样本。"""
    key = day
    entry = {
        "date": day,
        "metrics": {
            "down_share": metrics.get("down_share"),
            "limit_down_n": metrics.get("limit_down_n"),
            "limit_up_n": metrics.get("limit_up_n"),
            "dt_ratio": metrics.get("dt_ratio"),
            "broken_hi_n": metrics.get("broken_hi_n"),
            "up": metrics.get("up"),
            "down": metrics.get("down"),
        },
        "dim_levels": dict(dims or {}),
        "severity_level": level,
        "is_ice": bool(is_ice),
        "ice_type": itype,
        "size_factor": size_factor,
        "detected_at": _now(),
    }
    # history：全部交易日都记（供分位滚动）
    data["history"][key] = {**entry, "source": source}

    # auto_samples：只有命中才记（用户裁定 B）
    if not is_ice:
        # 2026-11-23 修复（审计 P0-5）：判定改为「非冰」时必须撤销旧的 auto 记录，
        # 否则 history 说非冰点、auto_samples 说冰点，样本库自相矛盾，
        # 且 drift() 仍把该日算作算法命中，虚高人工锚点重合率。
        # 不直接删除：标 superseded 保留痕迹（可回溯原则）。
        prev = data["auto_samples"].get(key)
        if prev is not None:
            prev["superseded"] = True
            prev["superseded_at"] = _now()
            data["auto_samples"].pop(key, None)
            data.setdefault("superseded_samples", {})[key] = prev
            return False
        return False
    prev = data["auto_samples"].get(key)
    if prev is not None:
        # 幂等：同一天重复写入只更新，不重复计数
        data["auto_samples"][key] = {**prev, **entry, "source": "auto"}
        return False
    data["auto_samples"][key] = {**entry, "source": "auto"}
    # 若曾被判非冰/被撤销，重新命中时从 superseded 移出
    if key in (data.get("superseded_samples") or {}):
        data["superseded_samples"].pop(key, None)
    return True


def calibrate_rows(data: dict[str, Any], *, include_auto: bool = True
                   ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """取用于分位校准的行（用户裁定 B：auto 参与校准）。

    Args:
        include_auto: True（默认，做法 B）= auto 样本参与分位校准；
                      False = 只用人工 anchor 与无来源标记的历史记录，
                      用于「回到纯人工口径」的对照实验。

    Returns:
        (rows, counted) —— rows 为四维指标行；counted 记录用了多少 anchor/auto/history。
    """
    rows: list[dict[str, Any]] = []
    counted = {"anchor": 0, "auto": 0, "history": 0}

    anchors = {s.get("date") for s in data.get("seeds") or []
               if isinstance(s, dict) and s.get("status") == "anchor"}
    for key, h in (data.get("history") or {}).items():
        if not isinstance(h, dict):
            continue
        is_anchor = key in anchors
        is_auto = (not is_anchor) and h.get("source") == "auto"
        if is_auto and not include_auto:
            continue                      # 纯人工口径：跳过 auto
        m = h.get("metrics") or {}
        rows.append({**m, "day": h.get("date") or key})
        if is_anchor:
            counted["anchor"] += 1
        elif is_auto:
            counted["auto"] += 1
        else:
            counted["history"] += 1
    return rows, counted
    return rows, counted


def drift(data: dict[str, Any]) -> dict[str, Any]:
    """漂移监控：人工锚点 vs 自动命中的重合度。

    重合率高 = 算法与人判断一致；低 = 形态漂移或判据失准。
    """
    anchors = sorted({s.get("date") for s in data.get("seeds") or []
                      if isinstance(s, dict) and s.get("status") == "anchor"})
    autos = sorted((data.get("auto_samples") or {}).keys())
    hits = sorted(set(anchors) & set(autos))
    n_a, n_u = len(anchors), len(autos)
    hit_rate = round(len(hits) / n_a, 4) if n_a else None
    return {
        "anchor_n": n_a,
        "auto_n": n_u,
        "overlap_n": len(hits),
        "anchor_hit_rate": hit_rate,
        "anchor_dates": anchors,
        "auto_dates": autos,
        "overlap_dates": hits,
        "note": ("anchor=用户标注、auto=算法判定；anchor_hit_rate 低说明"
                 "算法与人判断分歧大（可能形态漂移或阈值失准）。"),
    }
