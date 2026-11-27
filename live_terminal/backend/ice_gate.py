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
import threading

import polars as pl

from backend import feature_engine, feeds

#: 四维关键字（顺序固定，便于展示与测试）
DIM_KEYS: tuple[str, ...] = ("down_share", "limit_down_n", "dt_ratio", "broken_hi_n")

#: 档位顺序（索引越大越严重）
LEVEL_ORDER: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4", "L5")

#: 冰点硬旁路：跌停家数达到此值直接判冰（v3.1 原判据的一部分）
ICE_LIMIT_DOWN_ABS = 40

#: 2026-11-25 用户裁定新增：**普跌旁路**。
#: 背景：11-25 全市场 4614 跌 / 830 涨（下跌占比 84.75%），但跌停仅 11 家，
#: v3.1 判据 `(S>=L4 且 >=2维>=L3) 或 跌停>=40` 判成「非冰点 → 正常仓位」，
#: 导致照常满仓买入 20 只票、当日集体跳水。
#: 「普跌但无恐慌跌停」也是弱势，必须降仓。下跌占比 >= 此值即判冰点。
ICE_DOWN_SHARE_ABS = 0.80

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

#: 普跌细分先验仓位系数（仅作冷启动先验，随近 250 日滚动修复率与实盘因果结算盈亏动态演化）
REGIME_META: dict[str, dict[str, Any]] = {
    "external_shock": {
        "cn": "外盘冲击型普跌",
        "prior_mult": 0.40,
        "hint": "外盘大跌拖累开盘普跌，但内资高标仍有承接；降仓试错国产替代/内需独立题材，回避外资重仓与出海链。",
    },
    "panic_collapse": {
        "cn": "情绪退潮·核按钮型普跌",
        "prior_mult": 0.25,
        "hint": "跌停潮或高标集体断板负反馈扩散；严控最小试错仓，非极强硬核前排一字带动绝不硬接。",
    },
    "narrow_siphon": {
        "cn": "抱团虹吸·分化型普跌",
        "prior_mult": 0.55,
        "hint": "指数/微盘普跌但无恐慌跌停且连板活跃，属资金向主线抱团虹吸；半仓聚焦绝对前排主线。",
    },
    "volume_drain": {
        "cn": "缩量失血·阴跌型普跌",
        "prior_mult": 0.35,
        "hint": "量能跌至近 250 日低分位、无跌停潮但涨停稀少；流动性不足易炸板，仅极小仓做早盘强一致前排。",
    },
}

#: 自适应系数安全边界（遵循用户 2026-11-23「小仓试错、不绝对空仓」裁定，且普跌日绝不放大至满仓）
REGIME_MULT_MIN = 0.15
REGIME_MULT_MAX = 0.75
#: 因果实盘反馈衰减率（与 evolution.py 同口径，越新的实盘成交权重越高）
REGIME_TRADE_DECAY = 0.90


def _finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None          # 排除 NaN


def level_by_pct(x: Any, bounds: dict[str, float] | None) -> str | None:
    """按分位边界把标量映射到 L0..L5（与 v3.1 `_level_by_pct` 一致）。

    2026-11-23 加固：缺任一 P50..P95 键时返回 None，**不再用 `, 9` 兜底**。
    旧写法的 9 是 `limit_down_n` 的量纲，套到 `down_share`(0~1) 或 `dt_ratio`
    上会把该维度静默压到 L0/L1 —— 等于悄悄关掉这一维，却不报错。
    """
    fv = _finite(x)
    if fv is None or not bounds:
        return None
    need = ("P50", "P70", "P80", "P90", "P95")
    if any(bounds.get(k) is None for k in need):
        return None                      # 边界不完整 → 明确不可判定
    if fv > bounds["P95"]:
        return "L5"
    if fv > bounds["P90"]:
        return "L4"
    if fv > bounds["P80"]:
        return "L3"
    if fv > bounds["P70"]:
        return "L2"
    if fv > bounds["P50"]:
        return "L1"
    return "L0"


def dim_levels(rec: dict[str, Any],
               bounds: dict[str, Any] | None = None) -> dict[str, str | None]:
    """逐维度档位（v3.1 `dim_levels`）。

    2026-11-23 加固：若传入 `bounds` 为空/缺键，**退回 DEFAULT_BOUNDS 快照**，
    而不是让所有维度变 None —— 后者会让 `is_ice()` 静默返回 False，
    冰点闸门彻底失效却没有任何报错（比报错更危险）。
    """
    b = bounds if bounds else DEFAULT_BOUNDS
    # 缺任一维边界 → 整体退回快照，保证四维都能判定
    if not all(isinstance(b.get(k), dict) and b.get(k) for k in DIM_KEYS):
        b = DEFAULT_BOUNDS
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
    """**唯一冰点判据**：v3.1 三维确认 + 2026-11-25 普跌旁路。

        is_ice = (S>=L4 且 ≥2 维>=L3) 或 跌停>=40 或 **下跌占比>=80%**

    普跌旁路（用户 2026-11-25 裁定）：11-25 全市场 85% 下跌但跌停仅 11 家，
    原判据漏判为「非冰点」。普跌同样要降仓，不能只看恐慌跌停。
    """
    d = dims if dims is not None else dim_levels(rec)
    s = severity(d)
    n_ge3 = sum(1 for v in d.values()
                if v in LEVEL_ORDER and LEVEL_ORDER.index(v) >= 3)
    by_multi = bool(s and LEVEL_ORDER.index(s) >= 4 and n_ge3 >= 2)
    by_abs = (_finite(rec.get("limit_down_n")) or 0) >= ICE_LIMIT_DOWN_ABS
    by_breadth = (_finite(rec.get("down_share")) or 0) >= ICE_DOWN_SHARE_ABS
    return by_multi or by_abs or by_breadth


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

