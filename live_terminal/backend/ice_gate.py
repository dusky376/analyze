"""市场级冰点判定（v3.1 四维分位分级）· 悟空真经情绪体系第一层。

来源与口径
----------
本模块是精简前 `growing_samples.severity_level / is_ice_day / compute_bounds`
的**忠实移植**（算法逐行对齐，未改判据），只保留悟空真经需要的部分：
用真实日线（`feeds.build_panel()`）现算四维指标 → 分位档位 → 冰点判定 → 仓位系数。

四维（各自按全历史分位映射到 L0..L5，取最大值 S）
------------------------------------------------
1. ``down_share``    下跌家数占比（广度）
2. ``limit_down_n``  跌停家数（杀跌烈度）—— 用户 2026-11-16：「跌停数量越大，等级应该越高」
3. ``dt_ratio``      跌停 / 涨停（多空强弱）
4. ``broken_hi_n``   昨日≥3板今日未涨停家数（高标负反馈）

判据（v3.1，唯一）：``is_ice = (S>=L4 且至少 2 维>=L3) 或 limit_down_n>=40``

为什么不是「任一维 L4 即冰」
--------------------------
2026-11-17 验收修复：旧口径在 694 天里判出 157 天冰点（22.6%），冰点失去稀缺性；
移出的 45 天全部是「只有 broken_hi_n 一维高」的普通高位分歧日。

仓位（用户 2026-11-23 裁定）
--------------------------
「判断冰点可以小仓试错，控制仓位就行了，不能太绝对了。」
因此冰点**不直接空仓**，而是给出仓位系数：
    L5 冰点 → 0.30（三成仓试错）
    L4 冰点 → 0.50（半仓）
    非冰点   → 1.00（正常仓位）
系数只缩放 `position_size` 的金额，不改选股与成交判定。

诚实边界
--------
切分验证（备份 `data/icepoint_split.md`）证明：用 2024-01~2025-06 学到的分位边界，
外推到 2025-07 之后的 17 个用户标注冰点日，命中 **0/17** —— 冰点形态会漂移。
因此分位边界必须**随数据滚动重算**（本模块每次现算），且结论应标注「形态可能漂移」，
不得把历史冰点阈值当成永久真理。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import polars as pl

from backend import feature_engine, feeds

#: 四维关键字（顺序固定，便于展示与测试）
DIM_KEYS: tuple[str, ...] = ("down_share", "limit_down_n", "dt_ratio", "broken_hi_n")

#: 档位顺序（索引越大越严重）
LEVEL_ORDER: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4", "L5")

#: 冰点硬旁路：跌停家数达到此值直接判冰（v3.1 原判据的一部分）
ICE_LIMIT_DOWN_ABS = 40

#: 仓位系数（用户 2026-11-23 裁定：冰点小仓试错，不绝对空仓）
SIZE_FACTOR: dict[str, float] = {"L5": 0.30, "L4": 0.50}
SIZE_FACTOR_DEFAULT = 1.00

#: 分位档位边界（P50/P70/P80/P90/P95）。无历史分位表时用此兜底，
#: 数值取自备份 `ice_point_samples.json` 的 severity_bounds（2026-11 全历史快照）。
DEFAULT_BOUNDS: dict[str, dict[str, float]] = {
    "down_share": {"P50": 0.507, "P70": 0.6645, "P80": 0.7452, "P90": 0.8337, "P95": 0.8883},
    "limit_down_n": {"P50": 9.0, "P70": 15.0, "P80": 21.0, "P90": 37.0, "P95": 60.0},
    "dt_ratio": {"P50": 0.1509, "P70": 0.2857, "P80": 0.4333, "P90": 0.7576, "P95": 1.4706},
    "broken_hi_n": {"P50": 2.0, "P70": 3.0, "P80": 4.0, "P90": 6.0, "P95": 8.0},
}


def _finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None          # 排除 NaN


def level_by_pct(x: Any, bounds: dict[str, float] | None) -> str | None:
    """按分位边界把标量映射到 L0..L5（与 v3.1 `_level_by_pct` 一致）。"""
    fv = _finite(x)
    if fv is None or not bounds:
        return None
    if fv > bounds.get("P95", 9):
        return "L5"
    if fv > bounds.get("P90", 9):
        return "L4"
    if fv > bounds.get("P80", 9):
        return "L3"
    if fv > bounds.get("P70", 9):
        return "L2"
    if fv > bounds.get("P50", 9):
        return "L1"
    return "L0"


def dim_levels(rec: dict[str, Any],
               bounds: dict[str, Any] | None = None) -> dict[str, str | None]:
    """逐维度档位（v3.1 `dim_levels`）。"""
    b = bounds if bounds is not None else DEFAULT_BOUNDS
    out: dict[str, str | None] = {}
    if not isinstance(rec, dict):
        return {k: None for k in DIM_KEYS}
    for k in DIM_KEYS:
        bd = (b or {}).get(k) or {}
        out[k] = level_by_pct(rec.get(k), bd)
    return out


def severity(dims: dict[str, str | None]) -> str | None:
    """S = max(L_k)（v3.1 `severity_level`）。"""
    lvls = [v for v in dims.values() if v in LEVEL_ORDER]
    if not lvls:
        return None
    return max(lvls, key=LEVEL_ORDER.index)


def is_ice(rec: dict[str, Any], dims: dict[str, str | None] | None = None) -> bool:
    """**唯一冰点判据**（v3.1）：`S>=L4 且 ≥2 维>=L3`，或 `跌停>=40`。"""
    d = dims if dims is not None else dim_levels(rec)
    s = severity(d)
    n_ge3 = sum(1 for v in d.values()
                if v in LEVEL_ORDER and LEVEL_ORDER.index(v) >= 3)
    by_multi = bool(s and LEVEL_ORDER.index(s) >= 4 and n_ge3 >= 2)
    by_abs = (_finite(rec.get("limit_down_n")) or 0) >= ICE_LIMIT_DOWN_ABS
    return by_multi or by_abs


def size_factor(level: str | None, ice: bool) -> float:
    """冰点 → 仓位系数（用户裁定：小仓试错，不绝对空仓）。"""
    if not ice:
        return SIZE_FACTOR_DEFAULT
    return SIZE_FACTOR.get(level or "", 0.50)


def compute_bounds(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """从历史逐日指标现算分位边界（v3.1 `compute_bounds`，随市场滚动）。"""
    out: dict[str, dict[str, float]] = {}
    for col in DIM_KEYS:
        arr = [f for f in (_finite(r.get(col)) for r in rows) if f is not None]
        if len(arr) < 30:
            continue
        arr.sort()
        n = len(arr)

        def q(p: float) -> float:
            i = min(int(p * n), n - 1)
            return round(arr[i], 4)

        out[col] = {"P50": q(0.50), "P70": q(0.70), "P80": q(0.80),
                    "P90": q(0.90), "P95": q(0.95), "n": n,
                    "min": round(arr[0], 4), "max": round(arr[-1], 4)}
    return out


# ---------------------------------------------------------------------------
# 真实日线 → 逐日四维指标
# ---------------------------------------------------------------------------

def _metrics_frame(panel: pl.DataFrame) -> pl.DataFrame:
    """一次性向量化算全历史逐日四维指标（避免 O(天×股) 重复扫描）。

    Returns: DataFrame[trade_date(str), down_share, limit_down_n, limit_up_n,
             dt_ratio, broken_hi_n, up, down]，按日期升序。
    """
    # 涨跌幅 + 涨跌停价幅度（按代码板块）
    df = (panel.select(["ts_code", "trade_date", "close"])
          .sort(["ts_code", "trade_date"])
          .with_columns(pl.col("close").shift(1).over("ts_code").alias("prev_close")))

    lim = pl.col("ts_code").map_elements(
        lambda c: float(feature_engine.limit_pct(str(c))) * 100.0,
        return_dtype=pl.Float64).alias("lim_pct")
    df = df.with_columns(lim).with_columns(
        ((pl.col("close") / pl.col("prev_close") - 1.0) * 100.0).alias("pct"))

    # 涨停 / 跌停标记（留 0.3 个百分点容差，与 feature_engine 口径一致）
    df = df.with_columns([
        ((pl.col("pct") >= pl.col("lim_pct") - 0.3)).fill_null(False).alias("is_zt"),
        ((pl.col("pct") <= -(pl.col("lim_pct") - 0.3))).fill_null(False).alias("is_dt"),
        ((pl.col("pct") > 0)).fill_null(False).alias("is_up"),
        ((pl.col("pct") < 0)).fill_null(False).alias("is_down"),
    ])

    # 高标负反馈：昨日连板>=3 且今日未涨停。
    # streak = 连续涨停天数：按 (股票, 非涨停分组的累计) 对涨停计数。
    df = df.with_columns([
        pl.col("is_zt").cast(pl.Int32).alias("_zt_i"),
        (~pl.col("is_zt")).cast(pl.Int32).cum_sum().over("ts_code").alias("_off_run"),
    ])
    df = df.with_columns(
        pl.when(pl.col("is_zt"))
        .then(pl.col("_zt_i").cum_sum().over(["ts_code", "_off_run"]))
        .otherwise(0).cast(pl.Int32).alias("streak"))
    df = df.with_columns(pl.col("streak").shift(1).over("ts_code")
                         .fill_null(0).alias("prev_streak"))
    df = df.with_columns(
        ((pl.col("prev_streak") >= 3) & (~pl.col("is_zt"))).alias("hi_broken"))

    # 按日聚合
    agg = (df.filter(pl.col("pct").is_not_null())
           .group_by("trade_date")
           .agg([
               pl.col("is_up").sum().alias("up"),
               pl.col("is_down").sum().alias("down"),
               pl.col("is_zt").sum().alias("limit_up_n"),
               pl.col("is_dt").sum().alias("limit_down_n"),
               pl.col("hi_broken").sum().alias("broken_hi_n"),
           ])
           .sort("trade_date"))

    tot = pl.col("up") + pl.col("down")
    agg = agg.with_columns([
        pl.when(tot > 0).then(pl.col("down") / tot).otherwise(None)
        .round(4).alias("down_share"),
        pl.when(pl.col("limit_up_n") > 0)
        .then(pl.col("limit_down_n") / pl.col("limit_up_n")).otherwise(None)
        .round(4).alias("dt_ratio"),
    ])
    return agg.with_columns(pl.col("trade_date").cast(pl.Utf8).alias("day")).drop("trade_date")


_METRICS_CACHE: tuple[int, pl.DataFrame] | None = None


def metrics_all(panel: pl.DataFrame | None = None) -> pl.DataFrame:
    """全历史逐日四维指标（按 panel 对象 id + 高度缓存，避免重复计算）。"""
    global _METRICS_CACHE
    p = panel if panel is not None else feeds.build_panel()
    key = (id(p), p.height)
    if _METRICS_CACHE is not None and _METRICS_CACHE[0] == key:
        return _METRICS_CACHE[1]
    m = _metrics_frame(p)
    _METRICS_CACHE = (key, m)
    return m


def _norm(d: Any) -> str:
    s = str(d).replace("-", "")[:10]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s[:10]


def day_metrics(panel: pl.DataFrame, day: str) -> dict[str, Any] | None:
    """某日的四维原始指标（由向量化结果查表，全部来自真实日线）。"""
    d = _norm(day)
    m = metrics_all(panel)
    row = m.filter(pl.col("day") == d)
    if row.height == 0:
        return None
    r = row.to_dicts()[0]
    return {
        "day": d,
        "down_share": r.get("down_share"),
        "limit_down_n": int(r.get("limit_down_n") or 0),
        "limit_up_n": int(r.get("limit_up_n") or 0),
        "dt_ratio": r.get("dt_ratio"),
        "broken_hi_n": int(r.get("broken_hi_n") or 0),
        "up": int(r.get("up") or 0),
        "down": int(r.get("down") or 0),
    }


def history(panel: pl.DataFrame, *, days: int = 250) -> list[dict[str, Any]]:
    """最近 N 个交易日的四维指标（用于现算分位边界）。"""
    m = metrics_all(panel).tail(days)
    return [{**r, "day": r["day"]} for r in m.to_dicts()]


def assess(day: str, *, panel: pl.DataFrame | None = None,
           bounds: dict[str, Any] | None = None,
           roll_bounds: bool = True) -> dict[str, Any]:
    """评估某日冰点状态 + 仓位系数（只读，不写任何文件）。

    Args:
        day: 'YYYYMMDD' 或 'YYYY-MM-DD'
        panel: 可注入（便于测试）；None 时读 feeds.build_panel()
        bounds: 可注入分位边界；None 时按 roll_bounds 决定
        roll_bounds: True = 用最近 250 日现算分位（跟随市场）；
                     False = 用 DEFAULT_BOUNDS 快照（便于复现历史）
    """
    d = _norm(day)
    p = panel if panel is not None else feeds.build_panel()
    rec = day_metrics(p, d)
    if rec is None:
        return {
            "available": False,
            "day": d,
            "reason": f"面板无 {d} 或无前一交易日",
            "is_ice": False,
            "level": None,
            "size_factor": SIZE_FACTOR_DEFAULT,
        }

    if bounds is None:
        bounds = compute_bounds(history(p, days=250)) if roll_bounds else DEFAULT_BOUNDS
        if not bounds:                       # 历史不足 → 退回快照，不静默失效
            bounds = DEFAULT_BOUNDS

    dims = dim_levels(rec, bounds)
    s = severity(dims)
    ice = is_ice(rec, dims)
    factor = size_factor(s, ice)
    reasons = []
    if ice:
        n_ge3 = sum(1 for v in dims.values()
                    if v in LEVEL_ORDER and LEVEL_ORDER.index(v) >= 3)
        if s and LEVEL_ORDER.index(s) >= 4 and n_ge3 >= 2:
            reasons.append(f"多维确认：S={s} 且 {n_ge3} 维≥L3")
        if (_finite(rec.get("limit_down_n")) or 0) >= ICE_LIMIT_DOWN_ABS:
            reasons.append(f"跌停 {rec['limit_down_n']} 家 ≥ {ICE_LIMIT_DOWN_ABS}")

    return {
        "available": True,
        "day": d,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": rec,
        "dim_levels": dims,
        "level": s,
        "is_ice": ice,
        "size_factor": factor,
        "reasons": reasons,
        "verdict": ("冰点 → 小仓试错" if ice else "非冰点 → 正常仓位"),
        "bounds_source": ("rolling_250d" if roll_bounds else "snapshot"),
        "bounds": bounds,
        "caveat": ("分位边界随市场滚动现算；备份切分验证显示冰点形态会漂移"
                   "（OOS 种子命中 0/17），不得把历史阈值当永久真理。"),
    }


if __name__ == "__main__":
    import json
    import sys
    from datetime import datetime as _dt

    day = sys.argv[1] if len(sys.argv) > 1 else _dt.now().strftime("%Y%m%d")
    r = assess(day)
    brief = {k: v for k, v in r.items() if k != "bounds"}
    print(json.dumps(brief, ensure_ascii=False, indent=2, default=str))
