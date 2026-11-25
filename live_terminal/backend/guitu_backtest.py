"""悟空真经 · 二板排板历史回放（精简版唯一策略）。

口径（用户裁定）
------------------------------------------------
① 断板卖出 = **断板当日收盘**（不是断板次日）。
② 买点 = **二板当日涨停瞬间排板**（不是收盘价默认成交）。
⑥ 成本口径（用户 2026-11-23 裁定）→ 万 0.84 免五，单一口径；另列毛收益对照。

排板买点的诚实边界
------------------------------------------------
「涨停瞬间排板」= 在二板封板那一刻以涨停价挂单排队。按东财 Q037 公开规则，
**不开板不成交，开板且卖一有单才成交，成交价 = 委托价（涨停价）**。

仓库只有日线 OHLC，没有 Level-2 委托队列与分时逐笔：
- 一字板 → 确定买不进（跳过）；
- 其余 → 当日曾出现低于涨停价的价格，但无法判断发生在挂单之前还是炸板之后，
  **排板是否成交不可判定**；
- 因此历史表给的是「假设全部排到」的**乐观上界**，不是成交记录，也不是策略结论；
- **真实排板判定只在实时留档上做**：`queue_match.live_queue_book()` 用封单额与开板次数
  按 Q037 给出「开板过·可成交 / 未开板·排队中未成交 / 不可判定」。

数据：`feeds.build_panel()` 真实日线，零合成。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from backend import costs, feature_engine, feeds, paths, queue_match

ROOT = Path(__file__).resolve().parents[1]

# 数据与代码分离：输出文件落在数据根，不在代码目录。
OUT = paths.data_dir() / "guitu_sim.json"

#: 单一成本口径（用户 2026-11-23 裁定）：万0.84免五，见 costs.DEFAULT。
#: 历史回放只有日线、无逐笔队列，成本按纯费率近似（不含逐腿差异）。
COST_MAIN = round(costs.DEFAULT.round_trip_rate(), 6)   # ≈0.003688
COST_GROSS = 0.0        # 毛收益对照（不含成本），仅参考


def _arrays(df: pl.DataFrame) -> dict[str, Any]:
    df = df.sort("trade_date")
    return {
        "o": df["open"].to_numpy().astype(float),
        "h": df["high"].to_numpy().astype(float),
        "l": df["low"].to_numpy().astype(float),
        "c": df["close"].to_numpy().astype(float),
        "dates": [str(x) for x in df["trade_date"].to_list()],
    }


def _zt_flags(code: str, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """兼容别名：判据实现已统一到 `feature_engine`，这里只转调、不再自带逻辑。

    等价性由 `tests/test_feature_parity.py` 逐日证明（91 例，含真实面板抽样）。
    """
    return feature_engine.zt_flags(code, c)


def replay_guitu(panel: pl.DataFrame, names: dict[str, str]) -> dict[str, Any]:
    by = panel.partition_by("ts_code", as_dict=True)
    events: list[dict[str, Any]] = []
    queue_counts: dict[str, int] = defaultdict(int)
    censored = 0

    for k, df in by.items():
        code = k[0] if isinstance(k, tuple) else k
        name = names.get(code, "")
        if not name or feature_engine.is_st(name):
            continue
        a = _arrays(df)
        o, h, l, c, dates = a["o"], a["h"], a["l"], a["c"], a["dates"]
        n = len(c)
        if n < 30:
            continue
        zt, streak = _zt_flags(code, c)
        # 断板日一次算全：取代原先每个二板信号都从 t+1 向后逐日找的 O(n²) 内层循环。
        # next_nz[t] = t 之后首个未涨停日下标，-1 表示到序列末端仍连板（右删失）。
        next_nz = feature_engine.next_non_zt(zt)

        for t in range(1, n - 1):
            if streak[t] != 2 or streak[t - 1] != 1:
                continue                       # 只从「二板晋级」开始
            # 裁定②：二板封板瞬间挂涨停价排队，日线只能判定一字板确实买不进
            q = queue_match.classify_history(o[t], h[t], l[t], c[t], c[t])
            queue_counts[q["status"]] += 1
            if q["fillable"] is False:
                continue                       # one_word / sealed：确定排不到
                                                # uncertain(None) 保留，作为「不可判定」上界样本
            d = int(next_nz[t])
            if d < 0:
                censored += 1                  # 右删失：到末端仍连板
                continue
            max_streak = int(streak[d - 1]) if d > t else 2
            ev: dict[str, Any] = {
                "ts_code": code, "name": name,
                "signal_date": dates[t], "exit_date": dates[d],
                "hold": int(d - t), "max_streak": max_streak,
                "year": int(dates[t][:4]),
                "queue_status": q["status"], "queue_reason": q["reason"],
                "entry_rule": "二板封板瞬间挂涨停价排队（成交不可判定）",
                "exit_rule": "首个未涨停日（断板）当日收盘卖出",
            }
            # 主口径：涨停价委托，假设排到 → 断板当日收盘卖（乐观上界）
            ev["a"] = float(c[d] / c[t] - 1) if c[t] > 0 else None
            # 参考（已被裁定①②否决，仅留作对照，不作为策略口径）
            ev["b"] = float(c[d] / o[t + 1] - 1) if o[t + 1] > 0 else None
            ev["c"] = (float(o[d + 1] / o[t + 1] - 1)
                       if d + 1 < n and o[t + 1] > 0 and o[d + 1] > 0 else None)
            events.append(ev)

    # 同日同身位「有兔子」标记（一字二板）
    rabbit_days: dict[str, int] = defaultdict(int)
    for k, df in by.items():
        code = k[0] if isinstance(k, tuple) else k
        a = _arrays(df)
        c, o, h, l = a["c"], a["o"], a["h"], a["l"]
        if len(c) < 30:
            continue
        zt, streak = _zt_flags(code, c)
        for t in range(1, len(c)):
            if (streak[t] == 2 and streak[t - 1] == 1
                    and abs(o[t] - h[t]) < 1e-9 and abs(o[t] - l[t]) < 1e-9):
                rabbit_days[a["dates"][t]] += 1
    for ev in events:
        ev["has_rabbit"] = rabbit_days.get(ev["signal_date"], 0) > 0

    return {"events": events, "queue_counts": dict(queue_counts),
            "one_word_skip": queue_counts.get("one_word", 0),
            "censored": censored, "rabbit_days": len(rabbit_days)}


def _stat(rows: list[dict[str, Any]], key: str, cost: float) -> dict[str, Any]:
    vals = [x[key] - cost for x in rows if x.get(key) is not None]
    if not vals:
        return {"n": 0}
    v = np.array(vals, dtype=float)
    gross = np.array([x[key] for x in rows if x.get(key) is not None], dtype=float)
    by_year = {}
    for y in sorted({x["year"] for x in rows if x.get(key) is not None}):
        g = [x[key] - cost for x in rows if x["year"] == y and x.get(key) is not None]
        by_year[str(y)] = {
            "n": len(g), "win": round(float(np.mean([q > 0 for q in g])), 4),
            "median_net": round(float(np.median(g)), 5),
        }
    return {
        "n": len(vals),
        "win": round(float((v > 0).mean()), 4),
        "mean_net": round(float(v.mean()), 5),
        "median_net": round(float(np.median(v)), 5),
        "mean_gross": round(float(gross.mean()), 5),
        "cum_10pct": round(float(np.sum(v) * 0.10), 4),
        "by_year": by_year,
    }


def build(panel: pl.DataFrame | None = None) -> dict[str, Any]:
    if panel is None:
        panel = feeds.build_panel()
    names = dict(zip(feeds.stock_list()["ts_code"].to_list(),
                     feeds.stock_list()["name"].to_list()))

    guitu = replay_guitu(panel, names)
    ev = guitu["events"]

    stats: dict[str, Any] = {}
    stats["龟兔·二板排板买入→断板当日收盘卖（主口径·假设排到的上界）"] = _stat(ev, "a", COST_MAIN)
    stats["龟兔·毛收益对照（不含成本，仅参考）"] = _stat(ev, "a", COST_GROSS)
    stats["对照B·次日开盘买（已否决·仅参考）"] = _stat(ev, "b", COST_MAIN)
    stats["对照C·断板次日开盘卖（已否决·仅参考）"] = _stat(ev, "c", COST_MAIN)

    # 龟兔分组：有无同日同身位兔子
    groups = {
        "有兔子（同日同身位存在一字二板）": [x for x in ev if x["has_rabbit"]],
        "无兔子": [x for x in ev if not x["has_rabbit"]],
    }
    by_rabbit = {k: _stat(v, "a", COST_MAIN) for k, v in groups.items()}
    by_hold = {}
    for lo, hi, label in ((1, 1, "断板在 T+1（持有1日）"),
                          (2, 2, "持有2日"), (3, 5, "持有3-5日"), (6, 99, "持有6日以上")):
        by_hold[label] = _stat([x for x in ev if lo <= x["hold"] <= hi], "a", COST_MAIN)
    by_height = {}
    for lo, hi, label in ((2, 2, "止步2板"), (3, 3, "走到3板"),
                          (4, 5, "走到4-5板"), (6, 99, "走到6板以上")):
        by_height[label] = _stat([x for x in ev if lo <= x["max_streak"] <= hi], "a", COST_MAIN)

    qc = guitu["queue_counts"]
    caveats = [
        "裁定②：买点是「二板当日涨停瞬间挂涨停价排板」，不是收盘价默认成交。"
        "历史只有日线 OHLC，无委托队列/分时，除一字板确定买不进外，"
        f"其余 {qc.get('uncertain', 0)} 笔的排板成交**不可判定**；"
        "表中主口径是「假设全部排到」的乐观上界，不是成交记录，不得当作策略结论。",
        "裁定①：卖点固定为断板当日收盘；「断板次日卖」与「次日开盘买」已否决，仅留对照行。",
        "按持有天数 / 最高连板分档出现的高胜率与高收益是同义反复："
        "「持有到首个断板日」决定了一直涨停才一直持有，赚多少由连板高度直接给出，"
        "不构成「策略能选到牛股」的证据，也不可用于自动交易决策。",
        "主口径均值净为正但中位净为负，是少数连板大牛股拉出的右偏长尾；"
        "典型一笔的体验看中位净，不看均值净。",
        "「有兔子 / 无兔子」分组为描述性差异，未做显著性检验，不得解释为因果。",
        "多重比较警示：by_rabbit / by_hold / by_height 是多组描述性切分，"
        "未做多重检验校正（如 Bonferroni / FDR）；其中出现的任何「高胜率」"
        "都可能是多重切分下的偶然，不得当显著性证据，更不得据此反向调参。",
        f"成本口径（用户2026-11-23裁定）：万0.84免五单一口径，往返 {COST_MAIN:.4%}/笔；"
        "另列不含成本的毛收益对照行。行业中性对照/完整 DGTW 因缺市值/BM 数据未做，"
        "不得把本表收益称为 alpha。",
        "历史表现不代表未来收益；本表为模拟回放，不是实盘成交；"
        "本系统不向任何外部账户发送交易指令。",
    ]

    latest = sorted(ev, key=lambda x: x["signal_date"], reverse=True)[:20]
    out = {
        "caveats": caveats,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "panel_last": str(panel["trade_date"].max()),
        "panel_rows": panel.height,
        "cost_main": COST_MAIN,
        "cost_status": "用户2026-11-23裁定：万0.84免五，单一口径；历史回放按纯费率近似",
        "benchmark": "同日全市场等权收益（裁定②；本表收益为绝对收益，未减基准）",
        "definition": "二板（streak 1→2）封板瞬间挂涨停价排板 → 首个未涨停日（断板）当日收盘卖出",
        "entry_rule": "二板当日涨停瞬间以涨停价挂单排队（一字板确定买不进则跳过）",
        "exit_rule": "断板当日收盘卖出（裁定①，非次日）",
        "stats": stats,
        "guitu": {
            "events": len(ev), "one_word_skip": guitu["one_word_skip"],
            "censored": guitu["censored"], "rabbit_days": guitu["rabbit_days"],
            "queue_counts": qc,
            "queue_uncertain_share": round(
                qc.get("uncertain", 0) / max(sum(qc.values()), 1), 4),
            "hold_median": float(np.median([x["hold"] for x in ev])) if ev else 0,
            "by_rabbit": by_rabbit, "by_hold": by_hold, "by_height": by_height,
        },
        "ab": None,
        "queue_rule": queue_match.EXTERNAL_RESEARCH["queue_rule_sources"],
        "latest": latest,
        "note": (
            "悟空真经唯一策略：龟兔买点按裁定②为二板涨停瞬间排板，"
            "历史排板成交不可判定，主口径为乐观上界；真实排板状态见「今日实时二板排板委托单」。"
            "卖点按裁定①固定为断板当日收盘。成本按裁定⑥并列未定。"
            "历史表现不代表未来收益，不可用于自动交易决策。"
        ),
    }
    return out


def cached_result() -> dict[str, Any]:
    if not OUT.is_file():
        return {"available": False}
    try:
        d = json.loads(OUT.read_text(encoding="utf-8"))
        d["available"] = True
        return d
    except Exception:  # noqa: BLE001
        return {"available": False}


def today_orders() -> list[dict[str, Any]]:
    """今日实时二板排板委托单（裁定②），由 `queue_match` 按公开规则判定。"""
    return queue_match.live_queue_book().get("orders", [])


if __name__ == "__main__":
    res = build()
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in res["stats"].items() if isinstance(v, dict)},
                     ensure_ascii=False, indent=1)[:5000])
    print("guitu", res["guitu"]["events"], "saved", OUT)
