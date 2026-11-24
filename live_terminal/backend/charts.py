"""个股图表数据（只读）：K 线 + 分时。供前端画布渲染，无外部依赖。

数据源（全部真实落盘，不合成不插值）：
  * K 线：``feeds.build_panel()``（panel_raw + panel_live 合并视图），
    取最近 N 个交易日的 OHLCV。
  * 分时：``data/live/<day>/quotes/quotes_*.parquet`` —— recorder 每 5 分钟
    落一帧全市场快照，按 code 抽出 (时刻, 价格) 序列；早盘另有 auction 帧。
    注意：这是**离散快照**（约 5 分钟一点），不是逐笔，前端要如实标注。

不写任何文件、不触发采集。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from backend import feeds

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "data" / "live"


def _compact(day: str) -> str:
    return str(day).replace("-", "").replace("/", "")[:8]


def _live_bar_for(code: str) -> dict[str, Any] | None:
    """盘中实时 bar（内存合成，不写盘）。

    `feeds.intraday_bar()` 只在交易时段返回；非交易时段（如收盘后）
    返回 None —— 此时面板已含当日定格 bar，K 线直接用面板数据即可。
    """
    try:
        bar, snap = feeds.intraday_bar(force=True)
        if bar is None or bar.height == 0:
            return None
        r = bar.filter(pl.col("ts_code") == str(code).zfill(6))
        if r.height == 0:
            return None
        d = r.to_dicts()[0]
        return {
            "date": str(d.get("trade_date")),
            "open": float(d["open"]) if d.get("open") is not None else None,
            "high": float(d["high"]) if d.get("high") is not None else None,
            "low": float(d["low"]) if d.get("low") is not None else None,
            "close": float(d["close"]) if d.get("close") is not None else None,
            "volume": float(d["volume"]) if d.get("volume") is not None else None,
            "pct": float(d["pct_chg"]) if d.get("pct_chg") is not None else None,
            "live": True,
            "as_of": snap,
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
          panel: pl.DataFrame | None = None) -> dict[str, Any]:
    """最近 N 个交易日 K 线（OHLCV）——**盘中最后一根是实时合成 bar**。

    盘中（A股交易时段）：`panel` 尚无当日行（panel_live 15:05 后才写），
    这里用 `feeds.intraday_bar()` 的内存实时 bar 补最后一根，标 `live=True`
    + `as_of` 时刻，前端每轮轮询都会拿到更新后的价。
    收盘后：面板已含当日定格 bar，直接用，不使用盘中合成（避免半日 bar）。
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

    # 盘中：补/替换最后一根为实时 bar（仅当面板还没有今天）
    live = _live_bar_for(c)
    if live is not None:
        if bars and bars[-1]["date"] == live["date"]:
            bars[-1] = live
        else:
            bars.append(live)
            bars = bars[-days:]
    else:
        # 收盘后但 panel_live 尚未写入当日（实测 11-24 面板只到 11-23）：
        # 用 quotes 最新快照合成当日 bar，避免 K 线停在昨天。
        # 取最近一个有 quotes 留档的交易日；若晚于面板末日，补一根。
        try:
            qdirs = sorted((p.name for p in LIVE.iterdir()
                            if p.is_dir() and p.name.isdigit()
                            and (p / "quotes").is_dir()), reverse=True)
            if qdirs:
                qd = qdirs[0]
                q_dash = f"{qd[:4]}-{qd[4:6]}-{qd[6:]}"
                if (not bars) or bars[-1]["date"] < q_dash:
                    qb = _quotes_bar_for(c, qd)
                    if qb is not None:
                        bars.append(qb)
                        bars = bars[-days:]
        except Exception:  # noqa: BLE001
            pass

    if not bars:
        return {"available": False, "code": c, "reason": "面板无该股日线", "bars": []}

    n_live = sum(1 for b in bars if b.get("live"))
    return {
        "available": True,
        "code": c,
        "name": None,
        "bars": bars,
        "live": n_live > 0,
        "as_of": next((b.get("as_of") for b in bars if b.get("live")), None),
        "note": (f"最近 {len(bars)} 个交易日真实日线（不复权）；"
                 + (f"最后一根为盘中实时 bar（as_of {next((b.get('as_of') for b in bars if b.get('live')), None)}），"
                    "前端每轮轮询自动刷新。" if n_live else
                    "已收盘，当日 bar 已定格。")),
    }


