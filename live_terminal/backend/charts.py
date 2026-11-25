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

from backend import archive, feeds, paths

ROOT = Path(__file__).resolve().parents[1]
# 数据与代码分离：留档根统一由 backend.paths 解析。
LIVE = paths.live_dir()

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


def _extract_quote_dict(code: str,
                        quotes: pl.DataFrame | dict[str, Any] | None = None) -> dict[str, Any] | None:
    """提取单股行情字典；未传入时走单股快查 feeds.single_quote（~50ms），绝不重拉全市场 5580 只。"""
    c = str(code).zfill(6)
    if isinstance(quotes, dict):
        if "price" in quotes and str(quotes.get("ts_code") or c).zfill(6) == c:
            return quotes
        hit = quotes.get(c)
        return hit if isinstance(hit, dict) else None
    if isinstance(quotes, pl.DataFrame) and quotes.height > 0:
        r = quotes.filter(pl.col("ts_code") == c)
        if r.height > 0:
            return r.to_dicts()[0]
        return None
    return feeds.single_quote(c, ttl=5.0)


def _live_bar_for(code: str,
                  quotes: pl.DataFrame | dict[str, Any] | None = None) -> dict[str, Any] | None:
    """当日实时 bar（只读内存合成，不写盘）。

    用户 2026-11-25 裁定后：**只要行情快照是今天的**就合成当日 bar，
    不再要求 `market_clock.is_cn_trading()` —— 午休/竞价/收盘瞬间也要看到今天。
    `live=True` 仅表示快照足够新（≤3 分钟）；午休时仍显示今天，但标 as_of。
    """
    try:
        d = _extract_quote_dict(code, quotes=quotes)
        if not d:
            return None
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
            "age_sec": round(age, 1) if age is not None else None,
            "as_of": snap,
            "source": f"live_{d.get('feed_source') or 'quotes'}",
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
    # 2026-11-25 瘦身：逐帧 / 合并归档都由 archive 抽象层统一处理。
    df = archive.latest_frame(d, "quotes")
    if df is None or df.height == 0:
        return None
    try:
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
    # 2026-11-25 瘦身：逐帧 / 合并归档统一读取，frame 列即 HHMMSS。
    frames = archive.read_frames(d, "quotes")
    points: list[dict[str, Any]] = []
    if frames is not None and frames.height:
        cols = [c for c in ("frame", "ts_code", "price", "prev_close")
                if c in frames.columns]
        sub = (frames.select(cols)
               .filter(pl.col("ts_code") == str(code).zfill(6)))
        has_pc = "prev_close" in sub.columns
        for r in sub.iter_rows(named=True):
            px = r.get("price")
            if px is None:
                continue
            stem = str(r.get("frame") or "")
            if len(stem) != 6:
                continue
            points.append({
                "t": f"{stem[:2]}:{stem[2:4]}:{stem[4:6]}",
                "price": float(px),
                "prev_close": (float(r["prev_close"])
                               if has_pc and r.get("prev_close") is not None
                               else None),
            })
    points.sort(key=lambda x: x["t"])
    return points, d


def _trade_markers(code: str) -> list[dict[str, Any]]:
    """只读提取该股在模拟账本里的买卖点标记，供 K 线图叠加核对。"""
    import json
    c = str(code).zfill(6)
    markers: list[dict[str, Any]] = []
    try:
        from backend import live_sim
        sf = getattr(live_sim, "STATE_FILE", paths.data_dir() / "paper" / "sim_state.json")
        if not Path(sf).is_file():
            return []
        st = json.loads(Path(sf).read_text(encoding="utf-8"))
        raw_pos = st.get("positions")
        pos_list = list(raw_pos.values()) if isinstance(raw_pos, dict) else list(raw_pos or [])
        tr_list = list(st.get("trades") or [])
        for p in pos_list + tr_list:
            if str(p.get("ts_code") or "").zfill(6) != c:
                continue
            ed = str(p.get("entry_date") or "")
            if len(ed) == 8:
                ed = f"{ed[:4]}-{ed[4:6]}-{ed[6:]}"
            ep = p.get("entry_price")
            if ed and ep:
                markers.append({
                    "kind": "buy",
                    "date": ed[:10],
                    "price": float(ep),
                    "label": "买",
                })
            xd = str(p.get("exit_date") or "")
            if len(xd) == 8:
                xd = f"{xd[:4]}-{xd[4:6]}-{xd[6:]}"
            xp = p.get("exit_price")
            if xd and xp:
                markers.append({
                    "kind": "sell",
                    "date": xd[:10],
                    "price": float(xp),
                    "label": "卖",
                    "reason": str(p.get("exit_reason") or ""),
                })
    except Exception:  # noqa: BLE001
        return []
    # 去重（positions 和 trades 可能重叠已平仓记录）
    seen: set[tuple[str, str, float]] = set()
    uniq: list[dict[str, Any]] = []
    for m in markers:
        key = (m["kind"], m["date"], round(m["price"], 4))
        if key not in seen:
            seen.add(key)
            uniq.append(m)
    return uniq


