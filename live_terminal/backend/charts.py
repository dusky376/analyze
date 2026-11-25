"""个股图表数据（只读）：K 线 + 分时。供前端画布渲染，无外部依赖。

数据源（全部真实数据，不合成日线）：
  * K 线：``feeds.build_panel()``（panel_raw + panel_live 合并视图），
    取最近 N 个交易日 OHLCV；**当日未进面板时**用实时行情合成一根当日 bar
    （open/high/low/price 都来自行情快照，标 live=True + as_of）。
  * 分时：**优先腾讯 1 分钟分时**（``minute_flow.live_series``，盘中实时）；
    取不到时回落到 ``data/live/<day>/quotes/quotes_*.parquet`` 的 5 分钟留档快照。
    两者都如实标注粒度，不插值。

2026-11-25 用户裁定：「k线不实时，盘中也没有实时显示，还是前一天的k线」。
根因是当日 bar 只在 `market_clock.is_cn_trading()` 为真时才补 —— 午休、
09:25–09:30、数据源慢一秒都会让最后一根退回昨天。现在改为「只要行情是今天的
就补」，并用分钟级分时替代 5 分钟快照点。

不写任何文件、不触发采集。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from backend import feeds

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "data" / "live"

#: 行情快照超过这么久仍显示，但不再标「实时」（如午休 11:30–13:00）。
LIVE_STALE_SEC = 180.0


def _compact(day: str) -> str:
    return str(day).replace("-", "").replace("/", "")[:8]


def _cn_today() -> str:
    """A 股口径的今天（YYYYMMDD）；时钟不可用时退回本机日期。"""
    try:
        from backend import market_clock
        tz = market_clock.TZ_CN
        return (datetime.now(tz) if tz else datetime.now()).strftime("%Y%m%d")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y%m%d")


def _quote_age_sec(qt: Any, now: datetime | None = None) -> float | None:
    """行情时点距今秒数；解析失败返回 None。"""
    s = str(qt or "")
    if len(s) < 14 or not s[:14].isdigit():
        return None
    try:
        t = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    now = now or datetime.now()
    return max(0.0, (now - t).total_seconds())


def _live_bar_for(code: str,
                  quotes: pl.DataFrame | None = None) -> dict[str, Any] | None:
    """当日实时 bar（只读内存合成，不写盘）。

    用户 2026-11-25 裁定后：**只要行情快照是今天的**就合成当日 bar，
    不再要求 `market_clock.is_cn_trading()` —— 午休/竞价/收盘瞬间也要看到今天。
    `live=True` 仅表示快照足够新（≤3 分钟）；午休时仍显示今天，但标 as_of。
    """
    try:
        q = quotes if quotes is not None else feeds.all_quotes(force=True)
        if q is None or q.height == 0:
            return None
        r = q.filter(pl.col("ts_code") == str(code).zfill(6))
        if r.height == 0:
            return None
        d = r.to_dicts()[0]
        qt = str(d.get("quote_time") or "")
        today = _cn_today()
        if qt[:8] != today:
            return None                      # 行情不是今天的 → 不冒充
        px = d.get("price")
        op = d.get("open")
        if px is None or op is None or float(px) <= 0 or float(op) <= 0:
            return None                      # 竞价未定格（open=0）→ 不画半根假 bar
        age = _quote_age_sec(qt)
        snap = (f"{qt[:4]}-{qt[4:6]}-{qt[6:8]}T{qt[8:10]}:{qt[10:12]}:{qt[12:14]}")
        return {
            "date": f"{today[:4]}-{today[4:6]}-{today[6:]}",
            "open": float(op),
            "high": float(d["high"]) if d.get("high") else float(px),
            "low": float(d["low"]) if d.get("low") else float(px),
            "close": float(px),
            "volume": float(d["volume"]) if d.get("volume") else None,
            "pct": float(d["pct"]) if d.get("pct") is not None else None,
            "prev_close": float(d["prev_close"]) if d.get("prev_close") else None,
            "live": bool(age is not None and age <= LIVE_STALE_SEC),
            "as_of": snap,
            "source": "live_quotes",
        }
    except Exception:  # noqa: BLE001 - 盘中合成失败不影响已有日线
        return None


def _quotes_bar_for(code: str, day: str) -> dict[str, Any] | None:
    """用当日 quotes 最新帧合成当日 bar（盘中/收盘后都能用）。

    2026-11-24 实测：panel_live 只到 11-23，今天的真实日线 bar 从未写进
    面板 → K 线停在昨天（如横店影视显示 16.60，实际今日 18.26 涨停）。
    quotes 最新帧含 price/open/high/low/prev_close/volume/amount，
    足以合成一根真实当日 bar。这里**只读内存合成，不写任何 parquet**
    （历史上把半日 bar 写进面板曾造成假收益污染）。
    """
    d = _compact(day)
    qdir = ROOT / "data" / "live" / d / "quotes"
    snaps = sorted(qdir.glob("quotes_*.parquet"))
    if not snaps:
        return None
    try:
        df = pl.read_parquet(snaps[-1])
        r = df.filter(pl.col("ts_code") == str(code).zfill(6))
        if r.height == 0:
            return None
        row = r.to_dicts()[0]
        px = row.get("price")
        if px is None or px <= 0:
            return None
        # 盘中合成 bar：以快照的 open/high/low/price 作为当日 OHLC
        return {
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
            "open": float(row["open"]) if row.get("open") else float(px),
            "high": float(row["high"]) if row.get("high") else float(px),
            "low": float(row["low"]) if row.get("low") else float(px),
            "close": float(px),
            "volume": float(row["volume"]) if row.get("volume") else None,
            "pct": float(row["pct"]) if row.get("pct") is not None else None,
            "prev_close": float(row["prev_close"]) if row.get("prev_close") else None,
            "live": False,          # 真实快照合成，非盘中 streaming
            "source": "quotes_archive",
        }
    except Exception:  # noqa: BLE001
        return None


def kline(code: str, *, days: int = 40,
          panel: pl.DataFrame | None = None,
          quotes: pl.DataFrame | None = None) -> dict[str, Any]:
    """最近 N 个交易日 K 线（OHLCV）——**盘中最后一根是当日实时 bar**。

    盘中（含午休/竞价）：`panel` 尚无当日行（panel_live 15:05 后才写），
    这里用实时行情合成当日 bar，标 `live=True` + `as_of`，前端轮询即刷新。
    收盘后：若面板已含当日定格 bar，直接使用；否则仍用行情快照合成
    （标 live=False，source=live_quotes），避免 K 线停在昨天。
    """
    p = panel if panel is not None else feeds.build_panel()
    c = str(code).zfill(6)
    df = (p.filter(pl.col("ts_code") == c)
          .sort("trade_date")
          .tail(days))
    bars = []
    for r in df.iter_rows(named=True):
        bars.append({
            "date": str(r.get("trade_date")),
            "open": float(r["open"]) if r.get("open") is not None else None,
            "high": float(r["high"]) if r.get("high") is not None else None,
            "low": float(r["low"]) if r.get("low") is not None else None,
            "close": float(r["close"]) if r.get("close") is not None else None,
            "volume": float(r["volume"]) if r.get("volume") is not None else None,
            "pct": float(r["pct_chg"]) if r.get("pct_chg") is not None else None,
        })

    # 当日实时 bar：只要行情是今天的就补（不再等 15:05 / 不等交易时段开关）
    live = _live_bar_for(c, quotes=quotes)
    if live is not None:
        if bars and bars[-1]["date"] == live["date"]:
            bars[-1] = live
        else:
            bars.append(live)
            bars = bars[-days:]
    else:
        # 行情拿不到时，退回当日 quotes 留档合成（收盘后/数据源故障兜底）。
        # 注意：**从新到旧逐个试**，直到补上一根。今天的目录可能已建但还没帧
        # （09:15–09:26），只试最新目录会把昨天的收盘 bar 也漏掉（K 线退到前天）。
        try:
            qdirs = sorted((x.name for x in LIVE.iterdir()
                            if x.is_dir() and x.name.isdigit()
                            and (x / "quotes").is_dir()), reverse=True)
            for qd in qdirs[:8]:
                q_dash = f"{qd[:4]}-{qd[4:6]}-{qd[6:]}"
                if bars and bars[-1]["date"] >= q_dash:
                    continue
                qb = _quotes_bar_for(c, qd)
                if qb is not None:
                    bars.append(qb)
                    bars = bars[-days:]
                    break
        except Exception:  # noqa: BLE001
            pass

    if not bars:
        return {"available": False, "code": c, "reason": "面板无该股日线", "bars": []}

    n_live = sum(1 for b in bars if b.get("live"))
    last = bars[-1]
    as_of = last.get("as_of")
    today_bar = last.get("date") == f"{_cn_today()[:4]}-{_cn_today()[4:6]}-{_cn_today()[6:]}"
    return {
        "available": True,
        "code": c,
        "name": None,
        "bars": bars,
        "live": n_live > 0,
        "as_of": as_of,
        "today_included": bool(today_bar),
        "note": (f"最近 {len(bars)} 个交易日真实日线（不复权）；"
                 + (f"最后一根为当日实时 bar（as_of {as_of}），前端轮询自动刷新。"
                    if n_live else
                    f"最后一根 {last.get('date')} 为当日行情快照（as_of {as_of}），"
                    "收盘后以日线为准。" if today_bar else
                    "已收盘，当日 bar 已定格。")),
    }


def _archive_points(code: str, day: str) -> tuple[list[dict[str, Any]], str]:
    """从 quotes 留档帧抽 (时刻, 价格) 序列（5 分钟一帧）。"""
    d = _compact(day)
    qdir = LIVE / d / "quotes"
    snaps = sorted(qdir.glob("quotes_*.parquet"))
    points: list[dict[str, Any]] = []
    for f in snaps:
        stem = f.stem.replace("quotes_", "")
        if len(stem) == 6:                    # quotes_092622.parquet → HHMMSS
            hhmmss = f"{stem[:2]}:{stem[2:4]}:{stem[4:6]}"
        elif len(stem) >= 14:                 # quotes_20261124092622.parquet
            hhmmss = f"{stem[8:10]}:{stem[10:12]}:{stem[12:14]}"
        else:
            continue
        df = pl.read_parquet(f, columns=["ts_code", "price", "prev_close"])
        r = df.filter(pl.col("ts_code") == str(code).zfill(6))
        if r.height:
            px = r["price"][0]
            if px is not None:
                pc = r["prev_close"][0] if "prev_close" in r.columns else None
                points.append({
                    "t": hhmmss,
                    "price": float(px),
                    "prev_close": float(pc) if pc is not None else None,
                })
    points.sort(key=lambda x: x["t"])
    return points, d


def intraday(code: str, day: str | None = None, *,
             quotes: pl.DataFrame | None = None) -> dict[str, Any]:
    """当日分时。

    优先级（2026-11-25 用户裁定「要当天实时分时图」）：
      1. 目标日 == 今天 → 腾讯 1 分钟分时（``minute_flow.live_series``，8 秒 TTL）；
      2. 目标日 == 今天但接口失败 → 当日 quotes 留档快照（5 分钟一点）；
      3. 历史日 → quotes 留档快照。

    Returns:
        {"available", "code", "day", "points": [{"t","price"}], "prev_close",
         "granularity", "source", "live", "as_of", "note"}
    """
    c = str(code).zfill(6)
    today8 = _cn_today()
    if not day:
        # 有今天的分钟数据就用今天；否则找最近有 quotes 留档的交易日。
        dirs = sorted((x.name for x in LIVE.iterdir()
                       if x.is_dir() and x.name.isdigit()
                       and (x / "quotes").is_dir()), reverse=True)
        day = today8 if today8 in dirs else (dirs[0] if dirs else None)
        if day is None:
            return {"available": False, "code": c, "reason": "无分时留档",
                    "points": []}
    d = _compact(day)
    is_today = d == today8

    points: list[dict[str, Any]] = []
    source = "quotes_archive"
    granularity = "5min"
    if is_today:
        try:
            from backend import minute_flow
            got = minute_flow.live_series(c, d)
            if got:
                points = [{"t": p["t"], "price": p["price"]} for p in got]
                source = "tencent_1min"
                granularity = "1min"
        except Exception:  # noqa: BLE001 - 接口失败落回留档快照
            points = []

    if not points:
        points, d = _archive_points(c, d)
        if not points and is_today:
            # 今天还没有行情帧（如 09:15 前）→ 明确说没有，不画昨天的冒充今天
            return {"available": False, "code": c, "day": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                    "reason": f"{d} 尚无分时数据（盘前或采集未开始）",
                    "points": [], "granularity": granularity, "source": source}

    # 昨收：优先实时行情（最可靠），其次留档帧里的 prev_close
    prev_close = None
    try:
        q = quotes if quotes is not None else feeds.all_quotes(force=True)
        r = q.filter(pl.col("ts_code") == c)
        if r.height:
            pc = r["prev_close"][0]
            prev_close = float(pc) if pc else None
    except Exception:  # noqa: BLE001
        prev_close = None
    if prev_close is None:
        prev_close = next((p["prev_close"] for p in points
                           if p.get("prev_close") and p["prev_close"] > 0), None)

    points.sort(key=lambda x: x["t"])
    last_t = points[-1]["t"] if points else None
    fresh = False
    if is_today and last_t:
        try:
            now_hm = datetime.now().strftime("%H:%M")
            hh, mm = (int(x) for x in last_t[:5].split(":"))
            nhh, nmm = (int(x) for x in now_hm.split(":"))
            fresh = (nhh * 60 + nmm) - (hh * 60 + mm) <= 5
        except Exception:  # noqa: BLE001
            fresh = False

    if source == "tencent_1min":
        note = ("腾讯 1 分钟分时（盘中实时，约 8 秒刷新）；"
                "价格为该分钟最后成交价。")
    else:
        note = (f"当日 {len(points)} 个 5 分钟留档快照点；"
                "非逐笔，连线只示趋势。")

    return {
        "available": bool(points),
        "code": c,
        "day": f"{d[:4]}-{d[4:6]}-{d[6:]}",
        "points": points,
        "n": len(points),
        "prev_close": prev_close,
        "live": bool(is_today and fresh and source == "tencent_1min"),
        "as_of": last_t,
        "source": source,
        "granularity": granularity,
        "note": note,
    }


def stock_chart(code: str, *, day: str | None = None,
                kdays: int = 40) -> dict[str, Any]:
    """K 线 + 分时合并输出（前端一次请求就能画两图）。"""
    # 一次请求只拉一次全市场快照，K 线与分时共用（否则 5580 行拉两遍）
    try:
        q = feeds.all_quotes(force=True)
    except Exception:  # noqa: BLE001
        q = None
    k = kline(code, days=kdays, quotes=q)
    i = intraday(code, day=day, quotes=q)
    return {
        "available": k["available"] or i["available"],
        "code": str(code).zfill(6),
        "kline": k,
        "intraday": i,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    import json
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "002931"
    day = sys.argv[2] if len(sys.argv) > 2 else None
    r = stock_chart(code, day=day)
    print(f"code={r['code']} kline bars={len(r['kline']['bars'])} "
          f"intraday points={r['intraday'].get('n')}")