def intraday(code: str, day: str | None = None) -> dict[str, Any]:
    """当日分时（离散快照）。

    Returns:
        {"available", "code", "day", "points": [{"t","price"}], "note"}
    """
    c = str(code).zfill(6)
    if not day:
        # 找最近有 quotes 留档的交易日
        dirs = sorted((p.name for p in LIVE.iterdir()
                       if p.is_dir() and p.name.isdigit()
                       and (p / "quotes").is_dir()), reverse=True)
        if not dirs:
            return {"available": False, "code": c, "reason": "无分时留档", "points": []}
        day = dirs[0]
    d = _compact(day)
    qdir = LIVE / d / "quotes"
    snaps = sorted(qdir.glob("quotes_*.parquet"))
    if not snaps:
        return {"available": False, "code": c, "day": d,
                "reason": f"{d} 无分时快照", "points": []}

    points: list[dict[str, Any]] = []
    try:
        for f in snaps:
            # 文件名 quotes_YYYYMMDDHHMMSS.parquet → 时刻
            stem = f.stem.replace("quotes_", "")
            if len(stem) == 6:                    # quotes_092622.parquet → HHMMSS
                hhmmss = f"{stem[:2]}:{stem[2:4]}:{stem[4:6]}"
            elif len(stem) >= 14:                 # quotes_20261124092622.parquet
                hhmmss = f"{stem[8:10]}:{stem[10:12]}:{stem[12:14]}"
            else:
                continue
            df = pl.read_parquet(f, columns=["ts_code", "price", "prev_close"])
            r = df.filter(pl.col("ts_code") == c)
            if r.height:
                px = r["price"][0]
                if px is not None:
                    pc = r["prev_close"][0] if "prev_close" in r.columns else None
                    points.append({
                        "t": hhmmss,
                        "price": float(px),
                        "prev_close": float(pc) if pc is not None else None,
                    })
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "code": c, "day": d,
                "reason": f"读取失败: {type(exc).__name__}: {exc}", "points": []}

    points.sort(key=lambda x: x["t"])
    # 昨收：分时图的基准线（交易软件的中间虚线）
    prev_close = next((p["prev_close"] for p in points
                       if p.get("prev_close") and p["prev_close"] > 0), None)
    note = (f"当日 {len(points)} 个离散快照点（约 5 分钟一点，"
            "盘中 recorder 留档）；非逐笔，连线只示趋势。")

    # 盘中：把最新实时价（all_quotes）作为一个 point 接在末尾，
    # 让分时图在两次留档帧之间也能看到当前价（每轮轮询刷新）。
    try:
        from backend import market_clock
        if market_clock.is_cn_trading():
            q = feeds.all_quotes(force=True)
            r = q.filter(pl.col("ts_code") == c)
            if r.height:
                d = r.to_dicts()[0]
                px = d.get("price")
                qt = str(d.get("quote_time") or "")
                if px and len(qt) >= 14:
                    t = f"{qt[8:10]}:{qt[10:12]}:{qt[12:14]}"
                    if not points or points[-1]["t"] != t:
                        points.append({"t": t, "price": float(px), "live": True})
                        points.sort(key=lambda x: x["t"])
                        note += " 末点为盘中实时价（每轮轮询刷新）。"
    except Exception:  # noqa: BLE001 - 实时价失败不影响已落盘分时
        pass

    return {
        "available": bool(points),
        "code": c,
        "day": f"{d[:4]}-{d[4:6]}-{d[6:]}",
        "points": points,
        "n": len(points),
        "prev_close": prev_close,
        "live": any(p.get("live") for p in points),
        "note": note,
    }


def stock_chart(code: str, *, day: str | None = None,
                kdays: int = 40) -> dict[str, Any]:
    """K 线 + 分时合并输出（前端一次请求就能画两图）。"""
    k = kline(code, days=kdays)
    i = intraday(code, day=day)
    return {
        "available": k["available"] or i["available"],
        "code": str(code).zfill(6),
        "kline": k,
        "intraday": i,
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    import json
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "002931"
    day = sys.argv[2] if len(sys.argv) > 2 else None
    r = stock_chart(code, day=day)
    print(f"code={r['code']} kline bars={len(r['kline']['bars'])} "
          f"intraday points={r['intraday'].get('n')}")