def intraday(code: str, day: str | None = None, *,
             quotes: pl.DataFrame | dict[str, Any] | None = None) -> dict[str, Any]:
    """当日分时。

    优先级（2026-11-25 用户裁定「要当天实时分时图」）：
      1. 目标日 == 今天 → 东财 Push2 1 分钟（含真实 VWAP 均价线+量能）/ 腾讯 1 分钟（8 秒 TTL）；
      2. 目标日 == 今天但接口失败 → 已落盘 1 分钟 / quotes 5 分钟留档；
      3. 历史日 → 已落盘 1 分钟优先，其次 quotes 5 分钟留档。
    """
    c = str(code).zfill(6)
    today8 = _cn_today()
    if not day:
        # 交易日 09:25 起默认直接查「今天」实时 1 分钟分时，不再等本地 quotes 留档目录建好
        use_today = False
        try:
            from backend import market_clock
            tz = market_clock.TZ_CN
            now_cn = datetime.now(tz) if tz else datetime.now()
            if market_clock.is_trading_day("CN", now=now_cn) and now_cn.strftime("%H%M") >= "0925":
                use_today = True
        except Exception:  # noqa: BLE001
            pass
        if use_today:
            day = today8
        else:
            dirs = sorted((x.name for x in LIVE.iterdir()
                           if x.is_dir() and x.name.isdigit()
                           and (x / "quotes").is_dir()), reverse=True) if LIVE.is_dir() else []
            day = today8 if today8 in dirs else (dirs[0] if dirs else today8)
    d = _compact(day)
    is_today = d == today8

    points: list[dict[str, Any]] = []
    source = "quotes_archive"
    granularity = "5min"
    prev_close_from_min: float | None = None
    if is_today:
        try:
            from backend import minute_flow
            got = minute_flow.live_series(c, d, persist=True)
            if got:
                points = [{
                    "t": p["t"],
                    "price": p["price"],
                    "avg_price": p.get("avg_price"),
                    "volume": p.get("volume"),
                    "amount": p.get("amount"),
                } for p in got]
                source = str(got[0].get("source") or "eastmoney_1min")
                granularity = "1min"
                prev_close_from_min = got[0].get("prev_close")
        except Exception:  # noqa: BLE001 - 接口失败落回留档快照
            points = []
    else:
        try:
            from backend import minute_flow
            got = minute_flow.load_persisted_minute(c, d)
            if got:
                points = [{
                    "t": p["t"],
                    "price": p["price"],
                    "avg_price": p.get("avg_price"),
                    "volume": p.get("volume"),
                    "amount": p.get("amount"),
                } for p in got]
                source = str(got[0].get("source") or "minute_archive")
                granularity = "1min"
        except Exception:  # noqa: BLE001
            points = []

    if not points:
        points, d = _archive_points(c, d)
        if not points and is_today:
            return {"available": False, "code": c, "day": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                    "reason": f"{d} 尚无分时数据（盘前或采集未开始）",
                    "points": [], "granularity": granularity, "source": source}

    # 昨收：优先实时单股行情（~50ms），其次分时接口，再其次留档帧里的 prev_close
    prev_close = None
    try:
        qd = _extract_quote_dict(c, quotes=quotes)
        if qd and qd.get("prev_close"):
            prev_close = float(qd["prev_close"])
    except Exception:  # noqa: BLE001
        prev_close = None
    if prev_close is None and prev_close_from_min:
        prev_close = float(prev_close_from_min)
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
            fresh = abs((nhh * 60 + nmm) - (hh * 60 + mm)) <= 5
        except Exception:  # noqa: BLE001
            fresh = False

    if source == "eastmoney_1min":
        note = "东方财富 Push2 1 分钟真实分时（含均价线 VWAP + 成交量，约 8 秒刷新）。"
    elif source == "tencent_1min":
        note = "腾讯 1 分钟分时（盘中实时，约 8 秒刷新）；价格为该分钟最后成交价。"
    else:
        note = f"当日 {len(points)} 个 {granularity} 留档点；非逐笔，连线只示趋势。"

    return {
        "available": bool(points),
        "code": c,
        "day": f"{d[:4]}-{d[4:6]}-{d[6:]}",
        "points": points,
        "n": len(points),
        "prev_close": prev_close,
        "has_vwap": any(p.get("avg_price") for p in points),
        "live": bool(is_today and fresh and granularity == "1min"),
        "as_of": last_t,
        "source": source,
        "granularity": granularity,
        "note": note,
    }


def stock_chart(code: str, *, day: str | None = None,
                kdays: int = 40) -> dict[str, Any]:
    """K 线 + 分时 + 买卖点标记合并输出（单股快查，不拉全市场 5580 只）。"""
    c = str(code).zfill(6)
    try:
        q = feeds.single_quote(c, ttl=5.0)
    except Exception:  # noqa: BLE001
        q = None
    k = kline(c, days=kdays, quotes=q)
    i = intraday(c, day=day, quotes=q)
    return {
        "available": k["available"] or i["available"],
        "code": c,
        "kline": k,
        "intraday": i,
        "markers": _trade_markers(c),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "002931"
    day = sys.argv[2] if len(sys.argv) > 2 else None
    r = stock_chart(code, day=day)
    print(f"code={r['code']} kline bars={len(r['kline']['bars'])} "
          f"intraday points={r['intraday'].get('n')}")
