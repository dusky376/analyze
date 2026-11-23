"""数据完整性自检（只读诊断，不改任何数据/买卖点）。

回答一个问题：**今天该有的涨停池留档，落盘了吗？**

背景：悟空真经的选股/成交判定完全依赖 `data/live/<day>/pools/pools.jsonl`。
若采集器当天没跑，sync 会诚实地返回「今日不开仓」，但用户容易把它误读成
「策略没信号」。本模块把「数据没到」与「策略没信号」区分开，显式报警。

判定（当日 = 本地今天）：
  - missing     当日非交易日但无帧        → 正常（周末/节假日不该有）
  - missing     当日是交易日且盘后(≥15:05)无帧 → 警告（采集器可能没跑）
  - stale       有帧但最新留档日 < 上一交易日 → 警告（数据过期）
  - ok          当日是交易日且当日有帧 / 非交易日有最近帧
  - intraday    当日交易日且盘中(<15:05)无帧 → 提示（尚未收盘，正常）

只出诊断字段，绝不写文件、不触发采集、不改账本。
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "data" / "live"

#: 盘后阈值（本地时间）；15:05 之后应当已有当日涨停池留档。
EOD_HHMM = (15, 5)


def _has_pool(day: str) -> bool:
    f = LIVE / day / "pools" / "pools.jsonl"
    return f.is_file() and f.stat().st_size > 0


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
        "message": message,
        "note": "诊断只读，不触发采集、不改账本；今日无帧 ≠ 策略无信号。",
    }


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