def _flag_frame(panel: pl.DataFrame) -> pl.DataFrame:
    """逐行涨跌/涨跌停/连板标记（日线口径，供收盘与盘中两条路径共用）。"""
    # 涨跌幅 + 涨跌停价幅度（按代码板块）；若有成交额列一并带上供全市场量能统计
    sel_cols = ["ts_code", "trade_date", "close"]
    if "amount" in panel.columns:
        sel_cols.append("amount")
    df = (panel.select(sel_cols)
          .sort(["ts_code", "trade_date"])
          .with_columns(pl.col("close").shift(1).over("ts_code").alias("prev_close")))

    codes = df["ts_code"].unique().to_list()
    lim_map = {str(c): float(feature_engine.limit_pct(str(c))) * 100.0
               for c in codes}
    df = df.with_columns(
        pl.col("ts_code").cast(pl.Utf8)
        .replace_strict(lim_map, return_dtype=pl.Float64).alias("lim_pct")
    ).with_columns(
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
    return df.with_columns(
        ((pl.col("prev_streak") >= 3) & (~pl.col("is_zt"))).alias("hi_broken"))


def _metrics_frame(panel: pl.DataFrame) -> pl.DataFrame:
    """一次性向量化算全历史逐日四维指标 + 全市场量能比（避免 O(天×股) 重复扫描）。

    Returns: DataFrame[day(str), down_share, limit_down_n, limit_up_n,
             dt_ratio, broken_hi_n, up, down, total_amount, vol_ratio_5d]，按日期升序。
    """
    df = _flag_frame(panel)
    has_amt = "amount" in df.columns

    # 按日聚合
    agg_exprs = [
        pl.col("is_up").sum().alias("up"),
        pl.col("is_down").sum().alias("down"),
        pl.col("is_zt").sum().alias("limit_up_n"),
        pl.col("is_dt").sum().alias("limit_down_n"),
        pl.col("hi_broken").sum().alias("broken_hi_n"),
    ]
    if has_amt:
        agg_exprs.append(pl.col("amount").cast(pl.Float64).sum().alias("total_amount"))
    else:
        agg_exprs.append(pl.lit(None, dtype=pl.Float64).alias("total_amount"))

    agg = (df.filter(pl.col("pct").is_not_null())
           .group_by("trade_date")
           .agg(agg_exprs)
           .sort("trade_date"))

    tot = pl.col("up") + pl.col("down")
    ma5_prev = pl.col("total_amount").shift(1).rolling_mean(window_size=5, min_samples=3)
    agg = agg.with_columns([
        pl.when(tot > 0).then(pl.col("down") / tot).otherwise(None)
        .round(4).alias("down_share"),
        pl.when(pl.col("limit_up_n") > 0)
        .then(pl.col("limit_down_n") / pl.col("limit_up_n")).otherwise(None)
        .round(4).alias("dt_ratio"),
        pl.when(ma5_prev.is_not_null() & (ma5_prev > 0) & pl.col("total_amount").is_not_null() & (pl.col("total_amount") > 0))
        .then(pl.col("total_amount") / ma5_prev).otherwise(None)
        .round(4).alias("vol_ratio_5d"),
    ])
    return agg.with_columns(pl.col("trade_date").cast(pl.Utf8).alias("day")).drop("trade_date")


_METRICS_CACHE: tuple[tuple[Any, ...], pl.DataFrame, pl.DataFrame] | None = None
#: 第三个元素持有 panel 引用 —— 防止 panel 被 GC 后新对象复用同一 id，
#: 导致缓存误命中（2026-11-23 审计实测的确定性 bug）。
_METRICS_LOCK = threading.Lock()


def _panel_fingerprint(p: pl.DataFrame) -> tuple[Any, ...]:
    """panel 内容指纹（比起 id() 可靠）。

    用 (高度, 列名, 首末日, 末行收盘和) 组合：这四项任一变化都会改变指纹，
    而它们都由真实落盘数据决定，构造开销远小于重算全表。
    """
    try:
        first = str(p["trade_date"].min())
        last = str(p["trade_date"].max())
        tail_sum = float(p["close"].tail(1000).sum() or 0.0)
    except Exception:                    # noqa: BLE001 - 指纹失败则退化为弱键
        first = last = ""
        tail_sum = 0.0
    return (p.height, tuple(p.columns), first, last, round(tail_sum, 4))


def metrics_all(panel: pl.DataFrame | None = None) -> pl.DataFrame:
    """全历史逐日四维指标（按 panel 内容指纹缓存，避免重复计算）。"""
    global _METRICS_CACHE
    p = panel if panel is not None else feeds.build_panel()
    key = _panel_fingerprint(p)
    with _METRICS_LOCK:
        if _METRICS_CACHE is not None and _METRICS_CACHE[0] == key:
            return _METRICS_CACHE[1]
    m = _metrics_frame(p)
    with _METRICS_LOCK:
        _METRICS_CACHE = (key, m, p)
    return m


def _norm(d: Any) -> str:
    s = str(d).replace("-", "")[:10]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s[:10]


def day_metrics(panel: pl.DataFrame, day: str) -> dict[str, Any] | None:
    """某日的四维原始指标 + 量能比（由向量化结果查表，全部来自真实日线）。"""
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
        "total_amount": _finite(r.get("total_amount")),
        "vol_ratio_5d": _finite(r.get("vol_ratio_5d")),
    }


def history(panel: pl.DataFrame, *, days: int = 250) -> list[dict[str, Any]]:
    """最近 N 个交易日的四维指标（用于现算分位边界）。"""
    m = metrics_all(panel).tail(days)
    return [{**r, "day": r["day"]} for r in m.to_dicts()]


