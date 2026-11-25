# -*- coding: utf-8 -*-
"""排板成交模拟 · 完整算法（2026-11-16）。

## 为什么需要单独的排板模拟

打板买入 = 在涨停价挂买单，**排在封单队列尾部**。
「涨停被打开」不等于我们的单成交：只有开板期间砸进来的卖单
把**我们前面的队列**吃掉，才轮得到我们。

而且一天可能开板多次——每开一次都会消耗一部分队列，
没吃穿就队列前移，下一轮继续排队。所以必须**逐轮累积**，不能一开就判成交。

## 算法

```
挂单：信号帧以涨停价挂买单，队列前方 = S（挂单时封单额）
逐帧推进：
  若该票仍在池且 broken 未增 → 继续排队
  若发生开板（broken↑ 或 离池）→ 结算本轮：
         V     = 本轮开板期间成交额（上界）
         ratio = V / S
         if ratio >= 1        → 队列被吃穿，我方成交（成交价=委托价）
         else                 → 未成交，队列前移：S ← S × (1 - ratio)
                                 记入 rounds，继续排下一轮
收盘：
  任一轮成交 → filled（记成交帧/第几轮）
  否则       → queued_unfilled
```

## 成交额怎么算（不需要流通市值）

```
当日成交额 A = (当日总换手/100) × 流通市值
本轮成交额   = (本轮换手增量/100) × 流通市值
两式相除消掉流通市值：
本轮成交额 = A × (本轮换手增量 / 当日总换手)
```

## 保守性（重要）

- 本轮成交额是**上界**：开板后部分成交发生在涨停价以下，不消耗限价买队列。
- 封单里的挂单可能**撤销**而非被吃掉：撤单同样释放队列但无成交额。
  所以 `ratio < 1` 判「排不进」是可靠结论；`ratio >= 1` 只说明「有可能」。
- 结论一律带 `confidence`，不把边缘情形当确定成交。

## 数据来源（全部真实）

| 量 | 来源 |
|---|---|
| 累计换手率 | 涨停池留档帧 `turnover` |
| 封单额 | 涨停池留档帧 `seal_fund` |
| 当日成交额 | panel `amount` |
"""
from __future__ import annotations

from typing import Any

#: 消耗比 >= 1 视为队列被吃穿（成交）
RATIO_FILL = 1.0
#: 消耗比 >= 该值视为高置信成交
RATIO_STRONG = 2.0


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------

def frame_maps(frames: list[dict[str, Any]]) -> list[tuple[str, dict[str, dict[str, Any]]]]:
    """整段留档 → [(ts, {code: {turnover, seal_fund, broken, price}})]。"""
    from backend import live_sim as _ls
    out: list[tuple[str, dict[str, dict[str, Any]]]] = []
    for fr in frames:
        rows, _ = _ls._pool_rows(fr)
        m: dict[str, dict[str, Any]] = {}
        for r in rows:
            raw = str(r.get("ts_code") or "").strip()
            if not raw or not raw.isdigit():
                continue
            c = raw.zfill(6)
            vals: dict[str, Any] = {}
            for k, cast in (("turnover", float), ("seal_fund", float),
                            ("broken", int), ("price", float)):
                v = r.get(k)
                try:
                    vals[k] = cast(v) if v is not None else None
                except (TypeError, ValueError):
                    vals[k] = None
            m[c] = vals
        out.append((str(fr.get("ts") or ""), m))
    return out


_AMT_CACHE: dict[tuple[str, str], float | None] = {}


