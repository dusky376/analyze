"""统一交易时钟 · 缓存策略的唯一裁判（2026-11-17 用户硬约束）。

用户原话（必须逐条落地，不得打折）
==================================
1. "非交易时间才能用交易时间的缓存：今天晚上、周末不交易的时候才能用缓存"
2. "并且都只能用收盘的时候的数据"
3. "美日韩指数板块信息等都要根据美日韩交易时间来，A股用A股的交易时间"

由此确立三条铁律（代码化，不是口号）
====================================
R1 交易时段内：**禁止任何缓存命中**（磁盘缓存、分钟桶缓存都不信），
   每次请求都取实时快照现算。缓存命中的后果是 10 点看到 5 分钟前的盘口 —— 短线致命。
R2 非交易时段：**允许缓存，但缓存条目必须带 `final=True`（收盘定稿）标记**。
   盘中算出来的半日快照（`final=False`）即使落盘也不得在非交易时段被读取，
   因为它是未定稿数据（溢价/晋级率/涨停数收盘前会变）。
R3 每个市场用各自的当地交易时钟判断"是否在交易"：
   A 股用 Asia/Shanghai（含午休），美股用 America/New_York，
   日经用 Asia/Tokyo，韩国用 Asia/Seoul。跨市场不能共用一个"是否交易"。

2026-11-17 验收修复（P1，08_复测_架构师.md）
============================================
1. **节假日日历**（旧实现只用 `weekday()<5`，国庆 10-01 / 感恩节 11-26 都会判成交易中）：
   - CN：读 `live_terminal/data/market_cache/meta/calendar.parquet`
     （与 `feeds._trading_days()` 同源，含未来交易日；文件 mtime/size 变了自动重载）；
     日历覆盖区间内**以日历为准**（节假日 False；不含周末）；
     日历缺失或日期超出覆盖区间 → 退回「工作日近似」并 **`degraded=True`（fail-closed 标注）**。
   - US：内置联邦假日规则（元旦/MLK/总统日/耶稣受难日/阵亡将士/六月节/独立日/
     劳工节/**感恩节=11 月第 4 个周四**/圣诞，含周六→周五、周日→周一的观测规则）；
     不依赖第三方包（环境未安装 `holidays`/`pandas_market_calendars`，实测 ImportError）。
   - JP/KR：无可靠节假日源 → 仅工作日近似，`degraded=True` **显式标注**，不假装准确。
2. **`readable()` 语义漏洞**：旧实现 `return bool(entry)`，导致 `readable({'foo':'bar'})`
   与 `readable('x')` 都为 True。现要求条目必须是 `make_cache_entry()` 产出的
   `{"final": bool, "payload": ...}`，且 `final=True` 且 `payload is not None`，否则 False。
3. **R1/R2 接线（消除死代码）**：`make_cache_entry` / `readable` / `cache_allowed`
   过去全库无生产调用。现在 `feeds.live_signature()` 的非交易分支会实际走一遍
   `session_final()` → `make_cache_entry()` → `assert_readable()`；
   不可读时返回非定稿签名，迫使上层重建，而不是把半日快照当收盘数据。

downgrade 原则：时区库不可用时按"交易中"处理（fail-closed）——
宁可多算一次实时，也不能在盘中误用缓存。

2026-11-23 精简为悟空真经单策略后的适用范围
==========================================
生产链路（app.py / recorder / feeds）只用 CN 分支：
`trading_state("CN")` / `is_trading_day("CN")` / `is_cn_trading()` / `readable()`
等。US / JP / KR 分支在生产中**无调用**，目前由 `tests/test_market_clock.py`
（R3 多市场契约）覆盖，按"仅测试用"保留；如后续测试随策略精简移除，可整组删除
（SESSIONS/TZ 的 US/JP/KR 项、_US_HOL_MEM/_nth_weekday/_observed/_easter/
_us_holidays/_us_is_holiday、_day_state 的 US/JP/KR 分支、status() 的多市场循环）。
"""
from __future__ import annotations

import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 时区（Python 3.9+ 内置 zoneinfo；Windows 需 tzdata，缺失时降级 UTC+8 手工处理）
try:
    TZ_CN = ZoneInfo("Asia/Shanghai")
    TZ_US = ZoneInfo("America/New_York")
    TZ_JP = ZoneInfo("Asia/Tokyo")
    TZ_KR = ZoneInfo("Asia/Seoul")
    _TZ_OK = True
except ZoneInfoNotFoundError:  # noqa: BLE001
    TZ_CN = TZ_US = TZ_JP = TZ_KR = None  # type: ignore[assignment]
    _TZ_OK = False

