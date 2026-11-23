"""个股分时 → 策略决策输入（2026-11-15 用户明确：分时是给策略决策用的）。

为什么不是"看图工具"
====================
用户原话：「是为了给策略决策用的」。

因此本模块**不提供画图数据**，只产出**策略可以直接消费的判定**：
  - E5 需要：首次封板时刻、开板次数、最终封板时刻（决定排板能否成交）

日线 OHLC 无法表达这件事，这是分时存在的**唯一理由**。

数据源
======
东财 `push2his /api/qt/stock/trends2/get`，实测可取个股 241 个 1 分钟点
（字段：时间,开,收,高,低,量,额,均价）。

诚实边界
========
1. 分时是**当日实时**数据，盘后拉取只能拿到「最近交易日」的；
   想留历史必须自己存（本模块提供 `save_snapshot`）。
2. 腾讯/东财的 1 分钟点在我方无逐笔授权，**开板时刻是近似值**
   （以分钟级价格离开涨停价判定，不是逐笔）。
3. 取不到就返回 `available: False`，**不猜**。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "data" / "intraday"

EM_TRENDS = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _secid(code: str) -> str:
    c = str(code).zfill(6)
    return f"1.{c}" if c.startswith(("6", "9")) else f"0.{c}"


def fetch(code: str, *, ndays: int = 1, timeout: float = 8.0) -> dict[str, Any]:
    """拉取个股分时（最近交易日）。返回原始点 + 解析后的 DataFrame 化 dict。"""
    params = {
        "secid": _secid(code),
        "fields1": "f1,f2,f3,f4,f5",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "iscr": "0", "ndays": str(ndays),
    }
    try:
        r = requests.get(EM_TRENDS, params=params, timeout=timeout, headers=_UA)
        r.raise_for_status()
        j = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "code": code,
                "reason": f"分时拉取失败：{type(exc).__name__}: {exc}"}
    data = j.get("data") or {}
    trends = data.get("trends") or []
    if not trends:
        return {"available": False, "code": code, "reason": "接口空返回"}
    pts = []
    for line in trends:
        parts = str(line).split(",")
        if len(parts) < 8:
            continue
        try:
            pts.append({
                "t": parts[0][-5:],                    # HH:MM
                "date": parts[0][:10],
                "open": float(parts[1]), "close": float(parts[2]),
                "high": float(parts[3]), "low": float(parts[4]),
                "vol": float(parts[5]), "amt": float(parts[6]),
                "avg": float(parts[7]),
            })
        except (ValueError, IndexError):
            continue
    if not pts:
        return {"available": False, "code": code, "reason": "解析后无有效点"}
    return {
        "available": True, "code": code,
        "name": data.get("name"),
        "pre_close": data.get("preClose"),
        "limit_up": data.get("f55") if data.get("f55") else None,
        "n": len(pts), "points": pts,
        "date": pts[0]["date"],
        "source": "eastmoney push2his trends2（1 分钟）",
    }


# ---------------------------------------------------------------------------
# 悟空真经直接消费的判定
# ---------------------------------------------------------------------------


def judge_b1(points: list[dict[str, Any]], limit_up: float) -> dict[str, Any]:
    """E5 悟空二板：首次封板时刻 / 开板次数 / 最终封板时刻。

    这三项直接决定「排板能否成交」（Q037：开板才有成交机会）。
    ⚠️ 分钟级近似：以「当分钟收盘价 == 涨停价」近似「该分钟处于封板状态」。
    """
    if not points or limit_up <= 0:
        return {"available": False, "reason": "无分时或涨停价缺失"}
    tol = 0.011
    sealed_flags = [abs(p["close"] - limit_up) <= tol for p in points]
    if not any(sealed_flags):
        return {"available": True, "ever_sealed": False,
                "reason": "全天未封板"}
    first_idx = sealed_flags.index(True)
    last_idx = len(sealed_flags) - 1 - sealed_flags[::-1].index(True)
    # 开板次数 = 封板 -> 非封板 的切换次数
    opens = 0
    for i in range(1, len(sealed_flags)):
        if sealed_flags[i - 1] and not sealed_flags[i]:
            opens += 1
    return {
        "available": True, "ever_sealed": True,
        "first_seal": points[first_idx]["t"],
        "last_seal": points[last_idx]["t"],
        "open_times": opens,
        "sealed_ratio": round(sum(sealed_flags) / len(sealed_flags), 3),
        "can_fill": opens > 0,
        "note": (f"首封 {points[first_idx]['t']}，末封 {points[last_idx]['t']}，"
                 f"开板 {opens} 次；"
                 + ("开板过 → 按 Q037 排板有成交机会" if opens > 0
                    else "全天未开板 → 排板排不到")),
        "basis": "E5 悟空二板·二板封板瞬间排板（Q037）",
        "approx": "以分钟级收盘价近似封板状态，非逐笔",
    }


