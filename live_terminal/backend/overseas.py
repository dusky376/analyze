# -*- coding: utf-8 -*-
"""外盘指数风偏（日经/韩国KOSPI/美股/A50/中概）与美股领涨板块→A股题材映射引擎。

核心设计原则
------------
1. **严格时间因果**：美股隔夜收盘（04:00/05:00）与日韩早盘（08:00起）均早于 A 股 09:15 竞价，
   天然无未来函数；所有快照带真实时点并原子落盘至 `data/live/<day>/overseas.json`。
2. **多源免费直连（零 API Key、零虚构数据）**：
   - 新浪财经（日经225 / 韩国KOSPI / 富时A50期货 / 费城半导体SOX / 纳指 / 标普）
   - 腾讯行情（纳指 / 标普 / 恒生指数 / 美股核心龙头 NVDA、TSLA、AAPL、AMD、AVGO、TSM、MU、META、MSFT、PLTR、BABA、PDD、KWEB 等）
   取不到某项指标就如实标 None，绝不编造涨跌幅。
3. **两层接入悟空真经**：
   - **情绪风偏因子**：将日韩 + A50 + 美股 + 中概合成为 `overseas_score`（-100 ~ +100）与外盘风偏系数
     `size_mult`（`risk_off=0.70 / caution=0.85 / neutral=1.00`），作为市场情绪体系的前置风偏层；
   - **美股板块映射**：映射到 `configs/theme_axis.yaml` 的 A 股题材轴，与次日 A 股一字二板兔子形成内外共振催化。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend import paths, process_guard

MAX_STALE_HOURS = 168.0
STALE_HOURS = 36.0  # 仅作历史兼容符号保留；默认改用 A股交易日历因果锚点(_prev_cn_close_dt)
_TZ_CN = ZoneInfo("Asia/Shanghai")
_TZ_US = ZoneInfo("America/New_York")

_LOCK = threading.RLock()
_MEM_CACHE: tuple[float, dict[str, Any]] | None = None
# 动态自适应 TTL：非交易时段 120s；常规盘中 10s；指数/期货突拉或重大利好窗口自动切极速 1.5s
_TTL_IDLE = 120.0
_TTL_TRADING = 10.0
_TTL_BURST = 1.5
_BURST_UNTIL: float = 0.0


def trigger_burst_mode(duration_sec: float = 900.0) -> None:
    """重大政策利好或指数突拉时切换至 5 秒极速轮询档。"""
    global _BURST_UNTIL
    with _LOCK:
        _BURST_UNTIL = max(_BURST_UNTIL, time.monotonic() + float(duration_sec))


def _effective_ttl() -> float:
    now_m = time.monotonic()
    if now_m < _BURST_UNTIL:
        return _TTL_BURST
    try:
        from backend import market_clock
        tz = market_clock.TZ_CN
        now_cn = datetime.now(tz) if tz else datetime.now()
        hm = now_cn.strftime("%H%M")
        # 覆盖盘前竞价 + 午间政策高发期 + 交易时段（09:15–15:05）
        if market_clock.is_trading_day("CN", now=now_cn) and "0915" <= hm <= "1505":
            return _TTL_TRADING
    except Exception:  # noqa: BLE001
        pass
    return _TTL_IDLE

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: 核心全球指数定义（按 A 股风偏相关性配置权重）
INDEX_SPECS: list[dict[str, Any]] = [
    {"key": "KOSPI", "name": "韩国综合", "region": "亚太早盘", "weight": 0.22, "sina": "znb_KOSPI"},
    {"key": "N225",  "name": "日经225",  "region": "亚太早盘", "weight": 0.18, "sina": "znb_NKY"},
    {"key": "A50",   "name": "富时A50",  "region": "中国映射", "weight": 0.20, "sina": "hf_CHA50CFD"},
    {"key": "IXIC",  "name": "纳斯达克", "region": "美股隔夜", "weight": 0.14, "sina": "gb_ixic", "tx": "usIXIC"},
    {"key": "SPX",   "name": "标普500",  "region": "美股隔夜", "weight": 0.08, "sina": "gb_inx",  "tx": "usINX"},
    {"key": "SOX",   "name": "费城半导体", "region": "美股科技", "weight": 0.08, "sina": "gb_sox"},
    {"key": "HSI",   "name": "恒生指数", "region": "中国映射", "weight": 0.06, "tx": "hkHSI"},
    {"key": "KWEB",  "name": "中概互联", "region": "中国映射", "weight": 0.04, "tx": "usKWEB"},
]

#: 美股龙头 / 行业锚 → A 股 theme_axis.yaml 题材轴映射
US_THEME_MAP: list[dict[str, Any]] = [
    {
        "theme": "算力",
        "label": "美股 AI 算力 / 半导体链",
        "leaders": [
            ("NVDA", "英伟达", "usNVDA"),
            ("AVGO", "博通", "usAVGO"),
            ("AMD", "AMD", "usAMD"),
            ("TSM", "台积电", "usTSM"),
            ("MU", "美光科技", "usMU"),
        ],
        "index_ref": "SOX",
    },
    {
        "theme": "机器人",
        "label": "美股具身智能 / 智驾链",
        "leaders": [
            ("TSLA", "特斯拉", "usTSLA"),
        ],
    },
    {
        "theme": "AI传媒",
        "label": "美股 AI 应用 / 消费电子",
        "leaders": [
            ("PLTR", "Palantir", "usPLTR"),
            ("MSFT", "微软", "usMSFT"),
            ("META", "Meta", "usMETA"),
            ("AAPL", "苹果", "usAAPL"),
        ],
    },
    {
        "theme": "消费",
        "label": "中概互联网 / 消费映射",
        "leaders": [
            ("BABA", "阿里巴巴", "usBABA"),
            ("PDD", "拼多多", "usPDD"),
            ("KWEB", "中概ETF", "usKWEB"),
        ],
    },
    {
        "theme": "化工周期",
        "label": "海外能源 / 黄金周期",
        "leaders": [
            ("XOM", "埃克森美孚", "usXOM"),
            ("GLD", "黄金ETF", "usGLD"),
        ],
    },
]


def _today8() -> str:
    try:
        from backend import market_clock
        tz = market_clock.TZ_CN
        return (datetime.now(tz) if tz else datetime.now()).strftime("%Y%m%d")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y%m%d")


def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _parse_as_of_cn(as_of: Any, *, is_us_et: bool = False) -> datetime | None:
    """将外盘行情的 as_of 字符串解析为北京时间（Asia/Shanghai）。"""
    s = str(as_of or "").strip()
    if not s:
        return None
    s = s.replace("/", "-")
    dt: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    if is_us_et:
        return dt.replace(tzinfo=_TZ_US).astimezone(_TZ_CN)
    return dt.replace(tzinfo=_TZ_CN)


def _prev_cn_close_dt(ref_cn: datetime) -> datetime:
    """获取相对 ref_cn 所属决策日的上一 A 股已收盘交易日 15:00（北京时间）。

    物理含义：凡外盘报价时点 ≤ prev_cn_day 15:00，说明该外盘行情早在上一交易日 A 股收盘前
    就已经交易完毕并被昨日 A 股消化，今日无新隔夜更新（感恩节周五自动静默；周末周六05:00
    仍 > 周五15:00，下周一早盘正常生效，不会再被固定36小时规则误杀）。
    """
    ref_day = ref_cn.strftime("%Y-%m-%d")
    prev_d: date | None = None
    try:
        from backend import feeds
        tdays = [str(d)[:10] for d in (feeds._trading_days() or []) if d]
        if tdays:
            if ref_day in tdays:
                target_day = ref_day
            else:
                prior_le = [d for d in tdays if d <= ref_day]
                target_day = prior_le[-1] if prior_le else tdays[0]
            prev_days = [d for d in tdays if d < target_day]
            if prev_days:
                prev_d = date.fromisoformat(prev_days[-1])
    except Exception:  # noqa: BLE001
        prev_d = None

    if prev_d is None:
        # 交易日历不可用时回退按工作日推算
        cur_d = ref_cn.date()
        while cur_d.weekday() >= 5:
            cur_d -= timedelta(days=1)
        prev_d = cur_d - timedelta(days=1)
        while prev_d.weekday() >= 5:
            prev_d -= timedelta(days=1)
    return datetime(prev_d.year, prev_d.month, prev_d.day, 15, 0, 0, tzinfo=_TZ_CN)


def _freshness_meta(as_of: Any, *, now_cn: datetime | None = None,
                    is_us_et: bool = False,
                    stale_hours: float | None = None) -> dict[str, Any]:
    """判定外盘报价新鲜度：默认采用 A 股交易日历因果锚点（stale_hours 仅兼容显式传参）。"""
    dt = _parse_as_of_cn(as_of, is_us_et=is_us_et)
    if dt is None:
        return {"stale": False, "holiday_silent": False, "stale_hours": None, "status_note": ""}
    ref = now_cn
    if ref is None:
        ref = datetime.now(_TZ_CN)
    elif ref.tzinfo is None:
        ref = ref.replace(tzinfo=_TZ_CN)
    age_h = (ref - dt).total_seconds() / 3600.0
    if stale_hours is not None:
        is_stale = age_h > float(stale_hours)
        prev_close_str = None
        note = f"休市静默（已过期 {age_h:.0f}h）" if is_stale else ""
    else:
        prev_cn_close = _prev_cn_close_dt(ref)
        is_stale = bool(dt <= prev_cn_close or age_h > MAX_STALE_HOURS)
        prev_close_str = prev_cn_close.strftime("%Y-%m-%d %H:%M")
        note = (
            f"休市静默（报价停留在上一A股收盘前 {dt.strftime('%m-%d %H:%M')}，已过期 {age_h:.0f}h）"
            if is_stale else ""
        )
    return {
        "stale": is_stale,
        "holiday_silent": is_stale,
        "stale_hours": round(age_h, 1),
        "prev_cn_close": prev_close_str,
        "status_note": note,
    }


def _parse_sina_global(raw: str) -> dict[str, dict[str, Any]]:
    """解析新浪全球指数与期货行情（znb_* / gb_* / hf_*）。"""
    out: dict[str, dict[str, Any]] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line.startswith("var hq_str_") or '="' not in line:
            continue
        left, rest = line.split('="', 1)
        sym = left.replace("var hq_str_", "").strip()
        body = rest.rsplit('"', 1)[0]
        if not body:
            continue
        p = body.split(",")
        if sym.startswith("znb_") and len(p) >= 8:
            # znb_NKY="日经225,57011.40,-1633.23,-2.78,2:12 AM,ts,2026-11-25,14:30:01,..."
            px = _f(p[1])
            pct = _f(p[3])
            if px is not None and px > 0:
                out[sym] = {
                    "price": round(px, 2),
                    "pct": round(pct, 2) if pct is not None else None,
                    "as_of": f"{p[6]} {p[7]}".strip(),
                    "source": "sina",
                }
        elif sym.startswith("hf_") and len(p) >= 13:
            # hf_CHA50CFD="12366.84,,...,23:12:35,12455.00,...,2026-11-25,富时中国A50期货,..."
            px = _f(p[0])
            prev = _f(p[7]) or _f(p[8])
            if px is not None and px > 0:
                pct = ((px / prev - 1.0) * 100.0) if (prev and prev > 0) else None
                out[sym] = {
                    "price": round(px, 2),
                    "pct": round(pct, 2) if pct is not None else None,
                    "as_of": f"{p[12]} {p[6]}".strip(),
                    "source": "sina",
                }
        elif sym.startswith("gb_") and len(p) >= 5:
            # gb_ixic="纳斯达克,22941.30,-3.34,2026-11-25 23:12:29,-792.23,..."
            px = _f(p[1])
            pct = _f(p[2])
            if px is not None and px > 0:
                out[sym] = {
                    "price": round(px, 2),
                    "pct": round(pct, 2) if pct is not None else None,
                    "as_of": str(p[3]).strip(),
                    "source": "sina",
                }
    return out


def _parse_tencent_global(raw: str) -> dict[str, dict[str, Any]]:
    """解析腾讯美股/港股行情（v_us* / v_hk*）。"""
    out: dict[str, dict[str, Any]] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line.startswith("v_") or '="' not in line:
            continue
        left, rest = line.split('="', 1)
        sym = left[2:].strip()
        body = rest.rsplit('"', 1)[0]
        p = body.split("~")
        if len(p) < 33:
            continue
        px = _f(p[3])
        prev = _f(p[4])
        pct = _f(p[32])
        if pct is None and px and prev and prev > 0:
            pct = (px / prev - 1.0) * 100.0
        if px is not None and px > 0:
            out[sym] = {
                "price": round(px, 2),
                "pct": round(pct, 2) if pct is not None else None,
                "as_of": str(p[30]).strip(),
                "source": "tencent",
            }
    return out


def _fetch_sina(syms: list[str]) -> dict[str, dict[str, Any]]:
    if not syms:
        return {}
    url = "https://hq.sinajs.cn/list=" + ",".join(syms)
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"}
    )
    raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "replace")
    return _parse_sina_global(raw)


def _fetch_tencent(syms: list[str]) -> dict[str, dict[str, Any]]:
    if not syms:
        return {}
    url = "http://qt.gtimg.cn/q=" + ",".join(syms)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "replace")
    return _parse_tencent_global(raw)


def compute_overseas_score(indices: list[dict[str, Any]]) -> dict[str, Any]:
    """根据全球核心指数涨跌幅计算外盘情绪分（-100~+100）与风偏仓位系数。

    设计边界：
      - 日经225 + 韩国KOSPI 合占 40% 权重（A股早盘最直接的亚太风偏领先指标）；
      - 富时A50 + 恒生 + 中概合占 30%（外资对中国资产实时定价）；
      - 纳指 + 标普 + 费城半导体合占 30%（全球科技风险偏好）。
      - 额外硬预警：若日韩双跌 ≤ -1.5%，或韩国KOSPI ≤ -3.0%，或A50 ≤ -1.5%，
        即使均值未到极端也强制降档至 caution/risk_off。
    """
    by_key = {
        x["key"]: x for x in indices
        if x.get("pct") is not None and not x.get("stale") and not x.get("holiday_silent")
    }
    silenced = [
        f"{x.get('name') or x['key']}({x.get('stale_hours')}h)"
        for x in indices
        if x.get("stale") or x.get("holiday_silent")
    ]
    w_sum = 0.0
    w_pct = 0.0
    for spec in INDEX_SPECS:
        item = by_key.get(spec["key"])
        if not item:
            continue
        w = float(spec["weight"])
        w_sum += w
        w_pct += w * float(item["pct"])
    if w_sum <= 0:
        return {
            "score": None,
            "weighted_pct": None,
            "risk_level": "unknown",
            "risk_label": "外盘不可用",
            "size_mult": 1.0,
            "silenced_indices": silenced,
            "reasons": ["未获取到有效外盘指数报价" + (f"（休市静默：{', '.join(silenced)}）" if silenced else "")],
        }

    avg_pct = w_pct / w_sum
    # 线性映射到 [-100, +100]（±3.5% 对应 ±100 极端分）
    score = round(max(-100.0, min(100.0, (avg_pct / 3.5) * 100.0)), 1)

    kospi = (by_key.get("KOSPI") or {}).get("pct")
    n225 = (by_key.get("N225") or {}).get("pct")
    a50 = (by_key.get("A50") or {}).get("pct")
    ixic = (by_key.get("IXIC") or {}).get("pct")
    sox = (by_key.get("SOX") or {}).get("pct")

    reasons: list[str] = []
    if kospi is not None and n225 is not None and kospi <= -1.5 and n225 <= -1.5:
        reasons.append(f"日韩双杀（韩国 {kospi:+.2f}% / 日经 {n225:+.2f}%）")
    elif kospi is not None and kospi <= -2.5:
        reasons.append(f"韩国KOSPI大跌 {kospi:+.2f}%")
    if a50 is not None and a50 <= -1.2:
        reasons.append(f"富时A50走弱 {a50:+.2f}%")
    if ixic is not None and ixic <= -2.0:
        reasons.append(f"隔夜纳指大跌 {ixic:+.2f}%")
    if sox is not None and sox <= -3.0:
        reasons.append(f"费城半导体重挫 {sox:+.2f}%")
    if kospi is not None and n225 is not None and kospi >= 1.2 and n225 >= 1.2:
        reasons.append(f"日韩共振走强（韩国 {kospi:+.2f}% / 日经 {n225:+.2f}%）")
    if ixic is not None and ixic >= 1.5:
        reasons.append(f"隔夜纳指走强 {ixic:+.2f}%")

    severe_asia = (
        (kospi is not None and kospi <= -3.0)
        or (kospi is not None and n225 is not None and kospi <= -2.0 and n225 <= -2.0)
    )
    if score <= -45.0 or (severe_asia and score <= -25.0):
        level, label, mult = "risk_off", "外盘剧震 · 严控仓位", 0.70
    elif score <= -20.0 or (a50 is not None and a50 <= -1.5) or severe_asia:
        level, label, mult = "caution", "外盘偏弱 · 谨慎控仓", 0.85
    elif score >= 25.0:
        level, label, mult = "boost", "外盘偏暖 · 风偏支撑", 1.00
    else:
        level, label, mult = "neutral", "外盘中性", 1.00

    if silenced:
        reasons.append(f"休市静默（权重归零）：{'、'.join(silenced)}")
    if not reasons:
        reasons.append(f"加权外盘涨跌 {avg_pct:+.2f}%（情绪分 {score:+.1f}）")

    return {
        "score": score,
        "weighted_pct": round(avg_pct, 2),
        "risk_level": level,
        "risk_label": label,
        "size_mult": mult,
        "silenced_indices": silenced,
        "reasons": reasons,
    }


def compute_theme_catalysts(us_quotes: dict[str, dict[str, Any]],
                            indices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """根据美股领涨/领跌龙头计算映射到 A 股题材轴的催化强度（自动剔除休市过期数据）。"""
    idx_map = {
        x["key"]: x for x in indices
        if x.get("pct") is not None and not x.get("stale") and not x.get("holiday_silent")
    }
    out: list[dict[str, Any]] = []
    for spec in US_THEME_MAP:
        leaders_out: list[dict[str, Any]] = []
        pcts: list[float] = []
        for sym, cname, tx_sym in spec["leaders"]:
            q = us_quotes.get(tx_sym)
            if not q or q.get("pct") is None or q.get("stale") or q.get("holiday_silent"):
                continue
            pct = float(q["pct"])
            pcts.append(pct)
            leaders_out.append({"symbol": sym, "name": cname, "price": q["price"], "pct": pct})
        ref_k = spec.get("index_ref")
        if ref_k and ref_k in idx_map:
            pcts.append(float(idx_map[ref_k]["pct"]))
        if not pcts:
            continue
        avg_p = round(sum(pcts) / len(pcts), 2)
        hi_p = max(pcts)
        lo_p = min(pcts)
        if avg_p >= 2.0 or hi_p >= 4.5:
            direction, score = "bullish", (2 if (avg_p >= 3.0 or hi_p >= 6.0) else 1)
            note = f"美股领涨催化（均涨 {avg_p:+.2f}%），若A股同题材出兔则内外共振"
        elif avg_p <= -2.0 or lo_p <= -4.5:
            direction, score = "bearish", (-2 if (avg_p <= -3.5 or lo_p <= -6.0) else -1)
            note = f"美股承压拖累（均跌 {avg_p:+.2f}%），A股映射需防低开承压"
        else:
            direction, score = "neutral", 0
            note = f"美股表现平稳（均涨 {avg_p:+.2f}%）"
        out.append({
            "theme": spec["theme"],
            "label": spec["label"],
            "avg_pct": avg_p,
            "direction": direction,
            "catalyst_score": score,
            "leaders": leaders_out,
            "note": note,
        })
    out.sort(key=lambda x: -abs(x["avg_pct"]))
    return out


def archive_path(day: str | None = None) -> Path:
    d = str(day or _today8()).replace("-", "")[:8]
    return paths.live_dir() / d / "overseas.json"


def persist(snap: dict[str, Any], day: str | None = None) -> Path | None:
    if not snap or not snap.get("available"):
        return None
    p = archive_path(day)
    if not process_guard.can_write("overseas.persist", p):
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.stem + ".tmp.json")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def load_persisted(day: str | None = None) -> dict[str, Any] | None:
    p = archive_path(day)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def clear_cache() -> None:
    global _MEM_CACHE
    with _LOCK:
        _MEM_CACHE = None


#: 国内宽基指数 + 恒生科技（用于盘中「消息标记 → 期货/指数是否突然拉升确认市场认账」双阶段校验）
CN_REACTION_SPECS: list[dict[str, str]] = [
    {"key": "HSTECH", "name": "恒生科技", "tx": "hkHSTECH"},
    {"key": "SH",     "name": "上证指数", "tx": "sh000001"},
    {"key": "CYB",    "name": "创业板指", "tx": "sz399006"},
    {"key": "KC50",   "name": "科创50",   "tx": "sh000688"},
    {"key": "ZZ1000", "name": "中证1000", "tx": "sh000852"},
]

_REACTION_HISTORY: list[tuple[float, float]] = []  # (epoch_ts, composite_pct)


def _reaction_hist_path(day: str | None = None) -> Path:
    d = str(day or _today8()).replace("-", "")[:8]
    return paths.data_dir() / "live" / d / "reaction_history.jsonl"


def _load_reaction_history_if_empty(day: str | None = None) -> None:
    """进程重启后自动回载当日近 45 分钟突拉轨迹，避免重启归零。"""
    if _REACTION_HISTORY:
        return
    p = _reaction_hist_path(day)
    if not p.is_file():
        return
    cutoff = time.time() - 2700.0
    try:
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines()[-120:]:
            ln = ln.strip()
            if not ln:
                continue
            obj = json.loads(ln)
            ts = float(obj.get("ts") or 0.0)
            v = float(obj.get("comp") or 0.0)
            if ts >= cutoff:
                _REACTION_HISTORY.append((ts, v))
    except Exception:  # noqa: BLE001
        pass


def compute_market_reaction(
    indices: list[dict[str, Any]],
    cn_indices: list[dict[str, Any]],
    *,
    day: str | None = None,
    exclude_a50: bool = False,
) -> dict[str, Any]:
    """实时评估期货(A50/恒生科技) + 国内宽基指数(沪指/创业板/科创50/中证1000)的跟随与突然拉升斜率。

    设计原则：任何消息不能单看关键词，必须看期货与宽基指数是否真金白银跟随拉升；
    17:00 后 A50 已切入 T+1 夜盘，严禁混入当日日盘归因（exclude_a50=True）。
    """
    a50 = None if exclude_a50 else next(
        (x.get("pct") for x in indices if x.get("key") == "A50" and x.get("pct") is not None and not x.get("stale")),
        None,
    )
    hstech = next((x.get("pct") for x in cn_indices if x.get("key") == "HSTECH" and x.get("pct") is not None), None)
    cn_vals = [float(x["pct"]) for x in cn_indices if x.get("key") != "HSTECH" and x.get("pct") is not None]
    cn_avg = (sum(cn_vals) / len(cn_vals)) if cn_vals else None

    parts = [v for v in (a50, hstech, cn_avg) if v is not None]
    if not parts:
        return {
            "available": False, "verdict": "neutral", "sudden_surge": False,
            "a50_pct": None, "hstech_pct": None, "cn_avg_pct": None,
            "surge_delta_pct": None, "note": "期货与国内宽基实时报价不可用",
        }

    comp = sum(parts) / len(parts)
    now_t = time.time()
    with _LOCK:
        _load_reaction_history_if_empty(day)
        _REACTION_HISTORY.append((now_t, comp))
        # 仅保留近 45 分钟滚动轨迹，监测「盘中突然拉升」斜率并落盘防重启归零
        cutoff = now_t - 2700.0
        while _REACTION_HISTORY and _REACTION_HISTORY[0][0] < cutoff:
            _REACTION_HISTORY.pop(0)
        base_comp = min(v for _, v in _REACTION_HISTORY) if len(_REACTION_HISTORY) >= 2 else comp
        peak_comp = max(v for _, v in _REACTION_HISTORY) if len(_REACTION_HISTORY) >= 2 else comp
        try:
            hp = _reaction_hist_path(day)
            if process_guard.can_write("overseas.reaction_history", hp):
                hp.parent.mkdir(parents=True, exist_ok=True)
                with hp.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": round(now_t, 1), "comp": round(comp, 3)}) + "\n")
        except Exception:  # noqa: BLE001
            pass

    surge_delta = round(comp - base_comp, 2)
    fade_delta = round(comp - peak_comp, 2)
    sudden_surge = bool(
        surge_delta >= 1.2
        or (cn_avg is not None and cn_avg >= 2.0 and (a50 is None or a50 >= 0.8))
        or (hstech is not None and hstech >= 3.0 and (cn_avg is None or cn_avg >= 1.2))
    )
    if sudden_surge or (comp >= 1.0 and (cn_avg is None or cn_avg >= 0.6)):
        verdict = "confirm"
    elif fade_delta <= -1.2 or comp <= -1.0:
        verdict = "fade"
    else:
        verdict = "neutral"

    segs = []
    if a50 is not None:
        segs.append(f"A50 {a50:+.2f}%")
    elif exclude_a50:
        segs.append("A50(T+1夜盘已隔离)")
    if hstech is not None:
        segs.append(f"恒生科技 {hstech:+.2f}%")
    if cn_avg is not None:
        segs.append(f"国内宽基均 {cn_avg:+.2f}%")
    if surge_delta >= 1.2:
        segs.append(f"盘中突拉 +{surge_delta:.2f}pp")
    return {
        "available": True,
        "verdict": verdict,
        "sudden_surge": sudden_surge,
        "composite_pct": round(comp, 2),
        "a50_pct": round(float(a50), 2) if a50 is not None else None,
        "hstech_pct": round(float(hstech), 2) if hstech is not None else None,
        "cn_avg_pct": round(float(cn_avg), 2) if cn_avg is not None else None,
        "surge_delta_pct": surge_delta,
        "fade_delta_pct": fade_delta,
        "note": ("期货/宽基共振确认" if verdict == "confirm" else ("期货/宽基走弱回落" if verdict == "fade" else "期货/宽基平稳"))
                + ("（" + " · ".join(segs) + "）" if segs else ""),
    }


def snapshot(*, force: bool = False, save: bool = True,
             day: str | None = None) -> dict[str, Any]:
    """获取外盘指数情绪分与美股题材映射实时快照（动态变频缓存：平时15s / 异动突拉5s / 盘后120s）。"""
    global _MEM_CACHE
    now_m = time.monotonic()
    eff_ttl = _effective_ttl()
    with _LOCK:
        if not force and _MEM_CACHE is not None and now_m - _MEM_CACHE[0] < eff_ttl:
            return dict(_MEM_CACHE[1])

    sina_syms = [s["sina"] for s in INDEX_SPECS if s.get("sina")]
    tx_syms = list({
        *[s["tx"] for s in INDEX_SPECS if s.get("tx")],
        *[s["tx"] for s in CN_REACTION_SPECS],
        *[tx for m in US_THEME_MAP for _, _, tx in m["leaders"]],
    })

    sina_data: dict[str, dict[str, Any]] = {}
    tx_data: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    try:
        sina_data = _fetch_sina(sina_syms)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"sina:{type(exc).__name__}")
    try:
        tx_data = _fetch_tencent(tx_syms)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"tencent:{type(exc).__name__}")

    now_cn = datetime.now(_TZ_CN)
    d_target = str(day or _today8()).replace("-", "")[:8]
    is_today_cn = (d_target == now_cn.strftime("%Y%m%d"))

    # 校验腾讯美股报价新鲜度（us* 的 as_of 为美东时间，需转北京时间与当前比对）
    for sym, q in tx_data.items():
        if isinstance(q, dict) and sym.startswith("us"):
            q.update(_freshness_meta(q.get("as_of"), now_cn=now_cn, is_us_et=True))

    indices: list[dict[str, Any]] = []
    for spec in INDEX_SPECS:
        hit = None
        is_us_et = False
        if spec.get("sina") and spec["sina"] in sina_data:
            hit = sina_data[spec["sina"]]
        elif spec.get("tx") and spec["tx"] in tx_data:
            hit = tx_data[spec["tx"]]
            is_us_et = str(spec["tx"]).startswith("us")
        f_meta = _freshness_meta(
            hit.get("as_of") if hit else None,
            now_cn=now_cn,
            is_us_et=is_us_et,
        ) if hit else {"stale": False, "holiday_silent": False, "stale_hours": None, "status_note": ""}
        indices.append({
            "key": spec["key"],
            "name": spec["name"],
            "region": spec["region"],
            "weight": spec["weight"],
            "price": hit["price"] if hit else None,
            "pct": hit["pct"] if hit else None,
            "as_of": hit["as_of"] if hit else None,
            "source": hit["source"] if hit else None,
            **f_meta,
        })

    n_ok = sum(1 for x in indices if x.get("pct") is not None and not x.get("stale"))
    if n_ok == 0:
        disk = load_persisted(day)
        if disk:
            disk["stale_fallback"] = True
            return disk
        return {
            "available": False,
            "day": d_target,
            "indices": indices,
            "sentiment": compute_overseas_score(indices),
            "catalysts": [],
            "errors": errors,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    cn_indices: list[dict[str, Any]] = []
    for spec in CN_REACTION_SPECS:
        hit = tx_data.get(spec["tx"])
        cn_indices.append({
            "key": spec["key"],
            "name": spec["name"],
            "price": hit["price"] if hit else None,
            "pct": hit["pct"] if hit else None,
            "as_of": hit["as_of"] if hit else None,
        })

    sentiment = compute_overseas_score(indices)
    catalysts = compute_theme_catalysts(tx_data, indices)

    # 15:05 收盘后固化当日日盘指数共振快照，严禁用 17:00 后的 T+1 夜盘 A50 覆盖当日日盘归因
    disk_existing = load_persisted(d_target)
    frozen_day_reaction = None
    if isinstance(disk_existing, dict):
        frozen_day_reaction = disk_existing.get("day_session_reaction")
        if frozen_day_reaction is None:
            gen_s = str(disk_existing.get("generated_at") or "")
            if len(gen_s) >= 16 and gen_s[:10].replace("-", "") == d_target and gen_s[11:16] <= "15:15":
                mr_old = disk_existing.get("market_reaction")
                if isinstance(mr_old, dict) and mr_old.get("available"):
                    frozen_day_reaction = dict(mr_old)

    after_close_cn = is_today_cn and now_cn.time() >= dtime(15, 5)
    in_a50_night = is_today_cn and now_cn.time() >= dtime(17, 0)
    if after_close_cn and frozen_day_reaction is not None:
        market_reaction = {**frozen_day_reaction, "day_session_frozen": True}
    else:
        market_reaction = compute_market_reaction(
            indices, cn_indices, day=d_target, exclude_a50=in_a50_night,
        )
        if not in_a50_night and (not is_today_cn or now_cn.time() <= dtime(15, 15)):
            frozen_day_reaction = dict(market_reaction)
    if market_reaction.get("sudden_surge"):
        trigger_burst_mode(900.0)
        # 双向联动：市场/期货先异动突拉时，反过来立刻把快讯也切入 3s 极速档等待消息落地
        try:
            from backend import news_feed as _nf_burst
            _nf_burst.trigger_burst_mode(900.0)
        except Exception:  # noqa: BLE001
            pass
    snap = {
        "available": True,
        "day": d_target,
        "n_indices": n_ok,
        "indices": indices,
        "cn_indices": cn_indices,
        "market_reaction": market_reaction,
        "day_session_reaction": frozen_day_reaction,
        "sentiment": sentiment,
        "catalysts": catalysts,
        "catalyst_by_theme": {c["theme"]: c for c in catalysts},
        "errors": errors,
        "source": "新浪全球指数/期货 + 腾讯美港A股宽基实时行情",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if save:
        try:
            persist(snap, day=day)
        except Exception:  # noqa: BLE001
            pass
    with _LOCK:
        _MEM_CACHE = (now_m, snap)
    return dict(snap)
