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
import time as _time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from backend import paths

_BASE = ("http://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query"
         "?code={sym}")
_EM_TRENDS = (
    "https://push2.eastmoney.com/api/qt/stock/trends2/get"
    "?fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
    "&iscr=0&ndays=1&secid={secid}"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

#: (code, day) -> (monotonic_ts, [(HHMM, price, cum_amount_yuan)] | None)
#: 历史日永久缓存；当日使用短 TTL（避免早盘 09:30 调用后整天冻结）。
_CACHE: dict[tuple[str, str], tuple[float, list[tuple[str, float, float]] | None]] = {}

#: 盘中实时分时的短缓存：(code, day) -> (monotonic_ts, points)
_LIVE_TTL = 8.0
_LIVE_CACHE: dict[tuple[str, str],
                  tuple[float, list[dict[str, Any]] | None]] = {}


def _symbol(code: str) -> str:
    """6 位代码 -> 腾讯 symbol（显式处理北交所 920 新段，与 feeds.tx_symbol 对齐）。"""
    c = str(code).strip().zfill(6)
    if c.startswith(("43", "83", "87", "92")):
        return f"bj{c}"
    if c.startswith(("6", "9")):
        return f"sh{c}"
    if c.startswith(("0", "3")):
        return f"sz{c}"
    return f"bj{c}"


def _em_secid(code: str) -> str:
    """6 位代码 -> 东方财富 Push2 secid（1=沪市，0=深市/北交所）。"""
    c = str(code).strip().zfill(6)
    if c.startswith(("43", "83", "87", "92")):
        return f"0.{c}"
    if c.startswith(("6", "9")):
        return f"1.{c}"
    return f"0.{c}"


def _today8() -> str:
    try:
        from backend import market_clock
        tz = market_clock.TZ_CN
        return (datetime.now(tz) if tz else datetime.now()).strftime("%Y%m%d")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y%m%d")


def _fetch_em_trends(code: str, day: str) -> list[dict[str, Any]] | None:
    """东方财富 Push2 1 分钟真实分时（含均价线 VWAP + 每分钟成交量/成交额）。"""
    secid = _em_secid(code)
    want = str(day).replace("-", "")[:8]
    try:
        req = urllib.request.Request(_EM_TRENDS.format(secid=secid), headers=_HEADERS)
        raw = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace")
        d = json.loads(raw)
        data = d.get("data") or {}
        trends = data.get("trends") or []
        if not trends:
            return None
        pre_close = data.get("preClose")
        pts: list[dict[str, Any]] = []
        cum_amt = 0.0
        for line in trends:
            # 格式: "2026-11-25 09:31,open,close,high,low,vol_hand,amount,avg_price"
            p = str(line).split(",")
            if len(p) < 8:
                continue
            dt_str = p[0].strip()
            if len(dt_str) >= 10:
                got_day = dt_str[:10].replace("-", "")
                if want and got_day != want:
                    return None
            hhmm = dt_str[11:16] if len(dt_str) >= 16 else ""
            if len(hhmm) != 5:
                continue
            try:
                px = float(p[2])
                vol = float(p[5])
                amt = float(p[6])
                avg_px = float(p[7])
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            cum_amt += max(0.0, amt)
            pts.append({
                "t": hhmm,
                "price": px,
                "avg_price": avg_px if avg_px > 0 else None,
                "volume": vol,
                "amount": amt,
                "cum_amount": round(cum_amt, 2),
                "prev_close": float(pre_close) if pre_close else None,
                "source": "eastmoney_1min",
            })
        return pts or None
    except Exception:  # noqa: BLE001
        return None


def _fetch(code: str, day: str) -> list[tuple[str, float, float]] | None:
    """拉一次腾讯分钟接口；日期不匹配返回 None（不拿别的交易日冒充）。"""
    sym = _symbol(code)
    try:
        req = urllib.request.Request(_BASE.format(sym=sym),
                                     headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8",
                                                                   "replace")
        d = json.loads(raw)
        node = (d.get("data") or {}).get(sym) or {}
        arr = ((node.get("data") or {}).get("data")) or []
        # 校验日期，避免拿到别的交易日的分钟数据
        date = str(((node.get("data") or {}).get("date")) or "")
        want = str(day).replace("-", "")
        if date and want and date[:8] != want[:8]:
            return None
        s: list[tuple[str, float, float]] = []
        for line in arr:
            p = str(line).split()
            if len(p) >= 4:
                try:
                    s.append((p[0], float(p[1]), float(p[3])))
                except (TypeError, ValueError):
                    continue
        return s or None
    except Exception:  # noqa: BLE001
        return None


def minute_archive_path(code: str, day: str) -> Path:
    """盘中 1 分钟落盘路径：data/live/<YYYYMMDD>/minute/<code>.parquet。"""
    d = str(day).replace("-", "")[:8]
    c = str(code).strip().zfill(6)
    return paths.live_dir() / d / "minute" / f"{c}.parquet"


def persist_minute_series(code: str, day: str,
                          points: list[dict[str, Any]]) -> Path | None:
    """原子落盘 1 分钟真实序列，确保事后卖点/排板验证 100% 可复现。"""
    if not points:
        return None
    p = minute_archive_path(code, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for x in points:
        rows.append({
            "t": str(x.get("t") or ""),
            "price": float(x["price"]) if x.get("price") is not None else None,
            "avg_price": float(x["avg_price"]) if x.get("avg_price") is not None else None,
            "volume": float(x["volume"]) if x.get("volume") is not None else None,
            "amount": float(x["amount"]) if x.get("amount") is not None else None,
            "cum_amount": float(x["cum_amount"]) if x.get("cum_amount") is not None else None,
            "source": str(x.get("source") or "unknown"),
        })
    df = pl.DataFrame(rows)
    tmp = p.with_name(p.stem + ".tmp.parquet")
    df.write_parquet(tmp, compression="zstd")
    tmp.replace(p)
    return p


def load_persisted_minute(code: str, day: str) -> list[dict[str, Any]] | None:
    """读取已落盘的 1 分钟真实序列；无文件返回 None。"""
    p = minute_archive_path(code, day)
    if not p.is_file():
        return None
    try:
        df = pl.read_parquet(p)
        if df.height == 0:
            return None
        return df.to_dicts()
    except Exception:  # noqa: BLE001
        return None


def minute_series(code: str, day: str) -> list[tuple[str, float, float]] | None:
    """当日分钟序列 [(HHMM, price, 累计成交额元)]；取不到返回 None。"""
    d = str(day).replace("-", "")[:8]
    key = (str(code).zfill(6), d)
    now = _time.monotonic()
    hit = _CACHE.get(key)
    is_today = d == _today8()
    if hit is not None:
        if not is_today or (now - hit[0] < _LIVE_TTL):
            return hit[1]
    out = _fetch(code, d)
    if not out:
        # 腾讯失败时尝试东财 Push2 1 分钟真实源
        em = _fetch_em_trends(code, d)
        if em:
            out = [(p["t"].replace(":", ""), p["price"], p["cum_amount"])
                   for p in em if p.get("t") and p.get("price")]
    if not out:
        # 历史日回退已落盘的 1 分钟文件
        disk = load_persisted_minute(code, d)
        if disk:
            out = [(str(p["t"]).replace(":", ""), float(p["price"]),
                    float(p.get("cum_amount") or 0.0))
                   for p in disk if p.get("t") and p.get("price")]
    _CACHE[key] = (now, out)
    return out


def live_series(code: str, day: str, *,
                ttl: float | None = None,
                persist: bool = False) -> list[dict[str, Any]] | None:
    """盘中实时分时（1 分钟粒度），带短 TTL 缓存。

    优先东方财富 Push2 1 分钟（自带真实 VWAP 均价线 + 成交量），
    失败时自动回落腾讯 1 分钟，再回落本地已落盘 1 分钟文件。
    """
    d = str(day).replace("-", "")[:8]
    c = str(code).zfill(6)
    key = (c, d)
    now = _time.monotonic()
    hit = _LIVE_CACHE.get(key)
    if hit is not None and now - hit[0] < (ttl if ttl is not None else _LIVE_TTL):
        return hit[1]

    pts = _fetch_em_trends(c, d)
    if not pts:
        raw = _fetch(c, d)
        if raw:
            pts = []
            prev_amt = 0.0
            for t, px, amt in raw:
                if len(t) != 4 or not t.isdigit():
                    continue
                delta_amt = max(0.0, amt - prev_amt)
                prev_amt = amt
                pts.append({
                    "t": f"{t[:2]}:{t[2:]}",
                    "price": px,
                    "avg_price": None,
                    "volume": None,
                    "amount": round(delta_amt, 2),
                    "cum_amount": amt,
                    "source": "tencent_1min",
                })
    if not pts:
        pts = load_persisted_minute(c, d)
    elif persist:
        try:
            persist_minute_series(c, d, pts)
        except Exception:  # noqa: BLE001
            pass

    _LIVE_CACHE[key] = (now, pts)
    return pts


def _reset_live_cache() -> None:
    """测试/强制刷新用：清空盘中短缓存（不影响历史永久缓存）。"""
    _LIVE_CACHE.clear()


def clear_caches() -> None:
    """清空全部内存缓存（供 /api/refresh 调用）。"""
    _LIVE_CACHE.clear()
    _CACHE.clear()


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
