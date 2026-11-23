"""高标周期 · 多维案例册与归纳学习（用户 2026-11-12 第三批补充）。

用户要求：「以上股票都是市场选出来的龙头高标，是具有意义的，可以参考当时的
连板梯队、行业板块、消息刺激和日内走势、换手、量能等等……进行归纳整理学习，
复用在悟空真经里面。」

**维度可得性先说清（不编数据）**
------------------------------------------------
| 维度 | 仓库数据 | 能否回溯历史周期 |
| --- | --- | --- |
| 连板梯队 | `leader_ladder.json` 的逐日 by_day（688 交易日） | ✅ 可，含 max_height / n_alive / n_streak |
| 行业板块 | `industry_history.parquet` 带 change_date | ✅ 可做时点归属（PIT），不用今天行业倒推 |
| 换手 / 量能 | panel 的 turnover / volume / amount | ✅ 可，含量比（前 20 日均量） |
| 日内走势 | 只有日线 OHLC | ⚠️ 仅能给**形态代理**：一字/T字/换手板、振幅、上下影、收盘位置；无分时 |
| 消息刺激 | 实时快讯留档 `data/live/<day>` | ❌ 只有 2026-11-12 一天，2024–2026 历史消息面不可得 |
| 委托队列 / 龙虎榜 / 资金流 | 无 | ❌ 不可得 |

**选样边界（必须一起读）**
------------------------------------------------
这 10 个区间都是用户**事后选出「已经是龙头」**的样本，没有对照组、没有失败案例，
所以任何「共性」都是选样条件的结果，不是发现、不是预测因子、不是策略有效性证据。
归纳只作为悟空真经的**可核对判据清单**与待检验假设，不进入开仓规则、不改阈值。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from backend import feature_engine, leader_ladder, paths

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "data" / "panel_raw.parquet"
INDUSTRY_HIST = ((paths.MARKET_CACHE / "meta" / "industry_history.parquet")
                 if paths.MARKET_CACHE is not None
                 else ROOT / "data" / "market_cache" / "meta" / "industry_history.parquet")
LIVE = ROOT / "data" / "live"


def _pit_industry(hist: pl.DataFrame, code: str, day: str) -> str:
    """取 ≤ 观察日的最后一条行业归属，避免前视偏差。"""
    d = hist.filter((pl.col("ts_code") == code) &
                    (pl.col("change_date") <= date.fromisoformat(day)))
    if d.height == 0:
        return ""
    return str(d.sort("change_date")["industry"].to_list()[-1])


def _news_availability() -> dict[str, Any]:
    days = sorted(p.name for p in LIVE.iterdir() if p.is_dir() and p.name.isdigit())
    return {
        "available": False,
        "reason": "实时快讯留档从 2026-11-12 才开始，2024–2026 各历史周期没有对应的消息面数据；"
                  "不编造、不用今天的新闻倒推历史。",
        "live_days": days,
        "note": "若要补这一维，需要历史新闻源（财联社/东财快讯归档）并对齐交易日，当前仓库没有。",
    }


def _shape_metrics(d: pl.DataFrame, zt: np.ndarray, code: str) -> dict[str, Any]:
    """日线 OHLC 形态代理（不是分时）。"""
    o = d["open"].to_numpy().astype(float)
    h = d["high"].to_numpy().astype(float)
    lo = d["low"].to_numpy().astype(float)
    c = d["close"].to_numpy().astype(float)
    v = d["volume"].to_numpy().astype(float)
    amt = d["amount"].to_numpy().astype(float)
    to = d["turnover"].to_numpy().astype(float)
    prev = np.roll(c, 1)
    prev[0] = np.nan

    amp = np.where(prev > 0, (h - lo) / prev, 0.0)
    upper = np.where(prev > 0, (h - np.maximum(o, c)) / prev, 0.0)
    lower = np.where(prev > 0, (np.minimum(o, c) - lo) / prev, 0.0)
    den = np.where(h > lo, h - lo, 1.0)   # 避免一字板 h==lo 除零
    rng = np.where(h > lo, (c - lo) / den, 0.5)
    # 前 20 日均量（只用过去，不含当日）
    vol20 = np.concatenate([
        np.full(20, np.nan),
        np.convolve(v[:-1], np.ones(20) / 20, mode="valid")
    ])[:len(v)]
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = np.where(vol20 > 0, v / vol20, np.nan)

    one_word = zt & (h <= lo + 1e-6)
    t_word = zt & (~one_word) & (np.abs(o - h) < 1e-6) & (np.abs(c - h) < 1e-6) & (lo < o - 1e-6)
    turnover_board = zt & (~one_word) & (~t_word)
    zt_idx = np.flatnonzero(zt)
    return {
        "one_word_days": int(one_word.sum()),
        "t_word_days": int(t_word.sum()),
        "turnover_board_days": int(turnover_board.sum()),
        "zt_days": int(zt.sum()),
        "one_word_share_of_zt": round(float(one_word.sum() / max(zt.sum(), 1)), 4),
        "amplitude_median": round(float(np.nanmedian(amp)), 4),
        "upper_shadow_median": round(float(np.nanmedian(upper)), 4),
        "lower_shadow_median": round(float(np.nanmedian(lower)), 4),
        "close_position_median": round(float(np.nanmedian(rng)), 4),
        "turnover_median": round(float(np.nanmedian(to)), 2),
        "turnover_max": round(float(np.nanmax(to)), 2),
        "turnover_on_zt_median": round(float(np.nanmedian(to[zt_idx])), 2) if zt_idx.size else None,
        "vol_ratio_median": round(float(np.nanmedian(vr)), 3),
        "vol_ratio_on_zt_median": round(float(np.nanmedian(vr[zt_idx])), 3) if zt_idx.size else None,
        "amount_yi_median": round(float(np.nanmedian(amt)) / 1e8, 3),
        "note": "一字/T字/换手板由日线 OHLC 推定；无分时与委托队列，不能还原日内走势或排板成交。",
    }


def _ladder_stats(by_day: dict[str, Any], dates: list[str],
                  streak_seg: np.ndarray) -> dict[str, Any]:
    """dates 与 streak_seg 均为**区间切片**后等长序列（避免全局/局部索引错位）。"""
    smap = dict(zip(dates, [int(x) for x in streak_seg]))
    hit_days = [x for x in dates if x in by_day]
    rows = [by_day[x] for x in hit_days]
    if not rows:
        return {"available": False, "reason": "该区间不在梯队 by_day 覆盖内"}
    heights = [r["max_height"] for r in rows]
    alive = [r["n_alive"] for r in rows]
    gap = [h - smap[x] for h, x in zip(heights, hit_days)]
    return {
        "available": True,
        "sessions": len(rows),
        "max_height_median": float(np.median(heights)),
        "max_height_max": int(max(heights)),
        "alive_median": round(float(np.median(alive)), 1),
        "self_best_gap_to_top": int(min(gap)) if gap else None,
        "self_days_at_top": sum(1 for g in gap if g <= 0),
        "note": "梯队来自全市场逐日宽口径高标识别；self_best_gap_to_top=0 表示该票当日并列最高板。",
    }


def _max_drawdown(c: np.ndarray) -> float:
    peak = np.maximum.accumulate(c)
    return float(np.min(c / np.where(peak > 0, peak, np.nan) - 1))


def _panel_read() -> pl.DataFrame:
    """真实日线面板（经 feeds.build_panel 的合并视图，含 panel_live 近日行）。

    2026-11-18 验收修复（P0）：原为 `pl.read_parquet(PANEL)` 直读 panel_raw，
    而 panel_raw 只由离线脚本重建，实测停在 2026-11-12，导致案例册/周期看板的
    行情基准恒落后 4 个交易日。改为统一入口并保留回退。
    """
    try:
        from backend import feeds
        df = feeds.build_panel()
        if df is not None and df.height > 0:
            return df
    except Exception as e:  # noqa: BLE001
        print(f"[leader_casebook] build_panel 不可用，回退 panel_raw: "
              f"{type(e).__name__}: {e}", flush=True)
    return pl.read_parquet(PANEL)


def casebook() -> dict[str, Any]:
    panel = _panel_read()
    hist = pl.read_parquet(INDUSTRY_HIST)
    ladder = leader_ladder.cached()
    by_day: dict[str, Any] = ladder.get("by_day") or {}
    periods = {x["name"]: x for x in ladder.get("period_cases", [])}

    cases: list[dict[str, Any]] = []
    for cp in leader_ladder.CASE_PERIODS:
        code, nm = cp["ts_code"], cp["name"]
        d = panel.filter(pl.col("ts_code") == code).sort("trade_date")
        if d.height < 25:
            cases.append({**cp, "available": False, "reason": "日线不足"})
            continue
        c = d["close"].to_numpy().astype(float)
        pct = d["pct_chg"].to_numpy().astype(float)
        zt, streak = feature_engine.zt_flags(code, c)
        dates = [str(x) for x in d["trade_date"].to_list()]
        idx = [i for i, x in enumerate(dates) if cp["start"] <= x <= cp["end"]]
        if not idx:
            cases.append({**cp, "available": False, "reason": "区间内无交易日"})
            continue
        i0, i1 = idx[0], idx[-1]
        seg = d.slice(i0, i1 - i0 + 1)
        zseg = zt[i0:i1 + 1]
        sseg = streak[i0:i1 + 1]

        # 连板段
        segments = []
        j = 0
        while j < len(zseg):
            if zseg[j]:
                k = j
                while k + 1 < len(zseg) and zseg[k + 1]:
                    k += 1
                if k - j + 1 >= 2:
                    segments.append({"start": dates[i0 + j], "end": dates[i0 + k],
                                     "boards": int(k - j + 1)})
                j = k + 1
            else:
                j += 1

        per = periods.get(nm, {})
        cases.append({
            **cp,
            "available": True,
            "actual_start": dates[i0], "actual_end": dates[i1],
            "sessions": len(idx),
            "price": {
                "start": round(float(c[i0]), 2), "end": round(float(c[i1]), 2),
                "gain": round(float(c[i1] / c[i0] - 1), 4),
                "high": round(float(c[i0:i1 + 1].max()), 2),
                "low": round(float(c[i0:i1 + 1].min()), 2),
                "max_drawdown": round(_max_drawdown(c[i0:i1 + 1]), 4),
            },
            "board": {
                "max_streak": int(sseg.max()),
                "zt_days": int(zseg.sum()),
                "non_zt_up_days": int(((pct[i0:i1 + 1] > 0) & ~zseg).sum()),
                "down_days": int((pct[i0:i1 + 1] < 0).sum()),
                "segments": segments,
                "n_segments": len(segments),
                "zt_share": round(float(zseg.sum() / len(idx)), 4),
            },
            "shape": _shape_metrics(seg, zseg, code),
            "ladder": _ladder_stats(by_day, [dates[i] for i in idx], sseg),
            "industry": {
                "at_start": _pit_industry(hist, code, dates[i0]),
                "at_end": _pit_industry(hist, code, dates[i1]),
                "source": "industry_history.parquet · change_date ≤ 观察日（PIT）",
                "note": "行业是分类归属，不是「题材」；题材需消息面/概念标签，仓库没有历史标签。",
            },
            "leader": {
                "alive_days": (per.get("counts") or {}).get("ALIVE"),
                "coverage": per.get("coverage"),
                "first_alive": per.get("first_alive", ""),
                "lag_sessions": (per.get("startup") or {}).get("lag_sessions"),
                "contiguous": (per.get("main_leg") or {}).get("contiguous"),
                "gaps": (per.get("main_leg") or {}).get("gaps"),
            },
            "news": _news_availability(),
        })

    return {
        "available": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "panel_last": str(panel["trade_date"].max()),
        "dimensions": {
            "连板梯队": "✅ 全市场逐日 by_day（max_height / 存活数 / 自身身位）",
            "行业板块": "✅ industry_history 时点归属（PIT）；注意行业≠题材",
            "换手 / 量能": "✅ turnover / 量比（前20日均量）/ 成交额",
            "日内走势": "⚠️ 仅日线 OHLC 形态代理（一字/T字/换手板、振幅、影线、收盘位置），无分时",
            "消息刺激": "❌ 快讯留档只有 2026-11-12，历史消息面不可得，不编造",
            "委托队列 / 龙虎榜 / 资金流": "❌ 仓库无此数据",
        },
        "cases": cases,
        "induction": _induct(cases),
        "caveats": [
            "10 个区间都是用户事后选出「已经是龙头」的样本，无对照组、无失败案例；"
            "所有共性都是选样条件的结果，不是发现、不是预测因子、不是策略有效性证据。",
            "「日内走势」只有日线 OHLC 推定，一字/T字/换手板是形态近似，"
            "不能替代分时或委托队列，也不能据此判定排板是否成交。",
            "历史消息刺激完全缺失；不得用今天（2026-11-12）的快讯倒推 2024–2026 的题材。",
            "归纳结果只登记为悟空真经的**可核对判据清单与待检验假设**，"
            "不进入开仓规则、不改任何阈值、不做样本外推断。",
        ],
    }


def _induct(cases: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [x for x in cases if x.get("available")]
    if not ok:
        return {"available": False}

    def med(vals: list[Any]) -> float | None:
        v = [float(x) for x in vals if x is not None and np.isfinite(float(x))]
        return round(float(np.median(v)), 4) if v else None

    gains = [x["price"]["gain"] for x in ok]
    maxst = [x["board"]["max_streak"] for x in ok]
    ztshare = [x["board"]["zt_share"] for x in ok]
    oneword = [x["shape"]["one_word_days"] for x in ok]
    tword = [x["shape"]["t_word_days"] for x in ok]
    tboard = [x["shape"]["turnover_board_days"] for x in ok]
    to = [x["shape"]["turnover_median"] for x in ok]
    vr = [x["shape"]["vol_ratio_median"] for x in ok]
    ladder_h = [x["ladder"]["max_height_median"] for x in ok if x["ladder"].get("available")]
    alive_med = [x["ladder"]["alive_median"] for x in ok if x["ladder"].get("available")]
    cover = [x["leader"]["coverage"] for x in ok if x["leader"].get("coverage") is not None]
    lag = [x["leader"]["lag_sessions"] for x in ok if x["leader"].get("lag_sessions") is not None]
    inds = [x["industry"]["at_start"] for x in ok if x["industry"].get("at_start")]

    return {
        "available": True,
        "n_cases": len(ok),
        "stats": {
            "区间涨幅中位": med(gains),
            "区间涨幅范围": [round(min(gains), 4), round(max(gains), 4)],
            "最大连板中位": med(maxst),
            "最大连板范围": [min(maxst), max(maxst)],
            "涨停日占比中位": med(ztshare),
            "一字板天数合计": int(sum(oneword)),
            "T字板天数合计": int(sum(tword)),
            "换手板天数合计": int(sum(tboard)),
            "换手率中位（各周期中位数的中位）": med(to),
            "量比中位": med(vr),
            "同期全场最高板中位": med(ladder_h),
            "同期存活高标数中位": med(alive_med),
            "宽口径 ALIVE 覆盖中位": med(cover),
            "首认滞后中位（交易日）": med(lag),
            "行业分布": {k: inds.count(k) for k in sorted(set(inds))},
        },
        "reusable_checks": [
            {
                "item": "可成交性优先",
                "rule": "一字/T字板占比高的段落，排板确定买不进；"
                        "同题材同身位下优先核对换手板（乌龟）能否成交。",
                "evidence": f"10 周期合计一字 {int(sum(oneword))} 天、T字 {int(sum(tword))} 天、"
                            f"换手板 {int(sum(tboard))} 天；全市场回放另有 1,040 笔一字确定跳过。",
            },
            {
                "item": "题材热度用梯队而非涨幅代理",
                "rule": "用当日全场 max_height、存活高标数、同板块高标数作热度代理；"
                        "但已有分箱显示同板块高标≥3 只时晋级率反而更低，属反向证据，不能当买入条件。",
                "evidence": f"10 周期同期全场最高板中位 {med(ladder_h)}、存活高标数中位 {med(alive_med)}。",
            },
            {
                "item": "启动段不等于买点",
                "rule": "首认 ALIVE 有滞后；识别器是形态确认，不是入场信号。",
                "evidence": f"首认滞后中位 {med(lag)} 个交易日；跨月多波周期还会断档后重新 BORN。",
            },
            {
                "item": "高标 ≠ 最高连板",
                "rule": "用宽口径（连板高标 ∪ 趋势龙头）看存亡，不用 max_height 单点。",
                "evidence": f"最大连板中位 {med(maxst)}，但宽口径 ALIVE 覆盖中位 {med(cover)}；"
                            "此前两个周期用「连板≥3」口径 73 天只命中 1 天。",
            },
            {
                "item": "行业归属必须时点化",
                "rule": "历史板块归属取 change_date ≤ 观察日，不用今天的行业倒推。",
                "evidence": f"10 周期起点 PIT 行业：{', '.join(sorted(set(inds)))}；"
                            "行业是分类，不是题材，题材需历史概念标签（缺失）。",
            },
        ],
        "verdict": "以上是可核对的**判据清单**与待检验假设，不是策略结论。"
                   "用户命题「有连板行情 + 持续性题材 → 龟兔最适合操作」目前只满足"
                   "「执行前提（一字买不进）」，缺「持续性题材」定义、缺对照组、缺样本外验证。",
    }


if __name__ == "__main__":
    r = casebook()
    print("cases", len(r["cases"]), "panel_last", r["panel_last"])
    for x in r["cases"]:
        if not x.get("available"):
            print(" ", x["name"], "N/A", x.get("reason"))
            continue
        print(f"  {x['name']:<8}{x['actual_start']}~{x['actual_end']} {x['sessions']:>3}日 "
              f"涨幅{x['price']['gain']*100:+7.0f}% 最高{x['board']['max_streak']}板 "
              f"涨停{x['board']['zt_days']:>2} 一字{x['shape']['one_word_days']:>2} "
              f"T字{x['shape']['t_word_days']:>2} 换手板{x['shape']['turnover_board_days']:>2} "
              f"换手中位{x['shape']['turnover_median']:>5.1f}% 量比{x['shape']['vol_ratio_median']:>5.2f} "
              f"梯队最高板中位{x['ladder'].get('max_height_median')} 行业{x['industry']['at_start']}")
    print("induction", json.dumps(r["induction"]["stats"], ensure_ascii=False, indent=1))
