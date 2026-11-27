"""日期归一化：禁止裸字符串比较日期（G-0062）。

为什么单独一个模块
==================
2026-11-15 发现 `live_sim.find_exit` 用 `str(d["date"]) > entry_date` 比较
`'2026-11-14'` 与 `'20261113'`，逐字符比较 `'2' < '6'` → 恒为 False，
导致持仓永远 `pending_daily`、结算永不触发、反馈闭环从未运行。

根因不是某一处写错，而是**项目里日期有 3 种格式并存**：
  - `positions[].entry_date` / `orders[].day`：`YYYYMMDD`（`datetime.now():%Y%m%d`）
  - `load_daily()` / panel：`YYYY-MM-DD`（polars Date 的 str）
  - `paper/live_sim.json` 的 `generated_at`：ISO 8601 带时间

只要允许「拿两种格式的字符串直接比大小」，同类 bug 就会反复出现。
本模块提供唯一归一化入口，约定**整数比较**而非字符串比较。

使用约定（写入 CLAUDE/AGENTS 级纪律）
=====================================
1. 任何比较时间先后的地方，一律 `norm(d)` 成 int 再比；
2. 不得新增 `str(a) > str(b)` 形式的日期比较；
3. `YYYY-MM-DD HH:MM:SS`、`YYYYMMDD`、`date`/`datetime` 对象都接受。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

#: 归一化后的类型标记：8 位整数，如 20261113
NormDay = int

_INVALID = 0
_MIN_DAY = 19000101
_MAX_DAY = 29991231


def norm_day(value: Any) -> NormDay:
    """把各种日期表示归一成 `YYYYMMDD` 整数；无法解析返回 0（显式无效）。

    绝不抛异常：脏数据在数据管道里应表现为「可判定的无效」，
    而不是让整条链路崩掉或静默跳过（静默跳过正是 11-13 停更的成因）。

    >>> norm_day("2026-11-13")
    20261113
    >>> norm_day("20261113")
    20261113
    >>> norm_day(date(2026, 11, 13))
    20261113
    >>> norm_day("2026/11/13 15:00:00")
    20261113
    >>> norm_day(None)
    0
    """
    if value is None:
        return _INVALID
    if isinstance(value, datetime):
        return value.year * 10000 + value.month * 100 + value.day
    if isinstance(value, date):
        return value.year * 10000 + value.month * 100 + value.day

    s = str(value).strip()
    if not s:
        return _INVALID
    # 取日期部分：兼容 "2026-11-13 15:00:00" / "2026-11-13T15:00:00"
    head = s.replace("/", "-").split(" ")[0].split("T")[0]
    digits = "".join(ch for ch in head if ch.isdigit())
    if len(digits) < 8:
        return _INVALID
    try:
        y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
    except ValueError:
        return _INVALID
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return _INVALID
    day = y * 10000 + m * 100 + d
    if not (_MIN_DAY <= day <= _MAX_DAY):
        return _INVALID
    return day


def is_valid(value: Any) -> bool:
    """是否可解析为有效日期。"""
    return norm_day(value) != _INVALID


def fmt_iso(value: Any) -> str | None:
    """归一化为 `YYYY-MM-DD` 字符串（给人看/给 JSON 用），无效返回 None。"""
    d = norm_day(value)
    if not d:
        return None
    return f"{d // 10000:04d}-{d // 100 % 100:02d}-{d % 100:02d}"


def fmt_compact(value: Any) -> str | None:
    """归一化为 `YYYYMMDD` 字符串（文件名/幂等键用），无效返回 None。"""
    d = norm_day(value)
    return str(d) if d else None


def is_after(a: Any, b: Any, *, strict: bool = True) -> bool:
    """a 是否晚于 b（整数比较，永不踩字符串字典序的坑）。无效日期一律 False。"""
    da, db = norm_day(a), norm_day(b)
    if not da or not db:
        return False
    return da > db if strict else da >= db


__all__ = ["NormDay", "norm_day", "is_valid", "fmt_iso", "fmt_compact", "is_after"]