# ---------------------------------------------------------------------------
# 盘中读数（2026-11-24 用户裁定：「这是股票，不是游戏」）
# ---------------------------------------------------------------------------
# 收盘口径只在 15:05 后成立；盘中面板里没有当日行，页面只能显示「面板无」，
# 用户看到的是「没数据」，实际是「还没收盘」。这里用实时行情算同一套四维指标，
# 让盘中也有读数，并显式标注 intraday=True / as_of（不与收盘定格混淆）。

#: 盘中读数只回看最近 N 个交易日算连板状态（连板不会超过这个长度）。
LIVE_LOOKBACK = 60


def _slice_recent(panel: pl.DataFrame, n: int = LIVE_LOOKBACK) -> pl.DataFrame:
    dates = panel["trade_date"].unique().sort().to_list()
    if not dates:
        return panel
    cut = dates[-n] if len(dates) > n else dates[0]
    return panel.filter(pl.col("trade_date") >= cut)


def prev_day_state(panel: pl.DataFrame | None = None, *,
                   lookback: int = LIVE_LOOKBACK) -> pl.DataFrame:
    """上一交易日每只股票的 (prev_streak, prev_is_zt)。

    只回看最近 `lookback` 个交易日算连板 —— 连板状态只在近端有意义，
    全表扫描 370 万行会让盘中每 60 秒的重算变成秒级卡顿。

    口径：以面板最后一个交易日为「昨天」，取该日的 `streak`（截至当日连续涨停数）
    与 `is_zt`（当日是否涨停）。**不能用 `_flag_frame` 里的 `prev_streak`** ——
    那一列是相对当日的前一日连板数，直接取会整体错位一天（盘中会把前天的连板
    当成昨天的，高标破板数因此少算）。
    """
    p = panel if panel is not None else feeds.build_panel()
    if p is None or p.height == 0:
        return pl.DataFrame(schema={"ts_code": pl.Utf8, "prev_streak": pl.Int32,
                                    "prev_is_zt": pl.Boolean})
    fl = _flag_frame(_slice_recent(p, lookback))
    last = fl["trade_date"].max()
    snap = fl.filter(pl.col("trade_date") == last)
    out = snap.select([
        pl.col("ts_code").cast(pl.Utf8),
        pl.col("streak").fill_null(0).cast(pl.Int32).alias("prev_streak"),
        pl.col("is_zt").alias("prev_is_zt"),
    ])
    return out.unique(subset=["ts_code"], keep="last")


def _quote_stamp(q: pl.DataFrame) -> str:
    """行情自带数据时点：20261124093625 -> 2026-11-24T09:36:25。"""
    try:
        raw = str(q["quote_time"].max() or "")
    except Exception:  # noqa: BLE001
        raw = ""
    if len(raw) < 14 or not raw.isdigit():
        return ""
    return (f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T"
            f"{raw[8:10]}:{raw[10:12]}:{raw[12:14]}")


def _elapsed_trading_frac(stamp: str) -> float | None:
    """把 'YYYY-MM-DDTHH:MM:SS' 折算为 A 股 240 分钟交易进度（用于盘中成交额等比换算）。"""
    if not stamp or "T" not in stamp:
        return None
    try:
        hm = stamp.split("T", 1)[1][:5]
        hh, mm = int(hm[:2]), int(hm[3:5])
        mins = hh * 60 + mm
    except (ValueError, IndexError):
        return None
    # 上午 09:30(570)..11:30(690) = 120min；下午 13:00(780)..15:00(900) = 120min
    if mins < 585:          # 开盘前 15 分钟成交畸高，不做线性外推
        return None
    if mins <= 690:
        elapsed = mins - 570
    elif mins < 780:
        elapsed = 120
    elif mins <= 900:
        elapsed = 120 + (mins - 780)
    else:
        elapsed = 240
    return max(0.05, min(1.0, elapsed / 240.0))


def _live_vol_ratio(panel: pl.DataFrame, q: pl.DataFrame, stamp: str) -> tuple[float | None, float | None]:
    """用盘中实时成交额按交易进度折算全天量能比（缺 amount 列或刚开盘返回 None）。"""
    if "amount" not in q.columns:
        return None, None
    frac = _elapsed_trading_frac(stamp)
    if frac is None:
        return None, None
    try:
        amt_now = float(q["amount"].cast(pl.Float64).sum() or 0.0)
    except Exception:  # noqa: BLE001
        return None, None
    if not (amt_now > 0):
        return None, None
    # 对比最近 5 个日线交易日的全市场成交额均值（自动对齐量纲：若日线为千元 vs 行情为元则跳过防错）
    try:
        m = metrics_all(panel).tail(5)
        hist_amts = [float(x) for x in m["total_amount"].to_list() if x is not None and float(x) > 0]
    except Exception:  # noqa: BLE001
        hist_amts = []
    if len(hist_amts) < 3:
        return round(amt_now, 2), None
    ma5 = sum(hist_amts) / len(hist_amts)
    proj = amt_now / frac
    ratio = proj / ma5
    # 量纲护栏：panel 与 quotes 若单位不同（如千元 vs 元差 1000 倍），ratio 会极端离谱 → 置 None 不误判
    if ratio < 0.05 or ratio > 10.0:
        return round(amt_now, 2), None
    return round(amt_now, 2), round(ratio, 4)


