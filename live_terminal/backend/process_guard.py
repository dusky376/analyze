# -*- coding: utf-8 -*-
"""进程源码版本乐观锁与统一写盘护栏（Single Source of Truth）。

防止磁盘源码升级后，未重启的旧版常驻进程（_warm_loop / recorder）继续将旧逻辑
计算出的脏状态写回 sim_state.json / live_sim.json / overseas.json / pools.jsonl。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STALE_GRACE_SEC = 5.0

_LOCK = threading.RLock()
_BLOCKED_WRITES: list[dict[str, Any]] = []


def source_build() -> float:
    """磁盘上源码（app.py / backend/*.py / web/*）的最新 mtime（秒）。"""
    newest = 0.0
    p = ROOT / "app.py"
    if p.is_file():
        newest = max(newest, p.stat().st_mtime)
    for sub in ("backend", "web"):
        d = ROOT / sub
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".js", ".css", ".html"):
                newest = max(newest, f.stat().st_mtime)
    return newest


PROCESS_START_BUILD: float = source_build()


def under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in os.environ.get("_", "")


def is_stale_process(*, force_check: bool = False) -> bool:
    """当前进程启动时加载的代码是否落后于磁盘最新源码。"""
    if under_pytest() and not force_check:
        return False
    return source_build() > PROCESS_START_BUILD + STALE_GRACE_SEC


def can_write(op: str, target: str | Path = "", *, force_check: bool = False) -> bool:
    """写盘前版本护栏：旧版僵尸进程严禁覆写状态文件或行情留档。"""
    if not is_stale_process(force_check=force_check):
        return True
    now_iso = datetime.now().isoformat(timespec="seconds")
    t_str = str(target or "")
    with _LOCK:
        if not _BLOCKED_WRITES or (
            _BLOCKED_WRITES[-1].get("op") != op or _BLOCKED_WRITES[-1].get("path") != t_str
        ):
            _BLOCKED_WRITES.append({"op": op, "path": t_str, "at": now_iso})
            if len(_BLOCKED_WRITES) > 20:
                del _BLOCKED_WRITES[:-20]
    return False


def status() -> dict[str, Any]:
    """供 /api/health 与 /api/version 返回统一进程与写盘拦截状态。"""
    disk = source_build()
    stale = disk > PROCESS_START_BUILD + STALE_GRACE_SEC
    with _LOCK:
        blocked = list(_BLOCKED_WRITES)
    return {
        "build": PROCESS_START_BUILD,
        "disk_build": disk,
        "stale": stale,
        "stale_write_blocked": bool(blocked),
        "blocked_count": len(blocked),
        "blocked_recent": blocked[-5:],
    }