def daily_amount(day: str, code: str) -> float | None:
    """当日成交额（元）。带缓存，避免逐票重复读 373 万行面板。

    2026-11-24 盘中修复：panel_live 要 15:05 后才有当日 bar，盘中查询
    一律返回 None → `simulate_fill` 的「封单/成交额/换手缺一」让**全天
    消耗比都算不出来**。现在面板缺当日行时，回退读当日 quotes 留档
    最新帧的 `amount`（东财实时累计成交额，recorder 每 5 分钟落一帧）。
    """
    key = (str(day), str(code))
    if key in _AMT_CACHE:
        return _AMT_CACHE[key]
    val: float | None = None
    try:
        import datetime as _dt
        import polars as pl
        from backend import feeds
        p = feeds.build_panel()
        dash = day if "-" in day else f"{day[:4]}-{day[4:6]}-{day[6:]}"
        y, m, d = (int(x) for x in dash.split("-"))
        row = p.filter((pl.col("ts_code") == code)
                       & (pl.col("trade_date") == _dt.date(y, m, d)))
        if row.height:
            v = row["amount"][0]
            val = float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        val = None

    # 盘中回退：日线面板还没有当日 bar → 用 quotes 留档最新帧的累计成交额
    if val is None:
        try:
            from backend import archive
            df = archive.latest_frame(str(day), "quotes")
            if df is not None and df.height:
                r = df.filter(pl.col("ts_code") == str(code).zfill(6))
                if r.height:
                    a = r["amount"][0]
                    val = float(a) if a is not None else None
        except Exception:  # noqa: BLE001 - 留档缺失则保持 None（不猜）
            val = None

    # 2026-11-24 盘中：quotes 每 5 分钟才落一帧，本进程内首次查询时该票
    # 可能还没进最新帧 → **None 不缓存**，下次调用重试（拿到新帧就有值）。
    # 命中真实金额才缓存，避免"查过一次就没有"的假阴性。
    if val is not None:
        _AMT_CACHE[key] = val
    return val


# ---------------------------------------------------------------------------
# 核心：排板成交模拟
# ---------------------------------------------------------------------------