#: 各市场当地交易时段（含午休的市场分两段）。时间均为当地时间。
#: 美股 09:30-16:00 连续（无午休）；A股 09:30-11:30 / 13:00-15:00；
#: 日本 09:00-11:30 / 12:30-15:00；韩国 09:00-11:30 / 12:30-15:30。
#: 边界语义：**两端都含**（`a <= t <= b`）——11:30:00 / 15:00:00 判为交易中，
#: 11:30:01 / 15:00:01 判为非交易。这是对"收盘整点"的保守处理（宁可现算）。
SESSIONS: dict[str, tuple[tuple[dtime, dtime], ...]] = {
    "CN": ((dtime(9, 30), dtime(11, 30)), (dtime(13, 0), dtime(15, 0))),
    # 以下 US/JP/KR 仅测试用（生产只用 CN；tests/test_market_clock.py R3 契约依赖）
    "US": ((dtime(9, 30), dtime(16, 0)),),
    "JP": ((dtime(9, 0), dtime(11, 30)), (dtime(12, 30), dtime(15, 0))),
    "KR": ((dtime(9, 0), dtime(11, 30)), (dtime(12, 30), dtime(15, 30))),
}
TZ: dict[str, Any] = {"CN": TZ_CN, "US": TZ_US, "JP": TZ_JP, "KR": TZ_KR}  # US/JP/KR 仅测试用


# ---------------------------------------------------------------------------
# 交易日历（节假日）
# ---------------------------------------------------------------------------

_CAL_LOCK = threading.Lock()
_CAL_MEM: dict[str, Any] = {}
_US_HOL_MEM: dict[int, frozenset[date]] = {}  # 仅测试用（US 假日规则，生产不读）


def _calendar_path() -> Path | None:
    """CN 交易日历路径（与 `feeds._trading_days()` 同源，统一由 paths 解析）。"""
    try:
        from backend import paths
        return Path(paths.MARKET_CACHE) / "meta" / "calendar.parquet"
    except Exception:  # noqa: BLE001 - 路径解析失败 → 降级，不抛
        return None


def _cn_calendar() -> dict[str, Any]:
    """CN 真实交易日历（只读；mtime+size 变化自动重载）。

    返回：
      {"ok": True, "days": frozenset[date], "first": date, "last": date,
       "path": str, "sig": (...)}
      或 {"ok": False, "reason": str}
    """
    path = _calendar_path()
    if path is None or not path.is_file():
        return {"ok": False, "reason": "缺少 calendar.parquet"}
    try:
        st = path.stat()
    except OSError as exc:
        return {"ok": False, "reason": f"calendar 不可读：{type(exc).__name__}"}
    sig = (str(path), round(st.st_mtime, 3), st.st_size)
    with _CAL_LOCK:
        if _CAL_MEM.get("sig") == sig:
            return _CAL_MEM
    try:
        import polars as pl

        col = pl.read_parquet(path, columns=["trade_date"])["trade_date"]
        days = {d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
                for d in col.to_list()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"calendar 解析失败：{type(exc).__name__}"}
    if not days:
        return {"ok": False, "reason": "calendar 为空"}
    out: dict[str, Any] = {
        "ok": True, "sig": sig, "path": str(path),
        "days": frozenset(days), "first": min(days), "last": max(days),
        "source": "live_terminal/data/market_cache/meta/calendar.parquet",
    }
    with _CAL_LOCK:
        _CAL_MEM.clear()
        _CAL_MEM.update(out)
    return out


def _reset_calendar_cache() -> None:
    """测试/热更新用：清空日历与节假日内存缓存。"""
    with _CAL_LOCK:
        _CAL_MEM.clear()
    _US_HOL_MEM.clear()


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """某月第 n 个星期 weekday（0=周一）；n=-1 表示最后一个。（仅测试用：US 假日规则）"""
    if n > 0:
        first = date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        return first + timedelta(days=delta + 7 * (n - 1))
    last = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) \
        - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """美国联邦假日观测规则：周六→前一个周五，周日→后一个周一。（仅测试用）"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter(year: int) -> date:
    """复活节（Anonymous Gregorian algorithm），用于耶稣受难日（-2 天）。（仅测试用）"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _us_holidays(year: int) -> frozenset[date]:
    """美国 NYSE 休市日（内置规则，覆盖 1970+ 可接受的近似）。

    含：元旦 / MLK(1 月第 3 个周一) / 总统日(2 月第 3 个周一) /
    耶稣受难日 / 阵亡将士(5 月最后一个周一) / 六月节(6-19, 2022+ /
    独立日(7-4) / 劳工节(9 月第 1 个周一) / **感恩节(11 月第 4 个周四)** /
    圣诞(12-25)。周末假日按观测规则移位。
    未建模：提前收盘（13:00 半日）、临时休市（国葬/自然灾害）——见 `degraded` 说明。
    **仅测试用**：生产链路只用 CN 分支，本函数由 tests/test_market_clock.py 覆盖。
    """
    hit = _US_HOL_MEM.get(year)
    if hit is not None:
        return hit
    days: set[date] = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter(year) - timedelta(days=2),
        _nth_weekday(year, 5, 0, -1),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    out = frozenset(days)
    _US_HOL_MEM[year] = out
    return out


