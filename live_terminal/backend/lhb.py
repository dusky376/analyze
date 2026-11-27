# -*- coding: utf-8 -*-
"""东方财富真实龙虎榜引擎：全市场「活人情绪」+ 个股席位画像（严格因果，本地缓存）。

设计原则
--------
1. **严格因果（绝无未来函数）**：
   龙虎榜于交易日盘后 17:00–18:00 公布。因此第 T 日盘中选股/风控**只能读取 T-1 及之前**的龙虎榜；
   T 日盘后出榜数据仅用于指导 T+1 卖点预期与次日选股。
2. **全市场「龙虎榜活人数量」情绪**：
   剔除「东财拉萨天团」散户席位后，统计当日真金白银买入的非拉萨活跃席位（游资 + 机构 + 北向）
   去重家数、大额活人席位数（单笔 ≥ 1000 万）、拉萨接盘占比与机构净买额，反映场内大资金开工意愿。
3. **个股龙虎榜结构画像（供买点过滤 & 第 11 维蚂蚁进化因子）**：
   - ``机构合力``：机构/北向主导且净买为正
   - ``游资合力``：多家活跃游资均衡买入且净买为正
   - ``量化扎堆``：高频量化席位 ≥ 2 家（次日早盘易冲高兑现）
   - ``拉萨散户接盘``：拉萨席位 ≥ 2 家或占买五 ≥ 35%（次日核按钮高危）
   - ``主力出货榜``：龙虎榜净买比 ≤ -5%（借涨停派发）
   - ``一家独大``：买一占比 ≥ 50%（次日抛压集中）
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend import paths

EM_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/stock/tradedetail.html",
}

# 席位识别关键字（公开营业部名称规则）
_LASA_KEYS = ("拉萨", "山南香曲", "昌都聚盛", "东方财富证券股份有限公司西藏")
_INST_KEYS = ("机构专用",)
_NORTH_KEYS = ("深股通专用", "沪股通专用")
_QUANT_KEYS = (
    "量化",
    "华鑫证券有限责任公司上海分公司",
    "华鑫证券有限责任公司上海宛平南路",
    "开源证券股份有限公司西安西大街",
    "中国国际金融股份有限公司上海分公司",
    "瑞银证券有限责任公司上海花园石桥路",
    "高盛(中国)证券",
    "摩根大通证券",
)


def lhb_dir() -> Path:
    d = paths.data_dir() / "lhb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _norm_dash(day: str) -> str:
    s = str(day or "").strip().replace("/", "-")
    digits = s.replace("-", "")
    if len(digits) >= 8 and digits[:8].isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return s


def classify_seat(seat_name: str) -> str:
    """席位归类：lasa / inst / north / quant / active。"""
    s = str(seat_name or "").strip()
    if not s:
        return "active"
    if any(k in s for k in _LASA_KEYS):
        return "lasa"
    if any(k in s for k in _INST_KEYS):
        return "inst"
    if any(k in s for k in _NORTH_KEYS):
        return "north"
    if any(k in s for k in _QUANT_KEYS):
        return "quant"
    return "active"


def classify_stock_lhb(
    stock_row: dict[str, Any],
    buy_seats: list[dict[str, Any]],
) -> dict[str, Any]:
    """根据个股龙虎榜总览与买前五席位生成结构画像（纯函数）。"""
    net_amt = float(stock_row.get("net_amt") or 0.0)
    deal_net_ratio = float(stock_row.get("deal_net_ratio") or 0.0)
    lasa_n = inst_n = north_n = quant_n = active_n = 0
    lasa_buy = inst_buy = active_buy = total_buy = max_one_buy = 0.0
    seat_names: list[str] = []

    for s in buy_seats:
        nm = str(s.get("seat") or "")
        b = max(0.0, float(s.get("buy") or 0.0))
        cat = classify_seat(nm)
        total_buy += b
        max_one_buy = max(max_one_buy, b)
        if nm:
            seat_names.append(nm)
        if cat == "lasa":
            lasa_n += 1
            lasa_buy += b
        elif cat == "inst":
            inst_n += 1
            inst_buy += b
        elif cat == "north":
            north_n += 1
            inst_buy += b
        elif cat == "quant":
            quant_n += 1
            active_buy += b
        else:
            active_n += 1
            active_buy += b

    lasa_ratio = (lasa_buy / total_buy) if total_buy > 0 else 0.0
    inst_ratio = (inst_buy / total_buy) if total_buy > 0 else 0.0
    top1_ratio = (max_one_buy / total_buy) if total_buy > 0 else 0.0

    if lasa_n >= 2 or lasa_ratio >= 0.35:
        tag = "拉萨散户接盘"
        score_delta = -10.0
        note = f"买五含 {lasa_n} 家拉萨席位（占买五 {lasa_ratio:.0%}），散户化重、次日易核按钮"
    elif deal_net_ratio <= -5.0 and net_amt < 0:
        tag = "主力出货榜"
        score_delta = -8.0
        note = f"龙虎榜净流出 {net_amt / 1e8:.2f} 亿（净买比 {deal_net_ratio:+.1f}%），警惕主力借板派发"
    elif (inst_n + north_n) >= 1 and inst_ratio >= 0.25 and net_amt >= 0:
        tag = "机构合力"
        score_delta = 8.0
        note = f"机构/北向 {inst_n + north_n} 席主导（占买五 {inst_ratio:.0%}），净买 {net_amt / 1e8:+.2f} 亿"
    elif quant_n >= 2:
        tag = "量化扎堆"
        score_delta = -4.0
        note = f"买五含 {quant_n} 家高频量化席位，次日早盘易冲高兑现"
    elif top1_ratio >= 0.50 and net_amt > 0:
        tag = "一家独大"
        score_delta = -3.0
        note = f"买一占比 {top1_ratio:.0%} 过高，次日集中砸盘风险偏大"
    elif active_n >= 3 and net_amt > 0:
        tag = "游资合力"
        score_delta = 6.0
        note = f"活跃游资 {active_n} 家均衡合力，净买 {net_amt / 1e8:+.2f} 亿"
    else:
        tag = "常规龙虎榜"
        score_delta = 0.0
        note = f"龙虎榜净买 {net_amt / 1e8:+.2f} 亿（活跃 {active_n} / 机构 {inst_n} / 拉萨 {lasa_n}）"

    return {
        "ts_code": str(stock_row.get("ts_code") or "").zfill(6),
        "name": str(stock_row.get("name") or ""),
        "day": str(stock_row.get("day") or ""),
        "tag": tag,
        "score_delta": score_delta,
        "net_amt_yi": round(net_amt / 1e8, 3),
        "deal_net_ratio": round(deal_net_ratio, 2),
        "lasa_n": lasa_n,
        "inst_n": inst_n + north_n,
        "quant_n": quant_n,
        "active_n": active_n,
        "lasa_ratio": round(lasa_ratio, 3),
        "top_seats": seat_names[:3],
        "note": note,
    }


def compute_market_lhb_sentiment(
    stocks: list[dict[str, Any]],
    buy_seats: list[dict[str, Any]],
    *,
    hist_active_counts: list[int] | None = None,
) -> dict[str, Any]:
    """根据当日全市场龙虎榜明细计算「活人数量」情绪（纯函数，支持滚动历史分位自适应）。"""
    if not stocks and not buy_seats:
        return {"available": False, "label": "无龙虎榜数据", "size_mult": 1.0}

    active_seats: set[str] = set()
    big_active_seats: set[str] = set()  # 单笔买入 ≥ 1000 万的真金白银活人席位
    inst_buy_total = 0.0
    lasa_buy_total = 0.0
    all_buy_total = 0.0
    quant_hits = 0

    for s in buy_seats:
        nm = str(s.get("seat") or "").strip()
        b = max(0.0, float(s.get("buy") or 0.0))
        all_buy_total += b
        cat = classify_seat(nm)
        if cat == "lasa":
            lasa_buy_total += b
        else:
            if nm:
                active_seats.add(nm)
                if b >= 10_000_000.0:
                    big_active_seats.add(nm)
            if cat in ("inst", "north"):
                inst_buy_total += b
            elif cat == "quant":
                quant_hits += 1

    active_n = len(active_seats)
    big_active_n = len(big_active_seats)
    lasa_share = (lasa_buy_total / all_buy_total) if all_buy_total > 0 else 0.0
    total_net = sum(float(x.get("net_amt") or 0.0) for x in stocks)

    # 活人数量自适应分位（有 ≥5 日历史缓存时按滚动分位，否则按经验先验）
    valid_hist = sorted(int(x) for x in (hist_active_counts or []) if x is not None and int(x) > 0)
    if len(valid_hist) >= 5:
        p30 = valid_hist[max(0, int(0.30 * len(valid_hist)) - 1)]
        p75 = valid_hist[min(len(valid_hist) - 1, int(0.75 * len(valid_hist)))]
        rank_pct = round(sum(1 for x in valid_hist if x <= active_n) / len(valid_hist), 3)
        basis = f"滚动{len(valid_hist)}日分位(P30={p30}, P75={p75})"
    else:
        p30, p75 = 70, 130
        rank_pct = None
        basis = "先验基准(冷清<70 / 活跃≥130)"

    if active_n <= p30 or lasa_share >= 0.25:
        state = "cold"
        label = "游资撤退·拉萨主导"
        size_mult = 0.85
    elif active_n >= p75 and lasa_share < 0.15 and total_net >= 0:
        state = "hot"
        label = "活人充足·游资全面开工"
        size_mult = 1.05
    else:
        state = "neutral"
        label = "龙虎榜活人正常"
        size_mult = 1.00

    return {
        "available": True,
        "n_stocks": len(stocks),
        "active_seat_count": active_n,
        "big_active_seat_count": big_active_n,
        "lasa_buy_share": round(lasa_share, 4),
        "inst_buy_yi": round(inst_buy_total / 1e8, 2),
        "total_buy_yi": round(all_buy_total / 1e8, 2),
        "total_net_yi": round(total_net / 1e8, 2),
        "quant_hits": quant_hits,
        "active_rank_pct": rank_pct,
        "state": state,
        "label": label,
        "size_mult": size_mult,
        "basis": basis,
        "note": (
            f"上榜 {len(stocks)} 只 · 非拉萨活人席位 {active_n} 家（≥千万大额 {big_active_n} 家）· "
            f"拉萨占比 {lasa_share:.1%} · 总净买 {total_net / 1e8:+.2f} 亿 → {label}"
        ),
    }


def _em_get(report: str, columns: str, day_dash: str, *, timeout: float = 10.0) -> list[dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    out: list[dict[str, Any]] = []
    for page in range(1, 5):
        params = urllib.parse.urlencode({
            "reportName": report,
            "columns": columns,
            "filter": f"(TRADE_DATE='{day_dash}')",
            "pageSize": "500",
            "pageNumber": str(page),
        })
        req = urllib.request.Request(f"{EM_API}?{params}", headers=_HEADERS)
        raw = opener.open(req, timeout=timeout).read().decode("utf-8")
        d = json.loads(raw)
        res = d.get("result") or {}
        batch = res.get("data") or []
        out.extend(batch)
        if len(batch) < 500 or len(out) >= int(res.get("count") or 0):
            break
    return out


def fetch_day(day: str, *, force: bool = False, timeout: float = 10.0) -> dict[str, Any]:
    """拉取并缓存指定交易日的东财全市场龙虎榜（含活人情绪 + 个股画像）。"""
    d_dash = _norm_dash(day)
    if not d_dash:
        return {"available": False, "day": "", "reason": "非法日期"}
    cache_path = lhb_dir() / f"{d_dash}.json"
    if not force and cache_path.is_file():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    try:
        raw_stocks = _em_get(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLAIN,BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,DEAL_NET_RATIO,CHANGE_RATE,TURNOVERRATE",
            d_dash,
            timeout=timeout,
        )
        if not raw_stocks:
            return {"available": False, "day": d_dash, "reason": f"{d_dash} 暂无龙虎榜数据（非交易日或尚未公布）"}
        raw_buys = _em_get(
            "RPT_BILLBOARD_DAILYDETAILSBUY",
            "TRADE_DATE,SECURITY_CODE,OPERATEDEPT_NAME,BUY,SELL,NET",
            d_dash,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "day": d_dash, "reason": f"东财龙虎榜拉取失败：{type(exc).__name__}: {exc}"}

    # 同一股票可能因多个上榜原因重复出现，按 code 去重保留净买额绝对值最大记录
    by_code: dict[str, dict[str, Any]] = {}
    for r in raw_stocks:
        c6 = str(r.get("SECURITY_CODE") or "").zfill(6)
        if not c6 or c6 == "000000":
            continue
        item = {
            "ts_code": c6,
            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
            "day": d_dash,
            "net_amt": float(r.get("BILLBOARD_NET_AMT") or 0.0),
            "buy_amt": float(r.get("BILLBOARD_BUY_AMT") or 0.0),
            "sell_amt": float(r.get("BILLBOARD_SELL_AMT") or 0.0),
            "deal_net_ratio": float(r.get("DEAL_NET_RATIO") or 0.0),
            "change_rate": float(r.get("CHANGE_RATE") or 0.0),
            "turnover": float(r.get("TURNOVERRATE") or 0.0),
            "explain": str(r.get("EXPLAIN") or ""),
        }
        prev = by_code.get(c6)
        if prev is None or abs(item["net_amt"]) > abs(prev["net_amt"]):
            by_code[c6] = item

    seats_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_seat_key: set[tuple[str, str]] = set()
    flat_seats: list[dict[str, Any]] = []
    for r in raw_buys:
        c6 = str(r.get("SECURITY_CODE") or "").zfill(6)
        nm = str(r.get("OPERATEDEPT_NAME") or "").strip()
        if not c6 or not nm or (c6, nm) in seen_seat_key:
            continue
        seen_seat_key.add((c6, nm))
        seat_rec = {
            "ts_code": c6,
            "seat": nm,
            "buy": float(r.get("BUY") or 0.0),
            "sell": float(r.get("SELL") or 0.0),
            "net": float(r.get("NET") or 0.0),
        }
        seats_by_code[c6].append(seat_rec)
        flat_seats.append(seat_rec)

    # 读取已有历史缓存的活人数量做滚动分位
    hist_counts: list[int] = []
    for fp in sorted(lhb_dir().glob("*.json")):
        if fp.stem < d_dash:
            try:
                old = json.loads(fp.read_text(encoding="utf-8"))
                ac = ((old.get("market_sentiment") or {}).get("active_seat_count"))
                if isinstance(ac, (int, float)) and ac > 0:
                    hist_counts.append(int(ac))
            except Exception:  # noqa: BLE001
                pass

    stocks_list = list(by_code.values())
    sentiment = compute_market_lhb_sentiment(stocks_list, flat_seats, hist_active_counts=hist_counts)
    profiles = {
        c6: classify_stock_lhb(st, seats_by_code.get(c6) or [])
        for c6, st in by_code.items()
    }
    payload = {
        "available": True,
        "day": d_dash,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "market_sentiment": sentiment,
        "stock_profiles": profiles,
    }
    try:
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cache_path)
    except Exception:  # noqa: BLE001
        pass
    return payload


def causal_snapshot(before_day: str, *, lookback_days: int = 5, allow_fetch: bool = True) -> dict[str, Any]:
    """严格因果获取 `before_day` 之前最近的龙虎榜活人情绪与近 N 日个股龙虎榜画像。"""
    d_cut = _norm_dash(before_day)
    if not d_cut:
        return {"available": False, "latest_day": None, "market_sentiment": {}, "stock_profiles": {}}

    # 倒推最多 10 个自然日找最近 1~lookback_days 个有效龙虎榜日（严格 < d_cut）
    try:
        dt0 = datetime.strptime(d_cut, "%Y-%m-%d")
    except ValueError:
        return {"available": False, "latest_day": None, "market_sentiment": {}, "stock_profiles": {}}

    loaded: list[dict[str, Any]] = []
    for delta in range(1, 12):
        if len(loaded) >= lookback_days:
            break
        d_try = (dt0 - timedelta(days=delta)).strftime("%Y-%m-%d")
        cp = lhb_dir() / f"{d_try}.json"
        if cp.is_file():
            try:
                rec = json.loads(cp.read_text(encoding="utf-8"))
                if rec.get("available"):
                    loaded.append(rec)
                    continue
            except Exception:  # noqa: BLE001
                pass
        if allow_fetch and len(loaded) == 0 and delta <= 5:
            # 最多对最近 5 个自然日主动联网补抓最近 1 天（避免盘中阻塞）
            rec = fetch_day(d_try, timeout=6.0)
            if rec.get("available"):
                loaded.append(rec)

    if not loaded:
        return {"available": False, "latest_day": None, "market_sentiment": {}, "stock_profiles": {}}

    latest = loaded[0]
    merged_profiles: dict[str, dict[str, Any]] = {}
    # 从旧到新合并，使最新一日画像覆盖旧日
    for rec in reversed(loaded):
        for c6, prof in (rec.get("stock_profiles") or {}).items():
            merged_profiles[str(c6).zfill(6)] = prof

    # 昨日活人游资/机构主攻板块观察池（严格事前 T-1，剔除纯拉萨接盘）
    latest_day = latest.get("day") or ""
    latest_profs = latest.get("stock_profiles") or {}
    attack_profs = {
        c6: p for c6, p in latest_profs.items()
        if float(p.get("net_amt_yi") or 0.0) > 0.0
        and (int(p.get("active_n") or 0) + int(p.get("inst_n") or 0)) > 0
        and "拉萨" not in str(p.get("tag") or "")
    }
    focus = compute_lhb_focus(attack_profs, latest_day) if (attack_profs and latest_day) else {"top_themes": [], "top_stocks": []}
    attack_themes = [
        t for t in (focus.get("top_themes") or [])
        if int(t.get("n_stocks") or 0) >= 2 or float(t.get("net_yi") or 0.0) >= 0.8
    ][:5]

    return {
        "available": True,
        "before_day": d_cut,
        "latest_day": latest_day,
        "n_days_loaded": len(loaded),
        "market_sentiment": latest.get("market_sentiment") or {},
        "stock_profiles": merged_profiles,
        "attack_themes": attack_themes,
        "attack_stocks": (focus.get("top_stocks") or [])[:8],
    }


def compute_lhb_focus(
    profiles: dict[str, dict[str, Any]],
    day: str,
) -> dict[str, Any]:
    """客观汇总龙虎榜活人资金主攻的板块与焦点个股（仅作资金流向参考，不做绝对肯定/否定）。"""
    if not profiles:
        return {"top_themes": [], "top_stocks": []}
    theme_map: dict[str, list[str]] = {}
    try:
        from backend import live_sim, theme_match
        raw_tags = live_sim._theme_tags(day.replace("-", ""))
        if raw_tags:
            items = [{"code": c, "name": "", "tags": tlist, "industry": None, "streak": 2}
                     for c, tlist in raw_tags.items()]
            grouped = theme_match.group_by_theme(items)
            for g in grouped.get("groups") or []:
                if g.get("level") in ("axis", "tag"):
                    th = str(g.get("theme") or "")
                    for c in g.get("codes") or []:
                        theme_map.setdefault(str(c).zfill(6), []).append(th)
    except Exception:  # noqa: BLE001
        theme_map = {}

    th_agg: dict[str, dict[str, Any]] = {}
    top_stocks: list[dict[str, Any]] = []
    for c6, prof in profiles.items():
        net_yi = float(prof.get("net_amt_yi") or 0.0)
        ths = theme_map.get(c6) or []
        top_stocks.append({
            "ts_code": c6,
            "name": prof.get("name") or "",
            "tag": prof.get("tag") or "",
            "net_amt_yi": round(net_yi, 3),
            "active_n": int(prof.get("active_n") or 0),
            "inst_n": int(prof.get("inst_n") or 0),
            "lasa_n": int(prof.get("lasa_n") or 0),
            "themes": ths[:2],
        })
        for th in ths:
            slot = th_agg.setdefault(th, {"theme": th, "n_stocks": 0, "net_yi": 0.0, "stocks": []})
            slot["n_stocks"] += 1
            slot["net_yi"] += net_yi
            if len(slot["stocks"]) < 4:
                slot["stocks"].append(f"{prof.get('name') or c6}({net_yi:+.2f}亿)")

    top_themes = sorted(
        [
            {**v, "net_yi": round(v["net_yi"], 3)}
            for v in th_agg.values()
        ],
        key=lambda x: (-x["n_stocks"], -x["net_yi"]),
    )[:8]
    top_stocks.sort(key=lambda x: -x["net_amt_yi"])
    return {"top_themes": top_themes, "top_stocks": top_stocks[:10]}


def compute_youzi_premium(
    profiles: dict[str, dict[str, Any]],
    pct_by_code: dict[str, float] | None = None,
) -> dict[str, Any]:
    """统计昨日龙虎榜活人（非拉萨净买为正）标的今日真实溢价与胜率，用于验证游资赚钱效应。"""
    if not profiles or not pct_by_code:
        return {"available": False, "n": 0, "avg_pct": None, "win_rate": None, "note": "暂无次日溢价行情"}
    vals: list[float] = []
    inst_vals: list[float] = []
    lasa_vals: list[float] = []
    for c6, prof in profiles.items():
        pct = pct_by_code.get(str(c6).zfill(6))
        if pct is None:
            continue
        p_f = float(pct)
        tag = str(prof.get("tag") or "")
        net_yi = float(prof.get("net_amt_yi") or 0.0)
        if tag == "拉萨散户接盘":
            lasa_vals.append(p_f)
        elif net_yi > 0:
            vals.append(p_f)
            if tag == "机构合力":
                inst_vals.append(p_f)
    if not vals:
        return {"available": False, "n": 0, "avg_pct": None, "win_rate": None, "note": "暂无匹配溢价样本"}
    avg_p = round(sum(vals) / len(vals), 2)
    wr = round(sum(1 for x in vals if x > 0) / len(vals), 3)
    return {
        "available": True,
        "n": len(vals),
        "avg_pct": avg_p,
        "win_rate": wr,
        "inst_avg_pct": round(sum(inst_vals) / len(inst_vals), 2) if inst_vals else None,
        "lasa_avg_pct": round(sum(lasa_vals) / len(lasa_vals), 2) if lasa_vals else None,
        "note": (
            f"昨日活人净买 {len(vals)} 只今日均涨 {avg_p:+.2f}%（胜率 {wr:.0%}）"
            + (f" · 机构合力均涨 {sum(inst_vals)/len(inst_vals):+.2f}%" if inst_vals else "")
            + (f" · 拉萨接盘均涨 {sum(lasa_vals)/len(lasa_vals):+.2f}%" if lasa_vals else "")
        ),
    }


def cached_stock_tag_before(day: str, code: str, *, lookback_days: int = 5) -> str | None:
    """只读本地缓存（绝不发网络请求）：取 `day` 之前近 N 日该股最新龙虎榜标签，供第 11 维蚂蚁进化因子使用。"""
    d_cut = _norm_dash(day)
    c6 = str(code or "").zfill(6)
    if not d_cut or not c6:
        return None
    try:
        dt0 = datetime.strptime(d_cut, "%Y-%m-%d")
    except ValueError:
        return None
    for delta in range(1, lookback_days + 5):
        d_try = (dt0 - timedelta(days=delta)).strftime("%Y-%m-%d")
        cp = lhb_dir() / f"{d_try}.json"
        if not cp.is_file():
            continue
        try:
            rec = json.loads(cp.read_text(encoding="utf-8"))
            prof = (rec.get("stock_profiles") or {}).get(c6)
            if prof and prof.get("tag"):
                return str(prof["tag"])
        except Exception:  # noqa: BLE001
            continue
    return None