def live_metrics(day: str, *, panel: pl.DataFrame | None = None,
                 quotes: pl.DataFrame | None = None) -> dict[str, Any] | None:
    """用实时行情算「今天」的四维指标（收盘口径的盘中代理）。

    口径与历史一致：涨停/跌停按代码板块涨跌幅上限 ±0.3pp 容差，
    高标破板 = 昨日连板≥3 且当前未涨停。仅数据源由收盘价换成最新价。
    """
    d = _norm(day)
    day8 = d.replace("-", "")
    p = panel if panel is not None else feeds.build_panel()
    if p is None or p.height == 0:
        return None
    # 只有一个交易日 → 没有「昨天」，连板与高标破板都无从谈起。
    # 返回残缺读数比不返回更危险（会被界面显示成「非冰点」）。
    if p["trade_date"].n_unique() < 2:
        return None
    q = quotes if quotes is not None else feeds.all_quotes()
    if q is None or q.height == 0:
        return None
    stamp = _quote_stamp(q)
    # 行情不是当天的（周末/节假日/数据源滞后）→ 不拿旧价冒充今天
    if not stamp.startswith(d) and not str(q["quote_time"].max() or "").startswith(day8):
        return None

    prev = prev_day_state(p)
    if prev.height == 0:
        return None
    if "pct" not in q.columns:
        return None
    qq = (q.select([pl.col("ts_code").cast(pl.Utf8), pl.col("pct")])
          .filter(pl.col("pct").is_not_null()))
    if qq.height == 0:
        return None
    codes = qq["ts_code"].unique().to_list()
    lim_map = {str(c): float(feature_engine.limit_pct(str(c))) * 100.0
               for c in codes}
    qq = qq.with_columns(
        pl.col("ts_code").replace_strict(lim_map, return_dtype=pl.Float64)
        .alias("lim_pct")
    ).with_columns([
        (pl.col("pct") >= pl.col("lim_pct") - 0.3).alias("is_zt"),
        (pl.col("pct") <= -(pl.col("lim_pct") - 0.3)).alias("is_dt"),
        (pl.col("pct") > 0).alias("is_up"),
        (pl.col("pct") < 0).alias("is_down"),
    ])
    j = qq.join(prev, on="ts_code", how="left")
    up = int(j["is_up"].sum())
    down = int(j["is_down"].sum())
    limit_up_n = int(j["is_zt"].sum())
    limit_down_n = int(j["is_dt"].sum())
    broken_hi_n = int((j["prev_streak"].fill_null(0).ge(3) & ~j["is_zt"]).sum())
    total = up + down
    total_amt, vol_ratio = _live_vol_ratio(p, q, stamp)
    return {
        "day": d,
        "down_share": round(down / total, 4) if total else None,
        "limit_down_n": limit_down_n,
        "limit_up_n": limit_up_n,
        "dt_ratio": (round(limit_down_n / limit_up_n, 4)
                     if limit_up_n else None),
        "broken_hi_n": broken_hi_n,
        "up": up,
        "down": down,
        "total_amount": total_amt,
        "vol_ratio_5d": vol_ratio,
        "intraday": True,
        "as_of": stamp,
        "basis": "实时行情（盘中未收盘）",
    }


# ---------------------------------------------------------------------------
# 自适应普跌细分引擎（2026-11-26：不写死固定阈值、随近 250 日滚动分布与实盘因果演化）
# ---------------------------------------------------------------------------

def _pct_rank(val: float | None, sorted_arr: list[float]) -> float | None:
    """值在滚动历史升序数组中的连续分位秩 ∈ [0, 1]（无固定阈值）。"""
    if val is None or not sorted_arr:
        return None
    # bisect_right: 历史样本中 <= val 的比例
    import bisect
    pos = bisect.bisect_right(sorted_arr, val)
    return round(pos / len(sorted_arr), 4)


def _quantile(sorted_arr: list[float], p: float) -> float | None:
    if not sorted_arr:
        return None
    idx = min(max(0, int(p * len(sorted_arr))), len(sorted_arr) - 1)
    return round(sorted_arr[idx], 4)


def _approx_pct_from_bounds(val: float | None, bd: dict[str, float] | None) -> float | None:
    """历史样本不足 30 天时，用分位快照插值估计连续分位秩。"""
    if val is None or not bd:
        return None
    pts = [
        (0.0, float(bd.get("min", 0.0))),
        (0.50, float(bd["P50"])),
        (0.70, float(bd["P70"])),
        (0.80, float(bd["P80"])),
        (0.90, float(bd["P90"])),
        (0.95, float(bd["P95"])),
    ]
    if val <= pts[0][1]:
        return 0.05
    for i in range(1, len(pts)):
        p0, v0 = pts[i - 1]
        p1, v1 = pts[i]
        if val <= v1:
            if v1 <= v0:
                return p1
            return round(p0 + (p1 - p0) * (val - v0) / (v1 - v0), 4)
    return 0.98


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _score_regimes(
    *,
    p_down: float,
    p_ld: float,
    p_lu: float,
    p_brk: float,
    p_dt: float,
    p_vol: float | None,
    emotion: dict[str, Any] | None = None,
    overseas: dict[str, Any] | None = None,
) -> dict[str, float]:
    """用连续滚动分位秩 + 外盘/个股高标信号对 4 类普跌做软打分（0..1，无脆性断崖）。"""
    # 个股高标情绪压力 / 承接力
    emo_avail = bool(emotion and emotion.get("available"))
    emo_stress = None
    emo_ok = None
    if emo_avail:
        avg_ret = _finite(emotion.get("avg_ret"))
        ld_sh = _finite(emotion.get("limit_down_share")) or 0.0
        blocked = bool(emotion.get("blocked"))
        if blocked or (avg_ret is not None and avg_ret <= -3.0) or ld_sh >= 0.35:
            emo_stress = 0.95
            emo_ok = 0.10
        elif avg_ret is not None and avg_ret < -1.0:
            emo_stress = 0.55
            emo_ok = 0.45
        elif avg_ret is not None and avg_ret >= 0.5:
            emo_stress = 0.05
            emo_ok = 0.95
        else:
            emo_stress = 0.25
            emo_ok = 0.70

    # 1) 外盘冲击型普跌（仅在外盘明确风险偏好转弱时激活）
    ovs_score_val = None
    if overseas and overseas.get("available"):
        ovs_score_val = _finite((overseas.get("sentiment") or {}).get("score"))
    ext_score = 0.0
    if ovs_score_val is not None and ovs_score_val < -15.0:
        ovs_stress = _clamp((-ovs_score_val - 15.0) / 55.0, 0.0, 1.0)
        dom_resilience = 0.50 * (1.0 - p_brk) + 0.50 * p_lu
        if emo_ok is not None:
            dom_resilience = 0.65 * dom_resilience + 0.35 * emo_ok
        ext_score = _clamp(0.68 * ovs_stress + 0.32 * dom_resilience, 0.0, 1.0)

    # 2) 情绪退潮·核按钮型普跌
    panic_core = 0.42 * p_ld + 0.33 * p_dt + 0.25 * p_brk
    panic_score = (0.72 * panic_core + 0.28 * emo_stress) if emo_stress is not None else panic_core
    panic_score = _clamp(panic_score, 0.0, 1.0)

    # 3) 抱团虹吸·分化型普跌（面普跌，但无恐慌跌停且连板/高标仍有赚钱效应）
    siphon_core = (
        0.35 * (1.0 - p_ld)
        + 0.25 * (1.0 - p_dt)
        + 0.25 * p_lu
        + 0.15 * (1.0 - p_brk)
    )
    siphon_score = (0.75 * siphon_core + 0.25 * emo_ok) if emo_ok is not None else siphon_core
    siphon_score = _clamp(siphon_score * min(1.0, p_down / 0.80 if p_down > 0 else 0.0), 0.0, 1.0)

    # 4) 缩量失血·阴跌型普跌（量能处于滚动低分位 + 涨停稀少 + 非剧烈跌停潮）
    if p_vol is not None:
        drain_score = _clamp(
            0.50 * (1.0 - p_vol) + 0.30 * (1.0 - p_lu) + 0.20 * (1.0 - p_ld),
            0.0, 1.0,
        )
    else:
        drain_score = _clamp(
            0.75 * (0.55 * (1.0 - p_lu) + 0.45 * (1.0 - p_ld)),
            0.0, 1.0,
        )

    return {
        "external_shock": round(ext_score, 3),
        "panic_collapse": round(panic_score, 3),
        "narrow_siphon": round(siphon_score, 3),
        "volume_drain": round(drain_score, 3),
    }


