"""宽口径高标 / 趋势龙头识别器 · 按用户 2026-11-12 定义落地。

用户原话（要点）
------------------------------------------------
「高标不一定是连板最高，也有可能是龙头，比如说他连续很多天都在涨，
  但是不一定都涨停了，也可能中间回踩了几天，又继续涨的那种」
举例：襄阳轴承、东方海洋、冰山冷热、光洋股份、慈文传媒、凡拓数创。
用户 2026-11-12 又补了五个**历史高标周期**：大众交通 2024-07-08~08-05、
常山北明 2024-08-15~11-05，以及乐山电力 2025-04-08~07-08、
华电辽能 2026-02-04~06-05、华电能源 2026-02-24~06-05（后三个为跨月多波趋势牛股），
见 `CASE_PERIODS` / `period_cases()`。

所以本模块**不把高标等同于 max_height**：

    连板高标 (streak)  streak >= 3              —— 传统「最高板」
    趋势龙头 (trend)    20 日窗口低点起涨幅 >= 50%、涨多跌少、
                        至少 1 个涨停、仍在高位（near >= 0.85），
                        且不是纯一字连板（非涨停上涨 >= 3 天 或 回踩 >= 2 天）
    高标 = 连板高标 ∪ 趋势龙头

「存亡」三态（这是用户要的「高标存亡」）
------------------------------------------------
    ALIVE    当前满足高标条件
    COOLING  20 日内曾 >= 3 天满足趋势龙头，当前不满足但 near >= 0.75
             → 中间回踩、还没死，可能再创新高（慈文传媒当前即此态）
    DEAD     曾满足，当前 near < 0.75 → 退潮/消亡
    BORN     当日首次进入 ALIVE

参数是**样本内标定**：阈值是为了把用户给的 6 只票按真实日线正确归类而设的，
不是优化出来的策略参数，不构成任何有效性证据。6 只票真实指标见 `examples()`。

数据：真实日线 panel（OHLCV + turnover + pct_chg），零合成。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from numpy.lib.stride_tricks import sliding_window_view

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import feature_engine, feeds  # noqa: E402

# 数据与代码分离：输出文件落在数据根，不在代码目录。
from backend import paths  # noqa: E402

OUT = paths.data_dir() / "leader_ladder.json"

WINDOW = 20
MIN_RUN = 0.50            # 20 日窗口低点起涨幅
MIN_UP_SHARE = 0.45       # 上涨天数占比
MIN_NEAR = 0.85           # 当前价 / 20 日最高
COOLING_NEAR = 0.75       # 回踩观察态下界
MIN_EVER = 3              # 20 日内曾满足强条件的天数
STREAK_HIGH = 3           # 连板高标门槛
MIN_NONZT_UP = 3          # 非涨停上涨天数（证明不是纯连板）
MIN_PULLBACK = 2          # 回踩天数（允许中间回踩）

EXAMPLES: dict[str, str] = {
    "000678": "襄阳轴承",
    "002086": "东方海洋",
    "000530": "冰山冷热",
    "002708": "光洋股份",
    "002343": "慈文传媒",
    "301313": "凡拓数创",
}

# 用户 2026-11-12 补充的**历史龙头周期**（事后标注的区间，不是实时信号）
# kind: 龙头周期（主升段连续）/ 趋势牛股周期（跨月多波、中间大回撤）
CASE_PERIODS: list[dict[str, Any]] = [
    {"ts_code": "600611", "name": "大众交通", "start": "2024-07-08", "end": "2024-08-05",
     "kind": "龙头周期"},
    {"ts_code": "000158", "name": "常山北明", "start": "2024-08-15", "end": "2024-11-05",
     "kind": "龙头周期"},
    {"ts_code": "600644", "name": "乐山电力", "start": "2025-04-08", "end": "2025-07-08",
     "kind": "趋势牛股周期"},
    {"ts_code": "600396", "name": "华电辽能", "start": "2026-02-04", "end": "2026-06-05",
     "kind": "趋势牛股周期"},
    {"ts_code": "600726", "name": "华电能源", "start": "2026-02-24", "end": "2026-06-05",
     "kind": "趋势牛股周期"},
    # 第三批：用户 2026-11-12 补充「市场选出来的龙头高标，具有意义，可参考连板梯队/行业/消息/日内/换手/量能」
    {"ts_code": "000062", "name": "深圳华强", "start": "2024-08-15", "end": "2024-10-31",
     "kind": "龙头周期"},
    {"ts_code": "000592", "name": "平潭发展", "start": "2025-10-17", "end": "2026-01-07",
     "kind": "龙头周期"},
    {"ts_code": "600127", "name": "金健米业", "start": "2026-08-06", "end": "2026-09-24",
     "kind": "龙头周期"},
    {"ts_code": "000636", "name": "风华高科", "start": "2026-04-08", "end": "2026-06-29",
     "kind": "龙头周期"},
    {"ts_code": "600693", "name": "东百集团", "start": "2025-11-04", "end": "2026-01-13",
     "kind": "龙头周期"},
    # 第四批：与东百集团/平潭发展同时期的合富中国；以及独立牛股长城军工
    {"ts_code": "603122", "name": "合富中国", "start": "2025-10-28", "end": "2025-12-19",
     "kind": "龙头周期", "same_period_with": ["东百集团", "平潭发展"]},
    {"ts_code": "601606", "name": "长城军工", "start": "2025-06-17", "end": "2025-09-03",
     "kind": "趋势牛股周期"},
]

KIND_CN = {"ALIVE": "存活高标", "COOLING": "回踩观察", "DEAD": "已退潮", "BORN": "新诞生"}


def config() -> dict[str, Any]:
    return {
        "window": WINDOW, "min_run": MIN_RUN, "min_up_share": MIN_UP_SHARE,
        "min_near": MIN_NEAR, "cooling_near": COOLING_NEAR, "min_ever": MIN_EVER,
        "streak_high": STREAK_HIGH, "min_nonzt_up": MIN_NONZT_UP,
        "min_pullback": MIN_PULLBACK,
        "calibration": "样本内标定：仅按最初 6 只举例票的真实日线归类；后续补充的 5 个历史周期未用于调参，"
                       "未做参数寻优，不构成策略有效性证据",
    }


def _zt_flags(code: str, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """兼容别名：涨停/连板判据唯一实现在 `feature_engine`。

    重构前这里与 `guitu_backtest._zt_flags` 各写一份循环（写法不同、结果相同），
    漏改一处就会让同一只票在「龟兔回放」和「高标存亡」里判出不同连板数。
    """
    return feature_engine.zt_flags(code, c)


def metrics_for(code: str, c: np.ndarray, pct: np.ndarray) -> dict[str, np.ndarray]:
    """单只股票的逐日宽口径高标指标（全部向量化，无未来函数）。

    窗口只用 t-WINDOW+1 .. t 的数据，不含 t 之后任何信息。
    """
    w = WINDOW
    n = len(c)
    zt, streak = _zt_flags(code, c)
    win = sliding_window_view(np.concatenate([np.full(w - 1, c[0]), c]), w)
    pwin = sliding_window_view(np.concatenate([np.zeros(w - 1), pct]), w)
    zwin = sliding_window_view(np.concatenate([np.zeros(w - 1), zt.astype(float)]), w)

    wmin = np.where(win.min(axis=1) > 0, win.min(axis=1), np.nan)
    wmax = np.where(win.max(axis=1) > 0, win.max(axis=1), np.nan)
    run = c / wmin - 1                       # 窗口低点起涨幅
    near = c / wmax                          # 距窗口高点的比例
    up_share = (pwin > 0).mean(axis=1)
    pullback = (pwin < 0).sum(axis=1)
    zt_days = zwin.sum(axis=1)
    nonzt_up = ((pwin > 0) & (zwin < 0.5)).sum(axis=1)

    trend = (
        (run >= MIN_RUN) & (up_share >= MIN_UP_SHARE) & (zt_days >= 1)
        & (near >= MIN_NEAR)
        & ((nonzt_up >= MIN_NONZT_UP) | (pullback >= MIN_PULLBACK))
    )
    # 「曾经是趋势龙头」：滚动 20 日内满足强条件的天数
    ever = sliding_window_view(
        np.concatenate([np.zeros(w - 1), trend.astype(float)]), w
    ).sum(axis=1)

    alive = trend | (streak >= STREAK_HIGH)
    cooling = (~alive) & (ever >= MIN_EVER) & (near >= COOLING_NEAR)
    dead = (~alive) & (ever >= MIN_EVER) & (near < COOLING_NEAR)

    return {
        "run": run, "near": near, "up_share": up_share, "pullback": pullback,
        "zt_days": zt_days, "nonzt_up": nonzt_up, "ever": ever,
        "trend": trend, "alive": alive, "cooling": cooling, "dead": dead,
        "zt": zt, "streak": streak,
    }


def _state(m: dict[str, np.ndarray], t: int, prev_alive: bool) -> str:
    if not bool(m["alive"][t]):
        if bool(m["cooling"][t]):
            return "COOLING"
        if bool(m["dead"][t]):
            return "DEAD"
        return ""
    return "" if prev_alive else "BORN"


def examples(panel: pl.DataFrame, names: dict[str, str]) -> list[dict[str, Any]]:
    """用真实日线验证用户给的 6 只票，逐项摊开指标，不编造。"""
    out: list[dict[str, Any]] = []
    for code, given in EXAMPLES.items():
        d = panel.filter(pl.col("ts_code") == code).sort("trade_date")
        if d.height < WINDOW + 5:
            out.append({"ts_code": code, "name": given, "available": False,
                        "reason": "日线不足 25 个交易日"})
            continue
        c = d["close"].to_numpy().astype(float)
        pct = d["pct_chg"].to_numpy().astype(float)
        m = metrics_for(code, c, pct)
        t = len(c) - 1
        recent = d.tail(WINDOW)
        trail = [{"date": str(x), "close": round(float(y), 2),
                  "pct": round(float(z), 2), "zt": bool(m["zt"][t - WINDOW + 1 + i])}
                 for i, (x, y, z) in enumerate(zip(
                     recent["trade_date"].to_list(), recent["close"].to_list(),
                     recent["pct_chg"].to_list()))]
        out.append({
            "ts_code": code, "name": names.get(code, given), "available": True,
            "date": str(d["trade_date"].to_list()[-1]),
            "close": round(float(c[t]), 2),
            "run20": round(float(m["run"][t]), 4),
            "near_high": round(float(m["near"][t]), 4),
            "up_share": round(float(m["up_share"][t]), 4),
            "pullback_days": int(m["pullback"][t]),
            "zt_days": int(m["zt_days"][t]),
            "non_zt_up_days": int(m["nonzt_up"][t]),
            "ever20_days": int(m["ever"][t]),
            "streak": int(m["streak"][t]),
            "trend_hit": bool(m["trend"][t]),
            "state": "ALIVE" if bool(m["alive"][t]) else _state(m, t, True),
            "is_streak_high": bool(m["streak"][t] >= STREAK_HIGH),
            "is_trend": bool(m["trend"][t]),
            "trail": trail,
            "readout": (
                f"20 日低点起 +{m['run'][t] * 100:.0f}%，涨停 {int(m['zt_days'][t])} 天、"
                f"非涨停上涨 {int(m['nonzt_up'][t])} 天、回踩 {int(m['pullback'][t])} 天，"
                f"当前连板 {int(m['streak'][t])} 板、距窗口高点 {m['near'][t] * 100:.0f}%"
            ),
        })
    return out


def period_cases(panel: pl.DataFrame, names: dict[str, str],
                 by_day: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """用户补充的历史龙头周期：逐日摊开识别状态，不改写阈值、不做事后调参。

    与 `examples()` 的区别：examples 只算**最新一天**（当前是不是高标）；
    历史案例必须按用户给的**区间**逐日回放，否则会把「2024 年的龙头」
    误判成「2026-11-11 不是高标」。
    """
    out: list[dict[str, Any]] = []
    for case in CASE_PERIODS:
        code, nm = case["ts_code"], case["name"]
        d = panel.filter(pl.col("ts_code") == code).sort("trade_date")
        if d.height < WINDOW + 5:
            out.append({**case, "available": False, "reason": "日线不足 25 个交易日"})
            continue
        c = d["close"].to_numpy().astype(float)
        pct = d["pct_chg"].to_numpy().astype(float)
        m = metrics_for(code, c, pct)
        dates = [str(x) for x in d["trade_date"].to_list()]
        idx = [i for i, x in enumerate(dates) if case["start"] <= x <= case["end"]]
        if not idx:
            out.append({**case, "available": False, "reason": "区间内无交易日（超出面板范围）"})
            continue

        i0, i1 = idx[0], idx[-1]
        states: list[str] = []
        series: list[dict[str, Any]] = []
        for i in idx:
            st = ("ALIVE" if m["alive"][i] else
                  "COOLING" if m["cooling"][i] else
                  "DEAD" if m["dead"][i] else "NOT_YET")
            states.append(st)
            iso = date.fromisoformat(dates[i]).isocalendar()
            series.append({
                "date": dates[i], "close": round(float(c[i]), 2),
                "pct": round(float(pct[i]), 2), "state": st,
                "run": round(float(m["run"][i]), 3),
                "near": round(float(m["near"][i]), 3),
                "streak": int(m["streak"][i]), "zt": bool(m["zt"][i]),
                "week": f"{iso.year}-W{iso.week:02d}",
            })
        alive_pos = [k for k, s in enumerate(states) if s == "ALIVE"]
        n_alive = len(alive_pos)
        # 主升段 = 首次到末次 ALIVE；gap 表示中间是否断档
        gap = 0
        main_leg: dict[str, Any] = {}
        if alive_pos:
            a, b = alive_pos[0], alive_pos[-1]
            gap = (b - a + 1) - n_alive
            seg = pct[idx[a]:idx[b] + 1]
            main_leg = {
                "start": series[a]["date"], "end": series[b]["date"],
                "sessions": b - a + 1, "alive_days": n_alive, "gaps": gap,
                "contiguous": gap == 0,
                "down_days": int((seg < 0).sum()),
                "worst_pct": round(float(seg.min()), 2),
                "zt_days": int(m["zt"][idx[a]:idx[b] + 1].sum()),
            }
        # 启动段 = 区间起点到首次 ALIVE 之前
        startup: dict[str, Any] = {}
        if alive_pos and alive_pos[0] > 0:
            a = alive_pos[0]
            startup = {
                "start": series[0]["date"], "end": series[a - 1]["date"],
                "sessions": a, "lag_sessions": a,
                "gain": round(float(c[idx[a - 1]] / c[idx[0]] - 1), 4),
                "zt_days": int(m["zt"][idx[0]:idx[a]].sum()),
                "run_at_end": round(float(m["run"][idx[a - 1]]), 3),
            }
        streak_only = [k for k, s in enumerate(series) if s["streak"] >= STREAK_HIGH]
        seg_pct = pct[i0:i1 + 1]
        seg_zt = m["zt"][i0:i1 + 1]

        # 区间内按 ISO 周聚合的全市场高标存亡（by_day 由 build 提供）
        weeks: list[dict[str, Any]] = []
        if by_day:
            by_week: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in series:
                if row["date"] in by_day:
                    by_week[row["week"]].append(by_day[row["date"]])
            for wk in sorted(by_week):
                rs = by_week[wk]
                weeks.append({
                    "week": wk, "sessions": len(rs),
                    "alive_median": round(float(np.median([r["n_alive"] for r in rs])), 1),
                    "trend_median": round(float(np.median([r["n_trend"] for r in rs])), 1),
                    "died_sum": int(sum(r["n_died"] for r in rs)),
                    "max_height": int(max(r["max_height"] for r in rs)),
                })

        gain = float(c[i1] / c[i0] - 1)
        out.append({
            **case,
            "available": True,
            "name": names.get(code, nm),
            "actual_start": dates[i0], "actual_end": dates[i1],
            "sessions": len(idx),
            "close_start": round(float(c[i0]), 2), "close_end": round(float(c[i1]), 2),
            "gain": round(gain, 4),
            "high": round(float(c[i0:i1 + 1].max()), 2),
            "low": round(float(c[i0:i1 + 1].min()), 2),
            "zt_days": int(seg_zt.sum()),
            "non_zt_up_days": int(((seg_pct > 0) & ~seg_zt).sum()),
            "down_days": int((seg_pct < 0).sum()),
            "max_streak": int(m["streak"][i0:i1 + 1].max()),
            "counts": {s: states.count(s) for s in ("ALIVE", "COOLING", "DEAD", "NOT_YET")},
            "coverage": round(n_alive / len(idx), 4),
            "first_alive": series[alive_pos[0]]["date"] if alive_pos else "",
            "recognized": n_alive > 0,
            "main_leg": main_leg,
            "startup": startup,
            "streak_only_days": len(streak_only),
            "streak_only_coverage": round(len(streak_only) / len(idx), 4),
            "series": series,
            "weeks": weeks,
            "readout": (
                f"{dates[i0]}~{dates[i1]} 共 {len(idx)} 个交易日，"
                f"{c[i0]:.2f}→{c[i1]:.2f}（{gain * 100:+.0f}%），"
                f"涨停 {int(seg_zt.sum())} 天、非涨停上涨 {int(((seg_pct > 0) & ~seg_zt).sum())} 天、"
                f"下跌 {int((seg_pct < 0).sum())} 天，最大连板 {int(m['streak'][i0:i1 + 1].max())} 板"
            ),
        })
    return out


def period_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [x for x in cases if x.get("available") and x.get("recognized")]
    return {
        "given": len(cases), "recognized": len(ok),
        "sessions": sum(x.get("sessions", 0) for x in cases if x.get("available")),
        "alive_days": sum(x["counts"]["ALIVE"] for x in cases if x.get("available")),
        "main_leg_days": sum(x["main_leg"].get("sessions", 0)
                             for x in cases if x.get("available") and x.get("main_leg")),
        "contiguous_cases": sum(1 for x in ok if x["main_leg"].get("contiguous")),
        "streak_only_days": sum(x.get("streak_only_days", 0) for x in cases if x.get("available")),
        "streak_only_cases_with_any": sum(1 for x in cases
                                          if x.get("available") and x.get("streak_only_days", 0) > 0),
        "max_streak_overall": max((x.get("max_streak", 0) for x in cases if x.get("available")),
                                  default=0),
        "by_kind": {
            k: {
                "given": sum(1 for x in cases if x.get("kind") == k),
                "sessions": sum(x.get("sessions", 0) for x in cases
                                if x.get("kind") == k and x.get("available")),
                "alive_days": sum(x["counts"]["ALIVE"] for x in cases
                                  if x.get("kind") == k and x.get("available")),
                "contiguous": sum(1 for x in ok if x.get("kind") == k
                                  and x["main_leg"].get("contiguous")),
                "total_gaps": sum(x["main_leg"].get("gaps", 0) for x in cases
                                  if x.get("kind") == k and x.get("available")
                                  and x.get("main_leg")),
            }
            for k in sorted({x.get("kind", "") for x in cases})
        },
        "note": "仅用「最高连板≥3」口径时，大众交通、风华高科这类低连板趋势龙头几乎完全漏检；"
                "宽口径在首批两个龙头周期的主升段连续命中。多数跨月趋势牛股中间有大回撤，"
                "主升段会断档，属形态真实反映。",
    }


def build(panel: pl.DataFrame | None = None,
          names: dict[str, str] | None = None) -> dict[str, Any]:
    """全市场逐日高标梯队 + 存亡。返回体含内存用 `flags`，落盘时会剔除。"""
    if panel is None:
        panel = feeds.build_panel()
    if names is None:
        names = dict(zip(feeds.stock_list()["ts_code"].to_list(),
                         feeds.stock_list()["name"].to_list()))

    # date -> code -> 指标；以及按日的集合
    flags: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    alive_by_day: dict[str, set[str]] = defaultdict(set)
    by_day: dict[str, dict[str, Any]] = {}

    parts = panel.partition_by("ts_code", as_dict=True)
    for k, df in parts.items():
        code = k[0] if isinstance(k, tuple) else k
        name = names.get(code, "")
        if not name or feature_engine.is_st(name):
            continue
        d = df.sort("trade_date")
        c = d["close"].to_numpy().astype(float)
        pct = d["pct_chg"].to_numpy().astype(float)
        if len(c) < WINDOW + 5:
            continue
        m = metrics_for(code, c, pct)
        dates = [str(x) for x in d["trade_date"].to_list()]
        active = np.flatnonzero(m["alive"] | m["cooling"] | m["dead"])
        for ti in active:
            t = int(ti)
            day = dates[t]
            alive = bool(m["alive"][t])
            prev_alive = bool(m["alive"][t - 1]) if t > 0 else False
            state = ("" if prev_alive else "BORN") if alive else _state(m, t, prev_alive)
            flags[day][code] = {
                "name": name, "alive": alive, "state": state or "ALIVE",
                "kind": ("连板高标" if m["streak"][t] >= STREAK_HIGH else "")
                        or ("趋势龙头" if m["trend"][t] else "回踩/退潮"),
                "run": round(float(m["run"][t]), 4),
                "near": round(float(m["near"][t]), 4),
                "streak": int(m["streak"][t]),
                "trend": bool(m["trend"][t]),
                "ever20": int(m["ever"][t]),
            }
            if alive:
                alive_by_day[day].add(code)

    all_days = sorted(flags)
    for i, day in enumerate(all_days):
        rows = flags[day]
        alive = {c: r for c, r in rows.items() if r["alive"]}
        cooling = {c: r for c, r in rows.items() if r["state"] == "COOLING"}
        dead = {c: r for c, r in rows.items() if r["state"] == "DEAD"}
        born = {c: r for c, r in rows.items() if r["state"] == "BORN"}
        prev_alive_set = alive_by_day.get(all_days[i - 1], set()) if i > 0 else set()
        died = sorted(prev_alive_set - set(alive))
        top = sorted(alive.items(), key=lambda kv: kv[1]["run"], reverse=True)[:10]
        by_day[day] = {
            "n_alive": len(alive),
            "n_streak": sum(1 for r in alive.values() if r["streak"] >= STREAK_HIGH),
            "n_trend": sum(1 for r in alive.values() if r["trend"]),
            "n_cooling": len(cooling), "n_dead_state": len(dead),
            "n_born": len(born), "n_died": len(died),
            "top_run": round(max((r["run"] for r in alive.values()), default=0.0), 4),
            "max_height": max((r["streak"] for r in alive.values()), default=0),
            "top10": [{"ts_code": c, **{k: r[k] for k in
                       ("name", "kind", "run", "near", "streak", "state")}}
                      for c, r in top],
            "died": [{"ts_code": c, "name": names.get(c, c)} for c in died[:8]],
        }

    latest = all_days[-1] if all_days else ""
    latest_top = [
        {"ts_code": c, **r}
        for c, r in sorted(flags.get(latest, {}).items(),
                           key=lambda kv: (kv[1]["alive"], kv[1]["run"]),
                           reverse=True)[:40]
    ] if latest else []

    ex = examples(panel, names)
    pcases = period_cases(panel, names, by_day)
    psum = period_summary(pcases)
    recent_days = all_days[-10:]
    timeline = [
        {"date": d, **{k: by_day[d][k] for k in
                       ("n_alive", "n_streak", "n_trend", "n_cooling", "n_born",
                        "n_died", "top_run", "max_height")}}
        for d in recent_days
    ]

    out: dict[str, Any] = {
        "available": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "panel_last": str(panel["trade_date"].max()),
        "sessions": len(all_days),
        "config": config(),
        "definitions": {
            "high_leader": "高标 = 连板高标（streak≥3）∪ 趋势龙头（20日低点起≥50%、涨多跌少、至少1涨停、仍在高位、非纯连板）",
            "states": {k: v for k, v in KIND_CN.items()},
            "period_states": {
                "ALIVE": "当日满足高标形态", "COOLING": "曾为龙头、回踩未破位",
                "DEAD": "形态退潮", "NOT_YET": "尚未走出高标形态（启动/酝酿段）",
            },
            "period_case": "用户事后标注的历史龙头周期，按区间逐日回放识别状态；"
                           "首认滞后 = 区间起点到首次 ALIVE 的交易日数，不是漏报、也不是实时买点。",
            "note": "用户口径：高标不一定是连板最高，也可能是连续上涨、中间回踩、不都涨停的龙头。",
        },
        "examples": ex,
        "examples_hit": {
            "given": len(EXAMPLES),
            "alive": sum(1 for x in ex if x.get("state") == "ALIVE"),
            "cooling": sum(1 for x in ex if x.get("state") == "COOLING"),
            "streak_high": sum(1 for x in ex if x.get("is_streak_high")),
            "trend_only": sum(1 for x in ex if x.get("is_trend")
                              and not x.get("is_streak_high")),
        },
        "period_cases": pcases,
        "period_summary": psum,
        "latest_day": latest,
        "latest_top": latest_top,
        "timeline": timeline,
        "by_day": by_day,
        "caveats": [
            "阈值是样本内标定（为把用户给的 6 只票按真实日线正确归类），未做参数寻优，"
            "不构成策略有效性证据，也不得直接用于选股；补充的两个历史龙头周期**未触发任何调参**。",
            "五个历史区间（大众交通、常山北明、乐山电力、华电辽能、华电能源）都是用户**事后标注**的"
            "龙头/趋势牛股周期，不是实时可识别信号。区间起点处的启动/酝酿段尚未满足高标形态"
            "（20 日涨幅不足 50%），识别器给出 NOT_YET 属正常；若把起涨第 1 天就判为高标，"
            "会把全市场大量普通上涨股一起标进来。",
            "后三个跨月趋势牛股（乐山电力、华电辽能、华电能源）主升段有明显断档："
            "中间大回撤使 20 日窗口的 ever 归零，识别器会暂时判 NOT_YET / DEAD 后再 BORN。"
            "这是形态真实反映，不是识别器故障，也说明「持续性题材」无法用单条连续 ALIVE 段表达。",
            "主升段连续命中只说明形态覆盖，不等于策略能吃到这段涨幅：区间是事后圈定的，"
            "没有实时买点、没有排板可成交性、没有卖出规则，也未经样本外验证。",
            "「高标存亡」是形态状态，不是资金流或龙虎榜数据；仓库无 Level-2 与席位数据。",
            "COOLING 表示曾为趋势龙头、当前回踩但未破位，不等于「还会继续涨」；"
            "DEAD 只表示形态退潮，不代表基本面变化。",
            "历史只有日线 OHLC，无分时/委托队列；涨停判定用收盘价与前收推算的涨停价，"
            "ST 股已按名称剔除，可能与交易所口径有细微差异。",
        ],
    }
    # 内存用（weekly_review 直接取），不落盘
    out["_flags"] = flags
    return out


def save(out: dict[str, Any]) -> None:
    disk = {k: v for k, v in out.items() if k != "_flags"}
    OUT.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")


def cached() -> dict[str, Any]:
    if not OUT.is_file():
        return {"available": False, "reason": f"缺少 {OUT}"}
    try:
        d = json.loads(OUT.read_text(encoding="utf-8"))
        d["available"] = True
        return d
    except Exception:  # noqa: BLE001
        return {"available": False, "reason": "解析失败"}


if __name__ == "__main__":
    res = build()
    save(res)
    print("sessions", res["sessions"], "latest", res["latest_day"])
    print("examples_hit", json.dumps(res["examples_hit"], ensure_ascii=False))
    for x in res["examples"]:
        print(" ", x["name"], x.get("state"), x.get("readout"))
    print("period_summary", json.dumps(res["period_summary"], ensure_ascii=False))
    for x in res["period_cases"]:
        print(" ", x["name"], x.get("readout"))
        print("    counts", x.get("counts"), "| first_alive", x.get("first_alive"),
              "| main_leg", x.get("main_leg"), "| streak_only_days", x.get("streak_only_days"))
    print("timeline tail", json.dumps(res["timeline"][-3:], ensure_ascii=False))
    print("saved", OUT)
