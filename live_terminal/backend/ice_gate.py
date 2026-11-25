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
    # 涨跌幅 + 涨跌停价幅度（按代码板块）
    df = (panel.select(["ts_code", "trade_date", "close"])
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
    """一次性向量化算全历史逐日四维指标（避免 O(天×股) 重复扫描）。

    Returns: DataFrame[trade_date(str), down_share, limit_down_n, limit_up_n,
             dt_ratio, broken_hi_n, up, down]，按日期升序。
    """
    df = _flag_frame(panel)

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
        "intraday": True,
        "as_of": stamp,
        "basis": "实时行情（盘中未收盘）",
    }


def assess(day: str, *, panel: pl.DataFrame | None = None,
           bounds: dict[str, Any] | None = None,
           roll_bounds: bool = True,
           persist: bool = False,
           intraday: bool = False,
           quotes: pl.DataFrame | None = None) -> dict[str, Any]:
    """评估某日冰点状态 + 仓位系数。

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

    if bounds is None:
        # 逐维度兜底：compute_bounds 对样本不足的维度会跳过（返回缺键），
        # 若只在整个 dict 为空时才兜底，缺失维度会被静默排除出冰点判定
        # （2026-11-23 审计实测：down_share=0.95 完全没计入）。
        computed = compute_bounds(history(p, days=250)) if roll_bounds else {}
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
                    size_factor=factor, itype=itype, source="auto")
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
        "reasons": reasons,
        "verdict": ("冰点 → 小仓试错" if ice else "非冰点 → 正常仓位"),
        "bounds_source": ("rolling_250d" if roll_bounds else "snapshot"),
        "bounds": bounds,
        "sample": sample_info,
        "caveat": (("盘中读数（" + str(rec.get("as_of") or "实时行情")
                    + "），非收盘定格；收盘后以日线口径复核，且不计入冰点样本库。")
                   if intraday_used else
                   ("分位边界随市场滚动现算；备份切分验证显示冰点形态会漂移"
                    "（OOS 种子命中 0/17），不得把历史阈值当永久真理。")),
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