def classify_decline_regime(
    rec: dict[str, Any],
    *,
    day: str | None = None,
    is_ice_flag: bool = False,
    hist_rows: list[dict[str, Any]] | None = None,
    bounds: dict[str, Any] | None = None,
    emotion: dict[str, Any] | None = None,
    overseas: dict[str, Any] | None = None,
    causal_trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """自适应普跌细分：滚动 250 日分位标尺 + 近 250 日 T+1 实证修复率 + 实盘因果反馈。

    为什么不写死固定阈值：
      A 股牛熊/成交额量级/涨跌停密度会随周期漂移（备份验证固定阈值 OOS 0/17）。
      因此所有判定均采用「相对近 250 个交易日的滚动分位秩」，且仓位系数由
      「冷启动先验 + 近 250 日同类普跌次日修复率修正 + 严格因果已结算实盘盈亏修正」
      三层合成，随市场每天自动更新。
    """
    d_norm = _norm(day or rec.get("day") or "")
    # 严格因果：只取目标日之前的滚动 250 个交易日作分布与次日修复统计
    rows_before = [
        r for r in (hist_rows or [])
        if _norm(r.get("day") or "") and (not d_norm or _norm(r.get("day")) < d_norm)
    ][-250:]
    use_rolling = len(rows_before) >= 30

    def _col_sorted(col: str) -> list[float]:
        arr = [f for f in (_finite(r.get(col)) for r in rows_before) if f is not None]
        arr.sort()
        return arr

    arr_down = _col_sorted("down_share")
    arr_ld = _col_sorted("limit_down_n")
    arr_lu = _col_sorted("limit_up_n")
    arr_brk = _col_sorted("broken_hi_n")
    arr_dt = _col_sorted("dt_ratio")
    arr_vol = _col_sorted("vol_ratio_5d")

    b_ref = bounds or DEFAULT_BOUNDS
    v_down = _finite(rec.get("down_share"))
    v_ld = _finite(rec.get("limit_down_n"))
    v_lu = _finite(rec.get("limit_up_n"))
    v_brk = _finite(rec.get("broken_hi_n"))
    v_dt = _finite(rec.get("dt_ratio"))
    v_vol = _finite(rec.get("vol_ratio_5d"))

    def _pick_pct(val: float | None, arr: list[float], col_key: str) -> float:
        r = (_pct_rank(val, arr) if use_rolling and arr
             else _approx_pct_from_bounds(val, b_ref.get(col_key)))
        return 0.5 if r is None else r

    p_down = _pick_pct(v_down, arr_down, "down_share")
    p_ld = _pick_pct(v_ld, arr_ld, "limit_down_n")
    p_dt = _pick_pct(v_dt, arr_dt, "dt_ratio")
    p_brk = _pick_pct(v_brk, arr_brk, "broken_hi_n")
    if use_rolling and arr_lu and v_lu is not None:
        _plu = _pct_rank(v_lu, arr_lu)
        p_lu = 0.5 if _plu is None else _plu
    elif v_lu is not None:
        # 无历史样本时用温和经验分布兜底（30..100 家线性映射到 0.2..0.85）
        p_lu = round(_clamp((v_lu - 15.0) / 100.0, 0.05, 0.95), 4)
    else:
        p_lu = 0.5
    p_vol = _pct_rank(v_vol, arr_vol) if (len(arr_vol) >= 20 and v_vol is not None) else (
        round(_clamp((v_vol - 0.65) / 0.70, 0.05, 0.95), 4) if v_vol is not None else None
    )

    adaptive_thresholds = {
        "window_days": len(rows_before),
        "basis": "rolling_250d" if use_rolling else "snapshot_fallback",
        "down_P80": _quantile(arr_down, 0.80) if arr_down else (b_ref.get("down_share") or {}).get("P80"),
        "ld_P80": _quantile(arr_ld, 0.80) if arr_ld else (b_ref.get("limit_down_n") or {}).get("P80"),
        "lu_P50": _quantile(arr_lu, 0.50) if arr_lu else None,
        "brk_P75": _quantile(arr_brk, 0.75) if arr_brk else (b_ref.get("broken_hi_n") or {}).get("P80"),
        "vol_P25": _quantile(arr_vol, 0.25) if len(arr_vol) >= 20 else None,
    }
    percentiles = {
        "down_share": round(p_down, 3),
        "limit_down_n": round(p_ld, 3),
        "limit_up_n": round(p_lu, 3),
        "broken_hi_n": round(p_brk, 3),
        "dt_ratio": round(p_dt, 3),
        "vol_ratio_5d": round(p_vol, 3) if p_vol is not None else None,
    }

    # 普跌激活：滚动 P80 以上下跌广度，或已触发冰点，或达到普跌旁路
    is_broad = bool(
        is_ice_flag
        or p_down >= 0.80
        or (v_down is not None and v_down >= ICE_DOWN_SHARE_ABS)
    )
    if not is_broad:
        return {
            "active": False,
            "regime": "normal",
            "regime_cn": "非普跌常态",
            "secondary_regime": None,
            "secondary_cn": None,
            "confidence": 1.0,
            "scores": {},
            "percentiles": percentiles,
            "adaptive_thresholds": adaptive_thresholds,
            "rolling_250d_stats": {"n": 0, "rebound_rate": None, "avg_next_up_share": None},
            "prior_mult": 1.0,
            "market_adj": 0.0,
            "trade_adj": 0.0,
            "adapted_mult": 1.0,
            "n_causal_trades": 0,
            "action_hint": "市场未进入滚动普跌区间，按常态规则与个股情绪闸门执行。",
            "adaptation_note": f"当前下跌广度分位 P{int(round(p_down * 100))} < P80，未触发普跌细分。",
        }

    scores = _score_regimes(
        p_down=p_down, p_ld=p_ld, p_lu=p_lu, p_brk=p_brk, p_dt=p_dt, p_vol=p_vol,
        emotion=emotion, overseas=overseas,
    )
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    primary, top_sc = ranked[0]
    sec_key, sec_sc = ranked[1] if len(ranked) > 1 else (None, 0.0)
    # 若次高分与主型接近且 >=0.50，如实标注复合特征（例如 11-25 兼具外盘冲击与尾盘恐慌）
    secondary = sec_key if (sec_key and sec_sc >= 0.50 and top_sc - sec_sc <= 0.25) else None
    sc_sum = sum(scores.values()) or 1.0
    confidence = round(_clamp(top_sc / sc_sum * 1.8, 0.25, 0.99), 2)

    # --- 近 250 日严格因果历史同类普跌 T+1 修复统计 + 历史日期 regime 映射 ---
    hist_regime_by_day: dict[str, str] = {}
    matched_next_up: list[float] = []
    matched_next_lu: list[float] = []
    stats_basis = "近250日同类普跌 T+1 实证"
    if use_rolling and len(rows_before) >= 35:
        for i in range(len(rows_before) - 1):
            ri = rows_before[i]
            rn = rows_before[i + 1]
            # 严格因果：rn 的日期也必须 < d_norm（rows_before 已过滤 < d_norm）
            di = _finite(ri.get("down_share"))
            _pdi = _pct_rank(di, arr_down)
            pdi = 0.0 if _pdi is None else _pdi
            if not (pdi >= 0.80 or (di is not None and di >= ICE_DOWN_SHARE_ABS)):
                continue
            _pldi = _pct_rank(_finite(ri.get("limit_down_n")), arr_ld)
            pldi = 0.5 if _pldi is None else _pldi
            _plui = _pct_rank(_finite(ri.get("limit_up_n")), arr_lu)
            plui = 0.5 if _plui is None else _plui
            _pbrki = _pct_rank(_finite(ri.get("broken_hi_n")), arr_brk)
            pbrki = 0.5 if _pbrki is None else _pbrki
            _pdti = _pct_rank(_finite(ri.get("dt_ratio")), arr_dt)
            pdti = 0.5 if _pdti is None else _pdti
            pvoli = (_pct_rank(_finite(ri.get("vol_ratio_5d")), arr_vol)
                     if (len(arr_vol) >= 20 and _finite(ri.get("vol_ratio_5d")) is not None) else None)
            sc_i = _score_regimes(
                p_down=pdi, p_ld=pldi, p_lu=plui, p_brk=pbrki, p_dt=pdti, p_vol=pvoli,
            )
            reg_i = max(sc_i.items(), key=lambda kv: kv[1])[0]
            hist_regime_by_day[_norm(ri.get("day") or "")] = reg_i

            # 统计匹配：外盘历史未全量留档，对 external_shock 用「高标尚抗跌的普跌日」作实证基线
            is_match = (reg_i == primary)
            if primary == "external_shock":
                is_match = (pbrki <= 0.55)
                stats_basis = "近250日高标抗跌型普跌 T+1 实证（历史无全量外盘留档）"
            if is_match:
                nxt_down = _finite(rn.get("down_share"))
                if nxt_down is not None:
                    matched_next_up.append(1.0 - nxt_down)
                nxt_lu = _finite(rn.get("limit_up_n"))
                if nxt_lu is not None:
                    matched_next_lu.append(nxt_lu)

    n_hist = len(matched_next_up)
    rebound_rate = (
        round(sum(1 for u in matched_next_up if u >= 0.50) / n_hist, 4)
        if n_hist else None
    )
    avg_next_up = round(sum(matched_next_up) / n_hist, 4) if n_hist else None
    avg_next_lu = round(sum(matched_next_lu) / len(matched_next_lu), 1) if matched_next_lu else None

    # --- 动态演化仓位系数：先验 + 滚动市场修复率修正 + 实盘因果结算盈亏修正 ---
    meta = REGIME_META[primary]
    prior_mult = float(meta["prior_mult"])

    market_adj = 0.0
    if n_hist >= 5 and rebound_rate is not None:
        market_adj = round(_clamp((rebound_rate - 0.50) * 0.30, -0.10, 0.10), 4)

    # 严格因果实盘成交反馈：只看 exit_date < d_norm 且 entry 属于同一普跌类型的已结算交易
    causal_rets: list[tuple[str, float]] = []
    for t in (causal_trades or []):
        if not isinstance(t, dict) or t.get("invalid_historical_bug"):
            continue
        exit_d = _norm(t.get("exit_date") or "")
        entry_d = _norm(t.get("entry_date") or "")
        if not exit_d or (d_norm and exit_d >= d_norm):
            continue
        ret = _finite(t.get("net_return"))
        if ret is None:
            ret = _finite(t.get("r_net"))
        if ret is None:
            continue
        t_reg = t.get("decline_regime") or hist_regime_by_day.get(entry_d)
        if t_reg == primary:
            causal_rets.append((exit_d, ret))

    causal_rets.sort(key=lambda x: x[0])
    trade_adj = 0.0
    causal_win_rate = None
    causal_mean_ret = None
    if causal_rets:
        # 指数衰减加权：越近的实盘成交权重越大（与 evolution.py 自然挥发一致）
        n_ct = len(causal_rets)
        weights = [REGIME_TRADE_DECAY ** (n_ct - 1 - idx) for idx in range(n_ct)]
        w_sum = sum(weights) or 1.0
        w_mean = sum(r * w for (_, r), w in zip(causal_rets, weights)) / w_sum
        ramp = min(1.0, n_ct / 5.0)
        trade_adj = round(_clamp(w_mean * 1.5 * ramp, -0.15, 0.20), 4)
        causal_win_rate = round(sum(1 for _, r in causal_rets if r > 0) / n_ct, 4)
        causal_mean_ret = round(w_mean, 4)

    adapted_mult = round(
        _clamp(prior_mult + market_adj + trade_adj, REGIME_MULT_MIN, REGIME_MULT_MAX),
        3,
    )

    parts = [f"先验 {prior_mult:.2f}"]
    if n_hist >= 5 and rebound_rate is not None:
        parts.append(f"近250日次日修复率 {rebound_rate:.0%}({n_hist}次) {market_adj:+.2f}")
    elif n_hist > 0:
        parts.append(f"近250日样本 {n_hist} 次(<5暂不偏移)")
    if causal_rets:
        parts.append(f"实盘同类 {len(causal_rets)} 笔(加权均净 {causal_mean_ret:+.1%}) {trade_adj:+.2f}")
    else:
        parts.append("实盘同类 0 笔(待积累)")
    parts.append(f"→ 动态系数 ×{adapted_mult:.2f}")

    return {
        "active": True,
        "regime": primary,
        "regime_cn": meta["cn"],
        "secondary_regime": secondary,
        "secondary_cn": REGIME_META[secondary]["cn"] if secondary else None,
        "confidence": confidence,
        "scores": scores,
        "percentiles": percentiles,
        "adaptive_thresholds": adaptive_thresholds,
        "rolling_250d_stats": {
            "n": n_hist,
            "rebound_rate": rebound_rate,
            "avg_next_up_share": avg_next_up,
            "avg_next_lu": avg_next_lu,
            "window_days": len(rows_before),
            "basis": stats_basis,
        },
        "prior_mult": prior_mult,
        "market_adj": market_adj,
        "trade_adj": trade_adj,
        "adapted_mult": adapted_mult,
        "n_causal_trades": len(causal_rets),
        "causal_win_rate": causal_win_rate,
        "causal_mean_ret": causal_mean_ret,
        "action_hint": meta["hint"],
        "adaptation_note": " · ".join(parts),
    }


def assess(day: str, *, panel: pl.DataFrame | None = None,
           bounds: dict[str, Any] | None = None,
           roll_bounds: bool = True,
           persist: bool = False,
           intraday: bool = False,
           quotes: pl.DataFrame | None = None,
           emotion: dict[str, Any] | None = None,
           overseas: dict[str, Any] | None = None,
           causal_trades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """评估某日冰点状态 + 自适应普跌细分 + 仓位系数。

    Args:
        day: 'YYYYMMDD' 或 'YYYY-MM-DD'
        panel: 可注入（便于测试）；None 时读 feeds.build_panel()
        bounds: 可注入分位边界；None 时按 roll_bounds 决定
        roll_bounds: True = 用最近 250 日现算分位（跟随市场）；
                     False = 用 DEFAULT_BOUNDS 快照（便于复现历史）
        persist: True 时把本次判定写入样本库（用户裁定 B：命中即新增样本）。
                 默认 False —— 纯查询不产生副作用，避免只读调用污染样本库。
        intraday: True 时，若当日收盘 bar 还没进面板（盘中），改用实时行情
                  算同一套四维指标，返回 `intraday=True` + `as_of`。
                  盘中读数**不写样本库** —— 未收盘的数据不能当历史样本。
        quotes: 可注入实时行情快照（便于测试）；None 时读 feeds.all_quotes()
        emotion: 可选，当日个股情绪闸门快照（用于普跌细分软打分）
        overseas: 可选，当日外盘情绪快照（用于识别外盘冲击型普跌）
        causal_trades: 可选，已结算实盘成交列表（严格只用 exit_date < day 的因果记录动态演化系数）
    """
    d = _norm(day)
    p = panel if panel is not None else feeds.build_panel()
    rec = day_metrics(p, d)
    intraday_used = False
    if rec is None and intraday:
        rec = live_metrics(d, panel=p, quotes=quotes)
        intraday_used = bool(rec)
    if rec is None:
        return {
            "available": False,
            "day": d,
            "reason": (f"面板无 {d} 或无前一交易日" if not intraday
                       else f"面板无 {d}，且实时行情不可用（非当日快照）"),
            "is_ice": False,
            "level": None,
            "size_factor": SIZE_FACTOR_DEFAULT,
        }

    # 取严格截至目标日的历史（多取 260 日供普跌细分严格因果筛选 < d 的 250 日）
    if p is not None and p.height > 0:
        m_le = metrics_all(p).filter(pl.col("day") <= d).tail(260)
        hist_all = [{**r, "day": r["day"]} for r in m_le.to_dicts()]
    else:
        hist_all = []
    hist_250 = hist_all[-250:]

    if bounds is None:
        # 逐维度兜底：compute_bounds 对样本不足的维度会跳过（返回缺键），
        # 若只在整个 dict 为空时才兜底，缺失维度会被静默排除出冰点判定
        # （2026-11-23 审计实测：down_share=0.95 完全没计入）。
        computed = compute_bounds(hist_250) if roll_bounds else {}
        if not computed:
            bounds = DEFAULT_BOUNDS
        else:
            bounds = {**DEFAULT_BOUNDS, **computed}
            for k in DIM_KEYS:               # 逐维度确保有边界
                if not isinstance(bounds.get(k), dict) or not bounds.get(k):
                    bounds[k] = DEFAULT_BOUNDS[k]

    dims = dim_levels(rec, bounds)
    s = severity(dims)
    ice = is_ice(rec, dims)
    factor = size_factor(s, ice)
    decline_regime = classify_decline_regime(
        rec,
        day=d,
        is_ice_flag=ice,
        hist_rows=hist_all,
        bounds=bounds,
        emotion=emotion,
        overseas=overseas,
        causal_trades=causal_trades,
    )
    reasons = []
    itype = None
    if ice:
        n_ge3 = sum(1 for v in dims.values()
                    if v in LEVEL_ORDER and LEVEL_ORDER.index(v) >= 3)
        by_multi = bool(s and LEVEL_ORDER.index(s) >= 4 and n_ge3 >= 2)
        by_abs = (_finite(rec.get("limit_down_n")) or 0) >= ICE_LIMIT_DOWN_ABS
        by_breadth = (_finite(rec.get("down_share")) or 0) >= ICE_DOWN_SHARE_ABS
        itype = ("both" if (by_multi and (by_abs or by_breadth))
                 else "multi_dim" if by_multi
                 else "abs_limit_down" if by_abs
                 else "broad_decline" if by_breadth else None)
        if by_multi:
            reasons.append(f"多维确认：S={s} 且 {n_ge3} 维≥L3")
        if by_abs:
            reasons.append(f"跌停 {rec['limit_down_n']} 家 ≥ {ICE_LIMIT_DOWN_ABS}")
        if by_breadth:
            reasons.append(f"普跌：下跌占比 {_finite(rec.get('down_share')):.1%} "
                           f"≥ {ICE_DOWN_SHARE_ABS:.0%}（2026-11-25 新增旁路）")

    # 用户裁定 B：命中即新增样本并参与校准（persist=True 时才写）
    sample_info: dict[str, Any] = {"persisted": False}
    if persist and not intraday_used:
        try:
            from backend import ice_samples
            # 原子临界区：load→record→save 全程持锁，避免并发覆盖/Windows replace 冲突
            with ice_samples.locked_update() as st:
                is_new = ice_samples.record_day(
                    st, day=d, metrics=rec, dims=dims, level=s, is_ice=ice,
                    size_factor=factor, itype=itype, source="auto",
                    decline_regime=decline_regime)
                auto_n = len(st.get("auto_samples") or {})
                hist_n = len(st.get("history") or {})
            sample_info = {
                "persisted": True,
                "new_sample": is_new,
                "auto_samples_n": auto_n,
                "history_n": hist_n,
            }
        except Exception as exc:  # noqa: BLE001 - 写库失败不阻塞判定
            sample_info = {"persisted": False,
                           "error": f"{type(exc).__name__}: {exc}"}

    return {
        "available": True,
        "day": d,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "intraday": intraday_used,
        "as_of": rec.get("as_of"),
        "metrics": rec,
        "dim_levels": dims,
        "level": s,
        "is_ice": ice,
        "ice_type": itype,
        "size_factor": factor,
        "decline_regime": decline_regime,
        "reasons": reasons,
        "verdict": ("冰点 → 小仓试错" if ice else "非冰点 → 正常仓位"),
        "bounds_source": ("rolling_250d" if roll_bounds else "snapshot"),
        "bounds": bounds,
        "sample": sample_info,
        "caveat": (("盘中读数（" + str(rec.get("as_of") or "实时行情")
                    + "），非收盘定格；收盘后以日线口径复核，且不计入冰点样本库。")
                   if intraday_used else
                   ("分位边界与普跌细分均随近 250 日市场分布及实盘因果盈亏滚动更新；"
                    "备份切分验证显示形态会漂移（OOS 种子命中 0/17），绝不把静态阈值当永久真理。")),
    }


if __name__ == "__main__":
    import json
    import sys
    from datetime import datetime as _dt

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    persist = "--persist" in sys.argv
    day = args[0] if args else _dt.now().strftime("%Y%m%d")
    r = assess(day, persist=persist)
    brief = {k: v for k, v in r.items() if k != "bounds"}
    print(json.dumps(brief, ensure_ascii=False, indent=2, default=str))
