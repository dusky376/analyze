# -*- coding: utf-8 -*-
"""个股分钟成交额（腾讯），用于精确计算开板期间成交量（2026-11-16）。

## 为什么需要

排板消耗模型的分子是「开板期间成交额」。原来只能拿**累计换手差分**近似：
```
本轮成交额 ≈ 当日成交额 × (换手增量 / 当日总换手)
```
误差来源：换手是累计值、帧间隔不均、且炸板不回封时只能拿收盘值近似。

现在改用**逐分钟真实成交额**：
```
腾讯 proxy.finance.qq.com/.../minute/query?code=sz002685
格式： "HHMM 价格 累计成交量 累计成交额"
本轮成交额 = 累计成交额(开板结束) − 累计成交额(开板前)
```

## 实测准确性（2026-11-16 校验）

| 股票 | 分钟末累计 | 日线成交额 | 偏差 |
|---|---|---|---|
| 002685 | 8.6129 亿 | 8.6128 亿 | +0.00% |
| 000819 | 8.1986 亿 | 8.1980 亿 | +0.01% |

与日线完全对得上，可以放心当分子。

## 边界

- 拿不到分钟数据（停牌/接口异常/北交所代码格式不同）→ 返回 None，
  调用方退回换手差分近似（并标注 approx）。
- 数据只做当日，不补历史；历史回放仍用换手差分。
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

_BASE = ("http://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query"
         "?code={sym}")

#: (code, day) -> [(HHMM, price, cum_amount_yuan)] | None
_CACHE: dict[tuple[str, str], list[tuple[str, float, float]] | None] = {}


def _symbol(code: str) -> str:
    c = str(code).strip().zfill(6)
    if c[0] in "69":
        return f"sh{c}"
    if c[0] in "03":
        return f"sz{c}"
    if c[0] in "48":
        return f"bj{c}"
    return f"sz{c}"


def minute_series(code: str, day: str) -> list[tuple[str, float, float]] | None:
    """当日分钟序列 [(HHMM, price, 累计成交额元)]；取不到返回 None。"""
    key = (str(code).zfill(6), str(day))
    if key in _CACHE:
        return _CACHE[key]
    out: list[tuple[str, float, float]] | None = None
    try:
        sym = _symbol(code)
        req = urllib.request.Request(_BASE.format(sym=sym),
                                     headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8",
                                                                   "replace")
        d = json.loads(raw)
        node = (d.get("data") or {}).get(sym) or {}
        arr = ((node.get("data") or {}).get("data")) or []
        # 校验日期，避免拿到别的交易日的分钟数据
        date = str(((node.get("data") or {}).get("date")) or "")
        want = str(day).replace("-", "")
        if date and want and date[:8] != want[:8]:
            out = None
        else:
            s: list[tuple[str, float, float]] = []
            for line in arr:
                p = str(line).split()
                if len(p) >= 4:
                    try:
                        s.append((p[0], float(p[1]), float(p[3])))
                    except (TypeError, ValueError):
                        continue
            out = s or None
    except Exception:  # noqa: BLE001
        out = None
    _CACHE[key] = out
    return out


def _hhmm_of(ts: str) -> str | None:
    """'2026-11-16T10:51:25' → '1051'。"""
    s = str(ts or "")
    if "T" in s:
        s = s.split("T", 1)[1]
    s = s.replace(":", "")
    if len(s) >= 4 and s[:4].isdigit():
        return s[:4]
    return None


def amount_between(code: str, day: str,
                   t_from: str, t_to: str) -> dict[str, Any]:
    """[t_from, t_to] 区间的真实成交额（元）。

    t_from/t_to 可以是 '2026-11-16T10:51:25' 或 '1051'。
    返回 {ok, amount, from_hhmm, to_hhmm, n_points}
    """
    a = _hhmm_of(t_from)
    b = _hhmm_of(t_to)
    if not a or not b:
        return {"ok": False, "reason": "时刻无法解析"}
    s = minute_series(code, day)
    if not s:
        return {"ok": False, "reason": "无分钟数据"}
    cum = {t: amt for t, _px, amt in s}
    # 取「不早于」目标时刻的第一条，避免刚好缺该分钟
    def pick(target: str) -> float | None:
        if target in cum:
            return cum[target]
        for t, _px, amt in s:
            if t >= target:
                return amt
        return None
    v0 = pick(a)
    v1 = pick(b)
    if v0 is None or v1 is None:
        return {"ok": False, "reason": "区间端点在分钟序列外"}
    return {"ok": True, "amount": max(0.0, v1 - v0),
            "from_hhmm": a, "to_hhmm": b, "n_points": len(s),
            "cum_from": v0, "cum_to": v1}
