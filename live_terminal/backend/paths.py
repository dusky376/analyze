"""统一数据路径解析（悟空真经单策略版）· 数据与代码分离的唯一事实源。

设计原则
--------
**代码目录与数据目录彻底分离**：

    E:\\workspace\\量化\\
    ├─ live_terminal\\      ← 纯代码（app.py / backend / web / tests / tools / configs）
    └─ data\\               ← 纯数据（面板 / 留档 / 账本 / 题材 / 行情缓存）

任何模块**不得**自己拼 ``ROOT / "data"``；一律调用本模块的函数。
这样数据可以整体搬走（换盘、外接盘、备份盘），代码零改动。

解析优先级（从高到低）
--------------------
1. 环境变量 ``WUKONG_DATA_HOME``：显式指定**数据根目录**。
   两种写法都接受，自动识别：
     * 指到项目根：``E:\\workspace\\量化``  → 数据在 ``<它>/data``
     * 指到数据目录本身：``E:\\workspace\\量化\\data`` → 原样使用
2. 本文件位置推断：``live_terminal/backend`` → 项目根 = ``parents[2]``
   → 数据目录 = ``<项目根>/data``。
3. 向后兼容：若项目根下没有 ``data`` 但 ``live_terminal/data`` 存在
   （旧布局），自动回落到旧位置，避免历史 checkout 直接失效。

所有子目录解析函数都基于 :func:`data_dir`，因此上面三级优先级对全部
数据（面板 / 留档 / 账本 / 题材 / 缓存）同时生效。
"""
from __future__ import annotations

import os
from pathlib import Path

#: live_terminal/backend → parents[0]=backend, [1]=live_terminal, [2]=项目根
_TERMINAL = Path(__file__).resolve().parents[1]
_PROJECT = _TERMINAL.parent

#: 旧布局（数据在代码目录内）的回落位置；仅为兼容历史 checkout 保留。
_LEGACY_DATA = _TERMINAL / "data"


def _resolve_data_home() -> Path:
    """按优先级解析数据根目录（含旧布局回落）。"""
    env = os.environ.get("WUKONG_DATA_HOME")
    if env:
        p = Path(env).expanduser()
        # 既支持「指向项目根」，也支持「直接指向 data 目录」
        if p.name.lower() == "data":
            return p
        return p / "data"
    new = _PROJECT / "data"
    if new.is_dir() or not _LEGACY_DATA.is_dir():
        return new
    return _LEGACY_DATA


def project_root() -> Path:
    """项目根目录（含 live_terminal 与 data 的那一层）。"""
    d = data_dir()
    return d.parent if d.name.lower() == "data" else _PROJECT


def data_dir() -> Path:
    """数据根目录 —— 本终端唯一可写数据根。"""
    return _resolve_data_home()


def code_dir() -> Path:
    """代码根目录（live_terminal/）。"""
    return _TERMINAL


def market_cache_dir() -> Path:
    """行情缓存根（交易日日历等只读 meta 的所在地）。"""
    return data_dir() / "market_cache"


def live_dir() -> Path:
    """逐日留档根：``<data>/live/<YYYYMMDD>/``。"""
    return data_dir() / "live"


def paper_dir() -> Path:
    """模拟账本根：``<data>/paper/``。"""
    return data_dir() / "paper"


def themes_dir() -> Path:
    """题材池落盘根：``<data>/themes/``。"""
    return data_dir() / "themes"


def configs_dir() -> Path:
    """配置目录（题材轴等）——跟随代码，不属于数据。"""
    return _TERMINAL / "configs"


# 兼容旧常量名：feeds.py / market_clock.py 等仍引用 MARKET_CACHE。
MARKET_CACHE: Path = market_cache_dir()


def panel_adj_file() -> Path:
    """带后复权的历史面板（历史口径；精简版不产出、不读取此文件）。"""
    return data_dir() / "panel_adj.parquet"


def panel_raw_file() -> Path:
    """不复权历史面板（涨停/连板判据与断板结算的唯一真实日线来源）。"""
    return data_dir() / "panel_raw.parquet"


def panel_live_file() -> Path:
    """当日实时 bar 的独立落盘文件（与历史面板物理隔离，15:05 后合并视图）。"""
    return data_dir() / "panel_live.parquet"


def describe() -> dict[str, object]:
    """诊断信息，供 /api/health 或界面提示使用。"""
    cache = market_cache_dir()
    d = data_dir()
    if os.environ.get("WUKONG_DATA_HOME"):
        resolved = "WUKONG_DATA_HOME 环境变量"
    elif d == _LEGACY_DATA:
        resolved = "旧布局回落（live_terminal/data）"
    else:
        resolved = "项目根推断（<项目根>/data）"
    return {
        "data_home": str(d),
        "code_home": str(_TERMINAL),
        "separated": d != _LEGACY_DATA and _TERMINAL not in d.parents,
        "resolved_by": resolved,
        "market_cache": str(cache),
        "market_cache_exists": cache.is_dir(),
    }
