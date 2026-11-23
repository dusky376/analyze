"""卖法对照（只读研究）：现行卖法 vs 固定止盈止损（v3 E 方案）+ 当时环境快照。

设计边界（严格遵守）
------------------------------------------------
1. **纯计算、只读**：不写账本、不产生买卖点、不 import live_sim.sync，
   更不会调用 sync。对 live_sim 只复用 :func:`emotion_gate`（纯读面板的函数）。
2. **严格复用 v3 严谨算法**：E 方案（entry×0.93 止损 / entry×1.15 止盈，
   20 交易日持有窗口，一字跌停不可成交需顺延，止损成交价 min(sl, open)、
   止盈 max(tp, open)），以及 daily_for / is_dt / oneword_dt / try_close
   的判据。口径逐行对齐 `research_tmp/exit_timing_study_v3.py`，
   唯一差异是 A（现行）收益直接取账本 ``gross_return``
   （避免重算与账本不一致；v3 里 A 是重算值，通常只差成本）。
3. **数据只取需要的 code**：panel 已由 feeds 缓存，按 code 过滤只处理账本里的
   8 只票；emotion_gate 内部扫全市场较慢（约 2s/日），按买入日缓存去重。

对外接口：:func:`analyze()`，返回字典（所有浮点经 json_safe.sanitize 净化，
NaN/±Inf → None，可直接 jsonify）。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from backend import feature_engine, feeds, json_safe, live_sim, paths, trading_rules

TERMINAL = Path(__file__).resolve().parents[1]
LIVE_DIR = paths.data_dir() / "live"


def _state_file() -> Path:
    """账本路径：**运行时读** `live_sim.STATE_FILE`。

    2026-11-23 审计 P1-10：旧实现 import 时硬编码，绕过了
    `tests/conftest.py` 的账本隔离（测试会读到生产账本）。
    """
    sf = getattr(live_sim, "STATE_FILE", None)
    if isinstance(sf, Path):
        return sf
    return TERMINAL / "data" / "paper" / "sim_state.json"

# v3 常量：固定止盈止损与持有窗口（与 exit_timing_study_v3.py L116-128 一致）
STOP_LOSS_RATE = 0.93
TAKE_PROFIT_RATE = 1.15
HOLD_DAYS = 20
DT_BUFFER = 0.003       # is_dt 容差（跌幅 ≤ -(lim-0.003)）
ZT_TOLERANCE = 0.011    # 一字跌停判定：high ≤ 跌停价 + 0.011

CAVEATS = [
    "本模块为只读描述性对照，不修改账本、不触发任何交易。",
    "固定止损 -7% / 止盈 +15%、持有窗口 20 交易日，口径复用 exit_timing_study_v3.py 的 E 方案。",
    "现行卖法收益直接取账本 gross_return（未重算），与止盈止损模拟口径（未扣交易成本）略有差异。",
    "一字跌停（全天封死）无法成交时止损顺延；跳空低开按开盘价成交，跳空高开止盈按开盘价成交。",
    "情绪闸门/高标快照取买入日 emotion_gate；市场环境取断板日 breadth.jsonl 最后一行；缺数据则为 None。",
    "按环境分组（by_env）只做描述性切分，未做显著性检验；n<5 的组标 small_sample=true，"
    "不得据此认为「某环境下固定止损更有效」——分组越多、多重比较偶然越大。",
    "样本量小（已结算交易笔数有限），结论仅供参考，不构成策略建议。",
]


# ---------------------------------------------------------------------------
# v3 判据复用（daily_for / is_dt / oneword_dt / try_close）
# ---------------------------------------------------------------------------

def _norm(d: Any) -> str:
    """'20261116' / date(2026,11,16) / '2026-11-16' → '2026-11-16'。"""
    s = str(d).replace("-", "")[:10]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s[:10]


def _compact(d: str) -> str:
    """'2026-11-16' → '20261116'（breadth 目录 / gate 入参格式）。"""
    return str(d).replace("-", "").replace("/", "")[:8]


def daily_for(code: str, panel: pl.DataFrame, cal: list[str]) -> list[dict[str, Any]]:
    """与 v3.daily_for 一致：单票日线 + 涨停/跌停价标记（只读，按 code 过滤）。"""
    df = panel.filter(pl.col("ts_code") == str(code)).sort("trade_date")
    if df.height == 0:
        return []
    c = df["close"].to_numpy().astype(float)
    ds = [_norm(x) for x in df["trade_date"].to_list()]
    pv = feature_engine.prev_day_valid(ds, cal)
    zt, _ = feature_engine.zt_flags(code, c, prev_valid=pv)
    lim = feature_engine.limit_pct(code)
    out: list[dict[str, Any]] = []
    for i, d in enumerate(ds):
        p = c[i - 1] if i > 0 else float("nan")
        out.append({
            "date": d,
            "open": float(df["open"][i]),
            "high": float(df["high"][i]),
            "low": float(df["low"][i]),
            "close": float(c[i]),
            "pct": (c[i] / p - 1.0) if (p == p and p > 0) else float("nan"),
            "zt": bool(zt[i]),
            "lim": lim,
            "dtp": (float(trading_rules.round_price(p * (1 - lim)))
                    if (p == p and p > 0) else float("nan")),
        })
    return out


def is_dt(r: dict[str, Any]) -> bool:
    """与 v3.is_dt 一致：收盘跌幅达跌停阈值。"""
    p = r["pct"]
    return (p == p) and bool(p <= -(r["lim"] - DT_BUFFER))


def oneword_dt(r: dict[str, Any]) -> bool:
    """与 v3.oneword_dt 一致：收盘一字跌停（全天封死，无法卖出）。"""
    return is_dt(r) and (r["high"] <= r["dtp"] + ZT_TOLERANCE)


def try_close(r: dict[str, Any], why: str = "") -> dict[str, Any] | None:
    """与 v3.try_close 一致：收盘跌停封死不可成交。"""
    if is_dt(r):
        return None
    return {"date": r["date"], "price": r["close"],
            "note": f"收盘@{r['close']:.2f} {why}".strip()}


def _ret(px: float | None, e: float | None) -> float | None:
    """(卖出价/买入价 - 1)；任一侧非法返回 None（不抛 ZeroDivisionError）。

    2026-11-23 审计 P0-6：旧实现缺 e>0 守卫，账本里出现 entry_price=0
    会让整个 analyze() 抛 ZeroDivisionError → /api/exit-alternatives 直接 500。
    """
    if px is None or e is None:
        return None
    try:
        p = float(px)
        b = float(e)
    except (TypeError, ValueError):
        return None
    if not (b > 0) or not (p == p):
        return None
    return p / b - 1.0


def _simulate_e(d: list[dict[str, Any]], ei: int, entry_price: float
                ) -> tuple[dict[str, Any] | None, float | None, str]:
    """v3 E 方案核心（逐行对齐 v3 L116-128）。

    - 一字跌停不可成交 → 该日止损跳过（顺延到后续交易日）；
    - 止损成交价 min(sl, open)，止盈成交价 max(tp, open)；
    - 20 日内未触价 → 用现行 A（断板日收盘，可顺延）兜底；
    - 返回 (exit_detail, return, break_date)；exit_detail 为 None 表示无可用兜底。
    """
    after = d[ei + 1:]
    if not after:
        return None, None, ""
    win = after[:HOLD_DAYS]
    bi = next((k for k, r in enumerate(after) if not r["zt"]), None)
    if bi is None:
        # v3 会 continue 跳过该笔；这里降级为无模拟结果（保留账本 current）
        return None, None, ""
    bd = after[bi]["date"]

    # A（v3 口径，仅用作 E 未触价时兜底；不是本模块对外的 current）
    a: dict[str, Any] | None = None
    for k in range(bi, len(after)):
        a = try_close(after[k], "断板日" if k == bi else f"顺延{k - bi}日")
        if a:
            break

    tp, sl = entry_price * TAKE_PROFIT_RATE, entry_price * STOP_LOSS_RATE
    e: dict[str, Any] | None = None
    for r in win:
        if r["low"] <= sl and not oneword_dt(r):
            px = min(sl, r["open"]) if r["open"] > 0 else sl
            e = {"date": r["date"], "price": px,
                 "note": f"止损@{px:.2f}（low={r['low']:.2f}）"}
            break
        if r["high"] >= tp:
            px = max(tp, r["open"]) if r["open"] > 0 else tp
            e = {"date": r["date"], "price": px,
                 "note": f"止盈@{px:.2f}（high={r['high']:.2f}）"}
            break
    if e is None:
        e = dict(a, note=(a["note"] if a else "") + "（E未触价→断板收盘兜底）") if a else None
    return e, (_ret(e["price"], entry_price) if e else None), bd


# ---------------------------------------------------------------------------
# 环境快照（gate / breadth），全部只读
# ---------------------------------------------------------------------------

def _gate_snapshot(entry_date: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """买入日情绪闸门（import live_sim.emotion_gate 但不调用 sync）。失败给 None。"""
    snap: dict[str, Any] = {
        "gate_blocked": None, "gate_reasons": [],
        "high_avg_ret": None, "high_limit_down_share": None,
    }
    day = _compact(entry_date)
    if day not in cache:
        try:
            cache[day] = live_sim.emotion_gate(day)
        except Exception as exc:  # noqa: BLE001 - 快照失败不影响主计算
            cache[day] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    g = cache[day]
    if g.get("available"):
        snap["gate_blocked"] = bool(g.get("blocked"))
        snap["gate_reasons"] = list(g.get("reasons") or [])
        snap["high_avg_ret"] = g.get("avg_ret")
        snap["high_limit_down_share"] = g.get("limit_down_share")
    return snap


def _breadth_snapshot(break_date: str) -> dict[str, Any] | None:
    """断板日 data/live/<YYYYMMDD>/breadth.jsonl 最后一行。无文件/坏行 → None。"""
    if not break_date:
        return None
    f = LIVE_DIR / _compact(break_date) / "breadth.jsonl"
    if not f.is_file():
        return None
    try:
        last = None
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return None
        obj = json.loads(last)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):  # noqa: BLE001 - 快照容错
        return None


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def _stats(vals: list[float]) -> dict[str, Any]:
    """n / 胜率 / 均值 / 中位数（v3.stats 口径，胜 = ret > 0）。"""
    v = [x for x in vals if x is not None]
    n = len(v)
    if not n:
        return {"n": 0, "win_rate": None, "mean": None, "median": None}
    s = sorted(v)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    win = sum(1 for x in v if x > 0)
    return {"n": n, "win_rate": win / n, "mean": sum(v) / n, "median": median}


def _load_closed_trades() -> list[dict[str, Any]]:
    """只读账本，取已结算（gross_return 为数值）的交易，按 id 稳定排序。"""
    sf = _state_file()
    if not sf.is_file():
        return []
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    trades = data.get("trades") or []
    out = []
    for t in trades:
        g = t.get("gross_return")
        if isinstance(g, (int, float)) and not isinstance(g, bool) and math.isfinite(float(g)):
            out.append(t)
    return sorted(out, key=lambda t: str(t.get("id") or ""))


# 环境分组维度 key（分组逻辑见 `_bucket`，标签在 `_bucket` 内按维度返回）。
_ENV_DIMS: tuple[str, ...] = (
    "break_limit_down",
    "gate",
    "high_avg_ret",
    "high_limit_down_share",
)


def _bucket(trade: dict[str, Any], dim: str) -> str | None:
    """把一笔交易映射到某个环境分组标签；无法判定返回 None（不进分组）。"""
    ctx = trade.get("context") or {}
    if dim == "break_limit_down":
        v = ctx.get("break_is_limit_down")
        if v is True:
            return "断板跌停"
        if v is False:
            return "断板未跌停"
        return None
    if dim == "gate":
        v = ctx.get("gate_blocked")
        if v is True:
            return "闸门拦截"
        if v is False:
            return "闸门通过"
        return None
    if dim == "high_avg_ret":
        v = ctx.get("high_avg_ret")
        if v is None:
            return None
        try:
            return "昨日高标均涨>0" if float(v) > 0 else "昨日高标均涨≤0"
        except (TypeError, ValueError):
            return None
    if dim == "high_limit_down_share":
        v = ctx.get("high_limit_down_share")
        if v is None:
            return None
        try:
            return "高标跌停占比≥20%" if float(v) >= 0.20 else "高标跌停占比<20%"
        except (TypeError, ValueError):
            return None
    return None


def _group_summary(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按环境维度分组，给出每组的现行/止损收益对比。

    只做描述性分组，不做显著性检验；n 很小的组会如实标出。
    """
    out: list[dict[str, Any]] = []
    for key in _ENV_DIMS:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for t in trades:
            b = _bucket(t, key)
            if b:
                buckets.setdefault(b, []).append(t)
        for label in sorted(buckets):
            rows = buckets[label]
            cur = _stats([r.get("current_return") for r in rows])
            sl = _stats([r.get("stoploss_return") for r in rows])
            delta = None
            if cur["mean"] is not None and sl["mean"] is not None:
                delta = (sl["mean"] - cur["mean"]) * 100.0
            out.append({
                "dimension": key,
                "bucket": label,
                "n": len(rows),
                "current": cur,
                "stoploss": sl,
                "delta_mean_pp": delta,
                "small_sample": len(rows) < 5,
            })
    return out


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def analyze() -> dict[str, Any]:
    """计算每笔已结算交易的现行卖法 vs 固定止盈止损对照 + 环境快照。

    返回结构见模块文档 / lead 的接口约定；所有浮点已通过 json_safe.sanitize。
    """
    trades = _load_closed_trades()
    if not trades:
        return json_safe.sanitize({
            "available": False,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "n": 0,
            "summary": {
                "current": {"n": 0, "win_rate": None, "mean": None, "median": None},
                "stoploss": {"n": 0, "win_rate": None, "mean": None, "median": None},
                "delta_mean_pp": None,
            },
            "by_env": [],
            "trades": [],
            "caveats": CAVEATS,
        })

    panel = feeds.build_panel()
    if panel is None or panel.height == 0:
        raise RuntimeError("历史面板不可用，无法计算卖法对照")
    cal = sorted({str(d)[:10] for d in panel["trade_date"].unique().to_list()})

    # 只处理账本里出现的 code（按 code 过滤，避免全市场扫描）
    codes = sorted({str(t["ts_code"]) for t in trades})
    daily: dict[str, list[dict[str, Any]]] = {
        c: daily_for(c, panel, cal) for c in codes
    }

    gate_cache: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for t in trades:
        code = str(t["ts_code"])
        entry_date = _norm(t["entry_date"])
        entry_price = float(t["entry_price"])
        current = float(t["gross_return"])  # 账本值，不重算
        d = daily.get(code, [])
        idx = {r["date"]: i for i, r in enumerate(d)}
        ei = idx.get(entry_date)

        exit_detail: dict[str, Any] | None = None
        stoploss: float | None = None
        break_date = ""
        if ei is not None:
            exit_detail, stoploss, break_date = _simulate_e(d, ei, entry_price)

        row: dict[str, Any] = {
            "id": t.get("id"),
            "code": code,
            "name": t.get("name"),
            "entry_date": entry_date,
            "entry_price": entry_price,
            "current_return": current,
            "stoploss_return": stoploss,
            "delta_pp": ((stoploss - current) * 100.0
                         if stoploss is not None else None),
            "stoploss_exit_date": exit_detail["date"] if exit_detail else "",
            "stoploss_note": exit_detail["note"] if exit_detail else "无可用日线/断板数据，未模拟",
            "context": {
                **_gate_snapshot(entry_date, gate_cache),
                "break_is_limit_down": None,
                "break_date": break_date,
                "market_breadth": None,
            },
        }
        if ei is not None and break_date:
            bi = next((k for k, r in enumerate(d[ei + 1:]) if not r["zt"]), None)
            if bi is not None:
                row["context"]["break_is_limit_down"] = is_dt(d[ei + 1:][bi])
        row["context"]["market_breadth"] = _breadth_snapshot(break_date)
        rows.append(row)

    current_stats = _stats([r["current_return"] for r in rows])
    stoploss_stats = _stats([r["stoploss_return"] for r in rows])
    delta = None
    if current_stats["mean"] is not None and stoploss_stats["mean"] is not None:
        delta = (stoploss_stats["mean"] - current_stats["mean"]) * 100.0

    out = {
        "available": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n": len(rows),
        "summary": {
            "current": current_stats,
            "stoploss": stoploss_stats,
            "delta_mean_pp": delta,
        },
        "by_env": _group_summary(rows),
        "trades": rows,
        "caveats": CAVEATS,
    }
    return json_safe.sanitize(out)


if __name__ == "__main__":
    print(json.dumps(analyze(), ensure_ascii=False, indent=2, default=str))