def simulate_fill(day: str, code: str, entry_ts: str,
                  frames: list[dict[str, Any]],
                  *, entry_price: float | None = None) -> dict[str, Any]:
    """完整排板模拟：逐轮开板累积消耗队列。

    返回：
      status       filled / queued_unfilled / unknown
      fill_ts      成交帧（成交时）
      fill_round   第几轮开板成交（从 1 开始）
      confidence   strong / edge / none
      rounds       每轮开板明细（成交额、消耗比、队列剩余）
      detail       人话说明
    """
    tmap = frame_maps(frames)
    c = str(code).zfill(6)
    ts_list = [x[0] for x in tmap]
    base = {"status": "unknown", "fill_ts": None, "fill_round": None,
            "confidence": "none", "rounds": [], "detail": ""}
    if entry_ts not in ts_list:
        return {**base, "detail": "挂单帧不在留档序列中"}
    i0 = ts_list.index(entry_ts)
    entry = tmap[i0][1].get(c)
    if entry is None:
        return {**base, "detail": "挂单帧内该票不在涨停池（数据缺口）"}

    S = entry.get("seal_fund")            # 挂单时封单额（仅用于统计排队消耗）
    broken_last = entry.get("broken")
    to_last = entry.get("turnover")
    amt_day = daily_amount(day, c)
    total_to = max((v["turnover"] or 0.0)
                   for (_, m) in tmap for cc, v in m.items() if cc == c)
    if S is None or not amt_day or total_to <= 0 or to_last is None:
        return {**base, "detail": "封单/成交额/换手缺一，无法模拟队列消耗"}

    rounds: list[dict[str, Any]] = []
    s_entry = S                     # 上一轮结束时我们前面的队列
    i_prev = i0                     # 最后一次「在池」的帧下标
    while i_prev + 1 < len(tmap):
        # ── 向后找到下一个开板事件；途中不断用最新封单更新「当前队列」 ──
        i_open, open_why = None, None
        j = i_prev + 1
        while j < len(tmap):
            cur = tmap[j][1].get(c)
            if cur is None:
                i_open, open_why = j, "炸板离池"
                break
            b_prev = tmap[i_prev][1][c].get("broken")
            b_cur = cur.get("broken")
            if (b_prev is not None and b_cur is not None
                    and int(b_cur) > int(b_prev)):
                i_open, open_why = j, f"开板 {b_prev}→{b_cur}"
                break
            i_prev = j              # 仍在池，前移；封单会随之下一次取最新值
            j += 1
        if i_open is None:
            break

        # ── 关键修正（2026-11-16 用户指出）──
        # 队列大小必须取「开板前最后一帧」的封单额，而不是挂单时的。
        # 排队期间封单一直在被吃掉：岳阳兴长 10:06 还 1.095 亿，
        # 10:29 开板时只剩 0.712 亿 —— 用挂单值会严重高估队列。
        cur_row = tmap[i_prev][1][c]
        s_now = cur_row.get("seal_fund")
        if s_now is None:
            s_now = s_entry
        wait_consumed = max(0.0, (s_entry or 0.0) - s_now)   # 排队期间被消耗
        to_before = cur_row.get("turnover") or to_last or 0.0

        # 开板期间成交额：从开板前换手到「恢复在池」时的换手增量
        to_after = None
        k = i_open
        while k < len(tmap):
            nx = tmap[k][1].get(c)
            if nx is not None and nx.get("turnover") is not None:
                to_after = nx["turnover"]
                break
            k += 1
        approx = False
        if to_after is None:
            to_after = total_to          # 一路炸板到收盘，用当日最终换手（上界）
            approx = True

        dto = (to_after or 0.0) - to_before
        # ── 2026-11-16：成交额优先用**分钟级真实数据**，不依赖换手增量 ──
        # 顺序很重要：必须先算分钟成交额，再判断 dto。
        # 否则「换手未增」会提前跳过，白白错过可得的精确数据
        # （横店影视 10:30 封 10:35 炸板，换手几乎没动，但那一分钟成交 8.99 亿）。
        ts_before = tmap[i_prev][0]
        ts_after = tmap[min(k, len(tmap) - 1)][0]
        amt_round = None
        exact = False
        mf_reason = None
        try:
            from backend import minute_flow as _mf
            mr = _mf.amount_between(c, day, ts_before, ts_after)
            if mr.get("ok"):
                amt_round = float(mr["amount"])
                exact = True
            else:
                mf_reason = mr.get("reason")
        except Exception as e:  # noqa: BLE001
            mf_reason = type(e).__name__
        if amt_round is None and dto > 0:
            amt_round = amt_day * (dto / total_to)   # 回退：换手差分近似
        if amt_round is None:
            rounds.append({"round": len(rounds) + 1, "ts": tmap[i_open][0],
                           "why": open_why, "dto": round(max(dto, 0.0), 3),
                           "amount_yi": None, "ratio": None,
                           "wait_consumed_yi": round(wait_consumed / 1e8, 3),
                           "queue_yi": round(s_now / 1e8, 3), "filled": False,
                           "amount_src": "不可得",
                           "note": (f"换手未增且分钟成交额不可得（{mf_reason}）"
                                    "→ 本轮消耗无法计算")})
            s_entry = s_now
            i_prev = k if k < len(tmap) else len(tmap) - 1
            continue

        ratio = amt_round / s_now if s_now > 0 else float("inf")
        base_round = {
            "round": len(rounds) + 1, "ts": tmap[i_open][0], "why": open_why,
            "dto": round(dto, 3),
            "amount_yi": round(amt_round / 1e8, 3),
            "ratio": round(ratio, 3) if ratio != float("inf") else None,
            "wait_consumed_yi": round(wait_consumed / 1e8, 3),
            "queue_yi": round(s_now / 1e8, 3),
            "approx": approx,
            "exact_amount": exact,
            "amount_src": ("分钟级真实成交额"
                            if exact else "换手差分近似"),
        }
        if ratio >= RATIO_FILL:
            rounds.append({**base_round, "filled": True,
                           "why2": "本轮成交额 ≥ 开板前队列，被吃穿"})
            conf = "strong" if ratio >= RATIO_STRONG else "edge"
            return {
                "status": "filled", "fill_ts": tmap[i_open][0],
                "fill_round": len(rounds),
                "fill_price": entry_price or entry.get("price"),
                "confidence": conf,
                "rounds": rounds,
                "seal_fund_at_entry_yi": round(S / 1e8, 3),
                "queue_before_open_yi": round(s_now / 1e8, 3),
                "wait_consumed_yi": round(wait_consumed / 1e8, 3),
                "day_amount_yi": round(amt_day / 1e8, 3),
                "detail": (f"第 {len(rounds)} 轮（{tmap[i_open][0]}，{open_why}）"
                           f"成交额 {amt_round/1e8:.2f} 亿 ≥ 开板前队列 "
                           f"{s_now/1e8:.2f} 亿（消耗比 {ratio:.2f}；"
                           f"排队期间已消耗 {wait_consumed/1e8:.2f} 亿）→ 排到"
                           + ("（边缘）" if conf == "edge" else "")),
            }

        # 未吃穿：队列继续前移
        s_entry = s_now * (1.0 - ratio)
        rounds.append({**base_round, "filled": False,
                       "queue_left_yi": round(s_entry / 1e8, 3),
                       "why2": "成交额 < 开板前队列，队列前移继续排"})
        i_prev = k if k < len(tmap) else len(tmap) - 1

    # 2026-11-20 修正（用户质疑「板了之后也会有交易，不开板≠买不进」）：
    # 旧实现在 rounds 为空时直接断言「全天未开板 → 队列不可能被吃穿」，
    # 这是 Q037（东财模拟组合简化规则）口径，不是真实交易真相。
    # 真实涨停板上一直有成交：卖单打在涨停价，封单持续被吃掉。
    # 真视通 11-18 实测：封单 1.58亿→1.41亿（减少 920 万）、换手 7.11%→7.81%，
    # 说明涨停价上一直有交易。能否成交应看「封单是否被吃穿」，不是「有没有开板」。
    # 补一个「封板期间净消耗」检查：挂单封单 S → 最后一帧封单 S_end。
    last_row = tmap[i_prev][1].get(c) if i_prev < len(tmap) else None
    s_end_raw = (last_row or {}).get("seal_fund")
    s_end = float(s_end_raw) if isinstance(s_end_raw, (int, float)) else s_entry
    net_consumed = max(0.0, (S or 0.0) - s_end)
    ratio = net_consumed / S if S and S > 0 else 0.0
    if not rounds and ratio >= RATIO_FILL:
        conf = "edge" if ratio < RATIO_STRONG else "strong"
        return {
            "status": "filled", "fill_ts": None, "fill_round": None,
            "fill_price": entry_price or entry.get("price"),
            "confidence": conf, "rounds": rounds,
            "queue_left_yi": round(max(0.0, (S or 0) - net_consumed) / 1e8, 3),
            "seal_fund_at_entry_yi": round((S or 0) / 1e8, 3),
            "seal_consumed_yi": round(net_consumed / 1e8, 3),
            "day_amount_yi": round(amt_day / 1e8, 3),
            "detail": (f"全天未开板，但封单从 {(S or 0)/1e8:.2f} 亿被吃到 "
                       f"{s_end/1e8:.2f} 亿（净消耗 {net_consumed/1e8:.2f} 亿，"
                       f"消耗比 {ratio:.2f}）→ 涨停价上有真实成交，前面队列被吃穿，"
                       f"排板大概率成交（无逐笔，时点为近似）"),
        }
    return {
        "status": "queued_unfilled",
        "fill_ts": None, "fill_round": None, "confidence": "none",
        "rounds": rounds,
        "queue_left_yi": round(s_end / 1e8, 3),
        "seal_fund_at_entry_yi": round((S or 0) / 1e8, 3),
        "seal_consumed_yi": round(net_consumed / 1e8, 3),
        "day_amount_yi": round(amt_day / 1e8, 3),
        "detail": (f"共 {len(rounds)} 轮开板，累计只消耗部分队列，"
                   f"收盘仍剩 {s_end/1e8:.2f} 亿排在前面 → 未排到"
                   if rounds else
                   f"全天未开板，封单从 {(S or 0)/1e8:.2f} 亿仅消耗 "
                   f"{net_consumed/1e8:.2f} 亿（消耗比 {ratio:.2f}），"
                   f"收盘仍剩 {s_end/1e8:.2f} 亿排队买单 → 涨停价虽有成交，"
                   f"但没吃穿前面的封单，排板未成交"),
    }


