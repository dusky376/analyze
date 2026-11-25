"""数据完整性自检（只读诊断，不改任何数据/买卖点）。

回答两个问题：
  1. **今天该有的涨停池留档，落盘了吗？**
  2. **题材池（同花顺三池）跟上了吗？**

背景：悟空真经的选股/成交判定完全依赖 `data/live/<day>/pools/pools.jsonl`；
按题材分组还要 `data/themes/themes.parquet` 的当日标签。
2026-11-24 实测：涨停池照常在采，但题材池停更在 11-20，
当天有 11 只一字二板却因为「当日无题材池标签」全天 0 委托，
而健康灯是绿的 —— 本模块必须把这两个源分开报。

判定（当日 = 本地今天）：
  - missing     当日非交易日但无帧        → 正常（周末/节假日不该有）
  - missing     当日是交易日且盘后(≥15:05)无帧 → 警告（采集器可能没跑）
  - stale       有帧但最新留档日 < 上一交易日 → 警告（数据过期）
  - ok          当日是交易日且当日有帧 / 非交易日有最近帧
  - intraday    当日交易日且盘中(<15:05)无帧 → 提示（尚未收盘，正常）

题材池单独判：
  - stale       盘中/盘后主题材池没有当日标签 → 策略无法按题材分组（会 0 委托）
  - stale       盘中 last fetch 超过 10 分钟 → 实时刷新停了
  - ok          当日标签存在且新鲜

只出诊断字段，绝不写文件、不触发采集、不改账本。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# 数据与代码分离：留档根统一由 backend.paths 解析。
from backend import paths  # noqa: E402

LIVE = paths.live_dir()

#: 盘后阈值（本地时间）；15:05 之后应当已有当日涨停池留档。
EOD_HHMM = (15, 5)

#: 题材池盘中新鲜度阈值：超过这么久没刷新，视为实时采集停了。
THEME_IDLE_SEC = 600

#: 题材池盘中最晚开始时间（09:25 竞价定格后才抓）。
THEME_START = dtime(9, 25)


def _has_pool(day: str) -> bool:
    """当日涨停池留档是否存在（明文或压缩归档都算）。"""
    from backend import archive
    # 传入模块级 LIVE（测试会 monkeypatch 它指到临时目录），保持隔离语义。
    p = archive.pools_path(day, root=LIVE)
    return p is not None and p.stat().st_size > 0


def _listed_days() -> list[str]:
    if not LIVE.is_dir():
        return []
    return sorted(p.name for p in LIVE.iterdir()
                  if p.is_dir() and p.name.isdigit() and len(p.name) == 8)


def _after_eod(now: datetime) -> bool:
    return (now.hour, now.minute) >= EOD_HHMM


def check(now: datetime | None = None) -> dict[str, Any]:
    """返回当日涨停池留档的健康诊断。"""
    now = now or datetime.now()
    day = now.strftime("%Y%m%d")
    # 允许测试/运维注入日期，方便复现历史场景
    day = os.environ.get("WUKONG_CHECK_DAY") or day

    try:
        from backend import market_clock
        is_td = bool(market_clock.is_trading_day("CN", now=now))
    except Exception:  # noqa: BLE001
        is_td = not (now.weekday() >= 5)   # 降级：仅按周末判断

    days = _listed_days()
    latest = days[-1] if days else None
    today_ok = _has_pool(day)
    eod = _after_eod(now)

    status: str
    message: str
    if today_ok:
        status, message = "ok", f"{day} 涨停池留档已落盘。"
    elif not is_td:
        # 非交易日：没有当日帧是正常的，但要看最近帧是否太旧
        if latest and _gap_days(latest, day) > 5:
            status = "stale"
            message = (f"今日非交易日；最近留档 {latest} 距今天已 "
                       f"{_gap_days(latest, day)} 天，数据可能停更。")
        else:
            status, message = "ok", (
                f"今日非交易日，无当日留档属正常；最近留档 {latest or '无'}。")
    else:  # 交易日且当日无帧
        if eod:
            status = "missing"
            message = (
                f"{day} 是交易日且已过 {EOD_HHMM[0]:02d}:{EOD_HHMM[1]:02d}，"
                f"但涨停池留档未落盘（最近 {latest or '无'}）——"
                "recorder 可能未运行；今日「不开仓」是数据缺失，不代表策略无信号。")
        else:
            status = "intraday"
            message = (
                f"{day} 是交易日、当前盘中（未到 {EOD_HHMM[0]:02d}:{EOD_HHMM[1]:02d}），"
                f"当日留档尚未生成属正常；最近留档 {latest or '无'}。")

    return {
        "status": status,            # ok / intraday / missing / stale
        "ok": status in ("ok", "intraday"),
        "day": day,
        "is_trading_day": is_td,
        "after_eod": eod,
        "today_archive_ok": today_ok,
        "latest_archive_day": latest,
        "archive_days": len(days),
        "themes": theme_check(now=now, is_trading_day=is_td),
        "message": message,
        "note": "诊断只读，不触发采集、不改账本；今日无帧 ≠ 策略无信号。",
    }


def theme_check(now: datetime | None = None, *,
                is_trading_day: bool | None = None,
                path: Path | None = None,
                manifest_path: Path | None = None) -> dict[str, Any]:
    """题材池（同花顺三池）新鲜度。

    单独一项：涨停池有留档 ≠ 题材池有标签。2026-11-24 的事故就是
    前者 ok、后者断更，策略拿不到题材分组而 0 委托。
    """
    now = now or datetime.now()
    day_dash = now.strftime("%Y-%m-%d")
    try:
        from backend import paths
        f = path or (paths.data_dir() / "themes" / "themes.parquet")
        mf = manifest_path or (paths.data_dir() / "themes" / "_manifest.json")
    except Exception:  # noqa: BLE001
        return {"status": "missing", "ok": False,
                "message": "题材池路径不可解析（paths 模块异常）"}
    if not f.is_file():
        return {"status": "missing", "ok": False, "day": day_dash,
                "message": "题材池文件不存在：策略无法按题材分组，会全天 0 委托。"}

    manifest: dict[str, Any] = {}
    if mf.is_file():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            manifest = {}
    latest = max(manifest) if manifest else None
    entry = (manifest.get(latest) or {}) if latest else {}
    fetched_at = entry.get("fetched_at")
    rows = entry.get("rows")
    intraday = entry.get("intraday")

    if is_trading_day is None:
        try:
            from backend import market_clock
            is_trading_day = bool(market_clock.is_trading_day("CN", now=now))
        except Exception:  # noqa: BLE001
            is_trading_day = now.weekday() < 5

    in_session = (is_trading_day and THEME_START <= now.time() <= dtime(*EOD_HHMM))
    fresh_sec: float | None = None
    if fetched_at:
        try:
            fresh_sec = (now - datetime.fromisoformat(str(fetched_at))).total_seconds()
        except (TypeError, ValueError):
            fresh_sec = None

    base = {"day": day_dash, "latest_day": latest, "rows": rows,
            "fetched_at": fetched_at, "intraday": intraday,
            "age_sec": (round(fresh_sec) if fresh_sec is not None else None)}

    if in_session and latest != day_dash:
        return {**base, "status": "stale", "ok": False,
                "message": (f"盘中题材池没有 {day_dash} 的标签（最新 {latest or '无'}）"
                            "—— 策略无法按题材分组，会全天 0 委托。"
                            "检查 recorder 的题材采集线程（同花顺接口）。")}
    if in_session and fresh_sec is not None and fresh_sec > THEME_IDLE_SEC:
        return {**base, "status": "stale", "ok": False,
                "message": (f"题材池已 {int(fresh_sec / 60)} 分钟没有刷新"
                            f"（最后 {fetched_at}）—— 盘中实时采集可能停了。")}
    if is_trading_day and not in_session and latest != day_dash:
        return {**base, "status": "stale", "ok": False,
                "message": (f"{day_dash} 是交易日，但题材池最新只到 {latest or '无'}"
                            "—— 收盘后当日标签未定格，次日选股会缺题材。")}
    return {**base, "status": "ok", "ok": True,
            "message": (f"题材池 {latest} 共 {rows} 行"
                        + (f"，最后抓取 {fetched_at}" if fetched_at else ""))}


def _gap_days(a: str, b: str) -> int:
    """两个 YYYYMMDD 的自然日差（绝对值，仅用于粗略停更提示）。"""
    try:
        da = datetime.strptime(a, "%Y%m%d")
        db = datetime.strptime(b, "%Y%m%d")
        return abs((db - da).days)
    except (ValueError, TypeError):
        return 0


if __name__ == "__main__":
    import json
    print(json.dumps(check(), ensure_ascii=False, indent=2))