def _us_is_holiday(d: date) -> bool:
    """跨年观测：元旦落在周六时会观测到上一年 12-31，故查邻近年份。（仅测试用）"""
    return any(d in _us_holidays(y) for y in (d.year - 1, d.year, d.year + 1))


def _day_state(market: str, local_date: date) -> dict[str, Any]:
    """某个市场「这一天是不是交易日」。CN 用真实日历，US 用规则，JP/KR 标注 degraded。"""
    weekend = local_date.weekday() >= 5
    if market == "CN":
        cal = _cn_calendar()
        if cal.get("ok") and cal["first"] <= local_date <= cal["last"]:
            is_td = local_date in cal["days"]
            return {
                "is_trading_day": is_td,
                "weekend": weekend,
                "holiday": (not weekend) and (not is_td),
                "degraded": False,
                "source": cal.get("source"),
            }
        return {
            "is_trading_day": not weekend,
            "weekend": weekend,
            "holiday": None,
            "degraded": True,
            "source": (cal.get("reason") or "节日历不可用")
                      + " → 退回工作日近似（节假日可能误判）",
        }
    if market == "US":
        is_hol = _us_is_holiday(local_date)
        return {
            "is_trading_day": (not weekend) and (not is_hol),
            "weekend": weekend,
            "holiday": is_hol,
            "degraded": False,
            "source": "US 联邦假日内置规则（除夕/感恩节等；未建模提前收盘与临时休市）",
        }
    # JP / KR：没有可靠节假日源，明说 degraded，不假装准确
    return {
        "is_trading_day": not weekend,
        "weekend": weekend,
        "holiday": None,
        "degraded": True,
        "source": f"{market} 无节假日日历：仅工作日近似（法定假日可能误判）",
    }


# ---------------------------------------------------------------------------
# 时钟
# ---------------------------------------------------------------------------

def _local_now(market: str) -> tuple[datetime, bool]:
    """返回 (市场当地时间, tz是否可用)。tz 不可用时用北京时间近似（仅 CN 准）。"""
    if _TZ_OK and TZ.get(market) is not None:
        return datetime.now(TZ[market]), True
    return datetime.now(), False


def _session_of(market: str, local: datetime) -> tuple[bool, str | None]:
    t = local.time().replace(tzinfo=None)
    for a, b in SESSIONS.get(market, ()):
        if a <= t <= b:
            return True, f"{a.strftime('%H:%M')}-{b.strftime('%H:%M')}"
    return False, None


def trading_state(market: str = "CN", now: datetime | None = None) -> dict[str, Any]:
    """该市场当前状态（交易中/节假日/降级），供 is_trading / status / 诊断使用。

    `now` 仅供测试注入（应为带时区的该市场当地时间）；生产用当地实时时间。
    """
    if market not in SESSIONS:
        return {"market": market, "trading": False, "is_trading_day": False,
                "in_session": False, "holiday": None, "degraded": True,
                "reason": f"未知市场 {market}", "cache_allowed": True,
                "local_time": None}
    if now is not None:
        local, tz_ok = now, True
    else:
        local, tz_ok = _local_now(market)
        if not tz_ok and market != "CN":
            # 时区库不可用 → fail-closed：按交易中处理（宁可现算不用缓存）
            return {"market": market, "trading": True, "is_trading_day": True,
                    "in_session": True, "holiday": None, "degraded": True,
                    "reason": "时区库(zoneinfo/tzdata)不可用 → fail-closed 按交易中",
                    "cache_allowed": False, "local_time": None}
    day = local.date()
    st = _day_state(market, day)
    in_session, window = _session_of(market, local)
    trading = bool(st["is_trading_day"]) and in_session
    reason = ("交易中：" + (window or "")) if trading else (
        f"节假日（{day.isoformat()}）" if st["holiday"]
        else ("周末" if st["weekend"] else ("非交易时段" if st["is_trading_day"]
                                           else "非交易日")))
    return {
        "market": market,
        "trading": trading,
        "is_trading_day": bool(st["is_trading_day"]),
        "in_session": in_session,
        "window": window,
        "holiday": st["holiday"],
        "weekend": st["weekend"],
        "degraded": bool(st["degraded"]),
        "source": st["source"],
        "calendar_ok": (not st["degraded"]) if market == "CN" else None,
        "local_time": local.isoformat(timespec="seconds"),
        "local_date": day.isoformat(),
        "reason": reason,
        "cache_allowed": not trading,
    }


