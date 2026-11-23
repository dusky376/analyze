"""统一数据路径解析（悟空真经单策略版）。

所有路径在这里解析一次，其余模块只 import 常量，不自己拼路径。

解析优先级（从高到低）
--------------------
1. 环境变量 ``WUKONG_DATA_HOME``：显式指定 live_terminal 的数据根；
   设置后只认它，无效则降级为本文件相对路径（不静默指向别处）。
2. 本文件位置推断：``live_terminal/backend -> live_terminal/``。

行情缓存（calendar / stock_list / industry_history / delist）收在
``live_terminal/data/market_cache``，不再依赖任何外部研究仓。
"""
from __future__ import annotations

import os
from pathlib import Path


_TERMINAL = Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    """live_terminal/data —— 本终端唯一可写数据根。"""
    env = os.environ.get("WUKONG_DATA_HOME")
    if env:
        return Path(env) / "data"
    return _TERMINAL / "data"


def market_cache_dir() -> Path:
    """行情缓存根（交易日日历等只读 meta 的所在地）。"""
    return data_dir() / "market_cache"


# 兼容旧常量名：feeds.py / market_clock.py 仍引用 MARKET_CACHE。
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
    return {
        "data_home": str(data_dir()),
        "resolved_by": "WUKONG_DATA_HOME 环境变量" if os.environ.get("WUKONG_DATA_HOME")
                       else "相对本文件位置推断",
        "market_cache": str(cache),
        "market_cache_exists": cache.is_dir(),
    }
