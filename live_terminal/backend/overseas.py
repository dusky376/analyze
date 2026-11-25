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
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import paths

_LOCK = threading.RLock()
_MEM_CACHE: tuple[float, dict[str, Any]] | None = None
_TTL_SEC = 90.0

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
    by_key = {x["key"]: x for x in indices if x.get("pct") is not None}
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
            "reasons": ["未获取到有效外盘指数报价"],
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

    if not reasons:
        reasons.append(f"加权外盘涨跌 {avg_pct:+.2f}%（情绪分 {score:+.1f}）")

    return {
        "score": score,
        "weighted_pct": round(avg_pct, 2),
        "risk_level": level,
        "risk_label": label,
        "size_mult": mult,
        "reasons": reasons,
    }


def compute_theme_catalysts(us_quotes: dict[str, dict[str, Any]],
                            indices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """根据美股领涨/领跌龙头计算映射到 A 股题材轴的催化强度。"""
    idx_map = {x["key"]: x for x in indices if x.get("pct") is not None}
    out: list[dict[str, Any]] = []
    for spec in US_THEME_MAP:
        leaders_out: list[dict[str, Any]] = []
        pcts: list[float] = []
        for sym, cname, tx_sym in spec["leaders"]:
            q = us_quotes.get(tx_sym)
            if not q or q.get("pct") is None:
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


def snapshot(*, force: bool = False, save: bool = True,
             day: str | None = None) -> dict[str, Any]:
    """获取外盘指数情绪分与美股题材映射实时快照（带 90s 内存缓存与磁盘兜底）。"""
    global _MEM_CACHE
    now_m = time.monotonic()
    with _LOCK:
        if not force and _MEM_CACHE is not None and now_m - _MEM_CACHE[0] < _TTL_SEC:
            return dict(_MEM_CACHE[1])

    sina_syms = [s["sina"] for s in INDEX_SPECS if s.get("sina")]
    tx_syms = list({
        *[s["tx"] for s in INDEX_SPECS if s.get("tx")],
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

    indices: list[dict[str, Any]] = []
    for spec in INDEX_SPECS:
        hit = None
        if spec.get("sina") and spec["sina"] in sina_data:
            hit = sina_data[spec["sina"]]
        elif spec.get("tx") and spec["tx"] in tx_data:
            hit = tx_data[spec["tx"]]
        indices.append({
            "key": spec["key"],
            "name": spec["name"],
            "region": spec["region"],
            "weight": spec["weight"],
            "price": hit["price"] if hit else None,
            "pct": hit["pct"] if hit else None,
            "as_of": hit["as_of"] if hit else None,
            "source": hit["source"] if hit else None,
        })

    n_ok = sum(1 for x in indices if x.get("pct") is not None)
    if n_ok == 0:
        disk = load_persisted(day)
        if disk:
            disk["stale_fallback"] = True
            return disk
        return {
            "available": False,
            "day": str(day or _today8()),
            "indices": indices,
            "sentiment": compute_overseas_score([]),
            "catalysts": [],
            "errors": errors,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    sentiment = compute_overseas_score(indices)
    catalysts = compute_theme_catalysts(tx_data, indices)
    snap = {
        "available": True,
        "day": str(day or _today8()),
        "n_indices": n_ok,
        "indices": indices,
        "sentiment": sentiment,
        "catalysts": catalysts,
        "catalyst_by_theme": {c["theme"]: c for c in catalysts},
        "errors": errors,
        "source": "新浪全球指数/期货 + 腾讯美港股实时行情",
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