def is_trading(market: str = "CN", now: datetime | None = None) -> bool:
    """该市场当前是否处于交易时段（当地时间 + 交易日历）。

    2026-11-17 修复：节假日（CN 国庆 / US 感恩节等）不再判为交易中。
    `now` 仅供测试注入（必须是带时区的该市场当地时间）；生产用当地时间。
    tz 不可用且 market != CN 时返回 True（fail-closed：宁可现算不用缓存）。
    """
    if market not in SESSIONS:
        return False
    return bool(trading_state(market, now)["trading"])


def is_trading_day(market: str = "CN", now: datetime | None = None) -> bool:
    """该市场当天是不是交易日（不含时段判断；节假日/周末为 False）。"""
    if market not in SESSIONS:
        return False
    return bool(trading_state(market, now)["is_trading_day"])


def is_cn_trading(now: datetime | None = None) -> bool:
    """A 股是否交易中（最常用入口）。"""
    return is_trading("CN", now)


def cache_allowed(market: str = "CN", now: datetime | None = None) -> bool:
    """**当前是否允许用缓存**（R1）。

    A 股数据：A 股交易中 → False（必须实时）；非交易时段 → True。
    外盘数据：对应市场交易中 → False；否则 True。
    这样"今晚"美股开盘时，A股模块可用缓存，但美股模块必须实时 —— 符合 R3。
    """
    return not is_trading(market, now)


def session_final(market: str = "CN", now: datetime | None = None) -> bool:
    """R2：当前可读的数据是否"收盘定稿"。

    项目里当日 live bar 只在 15:05 后才合并进 EOD 视图（`feeds.LIVE_MERGE_AFTER`），
    所以非交易时段拿到的最后一日永远来自已收盘的交易日 → 定稿。
    交易时段内数据不是定稿，但那时 `cache_allowed()==False`、根本不允许读缓存。
    保留独立函数是为了让 R2 闸门有单一实现，便于将来接盘中快照。
    """
    return not is_trading(market, now)


def make_cache_entry(payload: Any, *, final: bool) -> dict[str, Any]:
    """把业务 payload 包成带"是否收盘定稿"标记的缓存条目（R2）。

    final=True  仅允许收盘后（15:05 闸门）写入；非交易时段可被读取。
    final=False 盘中快照，可写入供同一次请求复用，但**任何缓存读取都必须跳过它**。
    """
    return {
        "final": bool(final),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "payload": payload,
    }


def readable(entry: Any) -> bool:
    """缓存条目是否可被读取（R2）：必须 `final=True` 且 payload 非 None。

    2026-11-17 修复：旧实现 `return bool(entry)` 会把 `{'foo':'bar'}`、`'x'`
    这类任意真值当"可读缓存"，等于 R2 形同虚设。现在只认
    `make_cache_entry()` 产出的条目结构，其余一律 False。
    """
    if not isinstance(entry, dict):
        return False
    if "final" not in entry or "payload" not in entry:
        return False
    return bool(entry.get("final")) and entry.get("payload") is not None


def assert_readable(entry: Any, *, market: str = "CN",
                    now: datetime | None = None) -> bool:
    """R2 闸门（谓词）：非交易时段 + `readable(entry)` 才允许读缓存。

    - 交易时段内：恒 False（R1 禁止任何缓存命中）；
    - 非交易时段：只有 `final=True` 且 payload 非空的条目才 True。
    返回值语义稳定（不抛异常），便于在采集热路径直接接
    `if not assert_readable(...): 重建`。
    """
    return bool(cache_allowed(market, now)) and readable(entry)


def status() -> dict[str, Any]:
    """给 /api/health 与前端展示各市场当前交易状态（含节假日与降级标注）。

    生产当前未调用本函数；CN 以外的市场分支仅测试用（tests/test_market_clock.py）。
    """
    out: dict[str, Any] = {}
    for m in ("CN", "US", "JP", "KR"):
        st = trading_state(m)
        out[m] = {
            "trading": st["trading"],
            "is_trading_day": st["is_trading_day"],
            "cache_allowed": st["cache_allowed"],
            "holiday": st["holiday"],
            "degraded": st["degraded"],
            "source": st["source"],
            "local_time": st["local_time"],
        }
    return out
