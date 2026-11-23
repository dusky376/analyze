"""悟空真经 B1 · 龟兔角色识别（用户 2026-11-13 裁定）

> 「悟空·二板的核心就是二板，乌龟和兔子」

B1 = **二板**（streak 1→2）+ **同题材同身位的龟兔结构**：

* 兔子 = 一字板：开盘即封、全天不开板、低换手 → 按东财 Q037 排板**买不进**；
* 乌龟 = 厂字 / 换手板：盘中开板或换手充分 → 排板**能上车**；
* 二者构成同题材同身位对照，乌龟是 B1 唯一可能成交的一侧。

严格边界（不可协商）
------------------------------------------------
1. 本模块只做**标注与研究对象**，**不做买入过滤、不改买卖点、不调任何阈值**。
   当前选股口径（用户 2026-11-16 裁定）由 `live_sim.collect_guitu_targets` 负责：
   「题材内有一字兔 → 买同题材其他二板；无组合则不开仓」。兔子在这里是**选股锚点**；
   而本模块的角色标注**不参与**成交判定——挂单后能否成交仍由
   `queue_match.classify_live` 按 Q037 独立判定：兔子一字买不进，成交的自然是乌龟。
2. 「一字 / 厂字」本质是**分时形态**。仓库只有涨停池快照（首次/末次封板时刻、
   开板次数、换手率、封单额）与日线 OHLC，**没有逐笔 / 分时 / Level-2**。
   本模块给的是基于真实字段的**近似代理**，不是分时形态判定。
3. 用户案例里的「盘子偏大」(heavy) 需要流通市值，留档帧无此字段 →
   明确登记为**当前数据源不可判定**，不用价格或封单额臆造。
   案例库中用户手工标注的 heavy 仍以 `guitu_cases.PAIR_GROUPS` 为准。
4. 「题材」用涨停池 `industry` 字段近似（不是严格题材分类），「同身位」由
   B1 只收 `streak == 2` 保证。行业缺失时不强行配对。
5. 角色按**当日全帧累计**判定：`broken` 取全天最大值、`first_seal` 取首帧、
   `turnover` 取末帧。盘中角色可能由兔子转乌龟（一字开板），链路上会留痕。

判据（全部来自真实字段，阈值仅用于描述性分箱，不是策略参数）
------------------------------------------------
* `broken > 0`                    → turtle（开过板，必非一字）
* `turnover >= 8%`                → turtle（换手充分，厂字/换手板倾向）
* `first_seal > 09:30:00`         → turtle（非开盘即封，盘中回封）
* `broken == 0` 且首封不晚于开盘
  且 `turnover < 5%`              → rabbit（一字倾向，买不进）
* 其余 / 关键字段缺失              → unknown（不猜、不补）
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

# 与 guitu_cases.ROLE_CN 保持一致；测试里有断言，防止两处口径分叉。
ROLE_CN: dict[str, str] = {
    "rabbit": "一字兔子",
    "turtle": "厂字乌龟",
    "heavy": "盘子偏大",
    "unknown": "不可判定",
}

OPEN_SEAL_CUTOFF = 93000        # 09:30:00，HHMMSS
RABBIT_MAX_TURNOVER = 5.0      # 低于此换手才可能是一字
TURTLE_MIN_TURNOVER = 8.0      # 高于此换手视为换手充分

HEAVY_NOTE = ("「盘子偏大」需流通市值，涨停池留档帧无该字段，"
              "当前数据源不可判定；案例库中的 heavy 为用户手工标注。")

PROXY_NOTE = ("一字/厂字是分时形态，本判定只用涨停池快照字段（首封/末封时刻、开板次数、"
              "换手、封单）做近似代理，无逐笔/分时/Level-2，不等于真实分时形态。")


def _hhmmss(v: Any) -> int | None:
    """东帧首封时刻可能是 92500 / '92500' / '09:25:00' / '925'。"""
    if v is None:
        return None
    s = str(v).strip().replace(":", "")
    if not s or not s.isdigit():
        return None
    n = int(s)
    # 允许 HMMSS / HHMMSS；补零到 6 位比较
    if n < 10000:                    # 925 -> 092500
        n = n * 100
    return n if 0 <= n <= 235959 else None


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def classify_role(row: dict[str, Any]) -> dict[str, Any]:
    """由一条真实涨停池行判定龟兔角色（纯函数）。

    `row` 需含 `broken` / `first_seal` / `last_seal` / `turnover` / `seal_fund`。
    缺失字段一律不推断，宁可返回 unknown。
    """
    broken = _int(row.get("broken"))
    first = _hhmmss(row.get("first_seal"))
    last = _hhmmss(row.get("last_seal"))
    turnover = _num(row.get("turnover"))
    seal = _num(row.get("seal_fund"))

    # 老留档帧（2026-11-13 中午前）没有 first_seal 列。末封时刻同样是真实字段：
    # 若末封 = 09:25 且当日 0 次开板，说明封板后从未打开，可替代首封作一字证据。
    # 反过来若末封在盘中，也如实判为换手/回封，不会误判成兔子。
    seal_source = "first_seal"
    if first is None and last is not None:
        first = last
        seal_source = "last_seal（首封列缺失，以真实末封时刻替代）"

    evidence: dict[str, Any] = {
        "broken": broken,
        "first_seal": row.get("first_seal"),
        "last_seal": row.get("last_seal"),
        "seal_source": seal_source,
        "turnover": round(turnover, 2) if turnover is not None else None,
        "seal_fund_yi": round(seal / 1e8, 3) if seal is not None else None,
    }
    base: dict[str, Any] = {
        "evidence": evidence,
        "proxy_note": PROXY_NOTE,
        "heavy_note": HEAVY_NOTE,
        "role_basis": "当日全帧累计：broken 取最大值、首封取首帧、换手/封单取末帧",
        "is_filter": False,
        "filter_note": "角色只作标注与分组统计，不参与开仓过滤；成交由 Q037 独立判定",
    }

    if broken is None and first is None and turnover is None:
        return {**base, "role": "unknown", "role_cn": ROLE_CN["unknown"],
                "reason": "开板次数、首封时刻、换手率三项均缺失，无法判定，不猜测"}

    # 乌龟：开过板 / 换手充分 / 非开盘即封
    if broken is not None and broken > 0:
        return {**base, "role": "turtle", "role_cn": ROLE_CN["turtle"],
                "reason": f"当日开板 {broken} 次，必非一字；换手充分、排板可上车"}
    if turnover is not None and turnover >= TURTLE_MIN_TURNOVER:
        return {**base, "role": "turtle", "role_cn": ROLE_CN["turtle"],
                "reason": f"换手 {turnover:.1f}% ≥ {TURTLE_MIN_TURNOVER:.0f}%，换手充分"}
    if first is not None and first > OPEN_SEAL_CUTOFF:
        return {**base, "role": "turtle", "role_cn": ROLE_CN["turtle"],
                "reason": f"封板时刻 {first:06d} 晚于 09:30（{seal_source}），属盘中回封/换手板"}

    # 兔子：全天未开板 + 开盘即封 + 低换手
    first_ok = first is not None and first <= OPEN_SEAL_CUTOFF
    low_turnover = turnover is not None and turnover < RABBIT_MAX_TURNOVER
    if broken == 0 and first_ok and low_turnover:
        return {**base, "role": "rabbit", "role_cn": ROLE_CN["rabbit"],
                "reason": (f"{first:06d} 开盘即封（{seal_source}）、全天 {broken} 次开板、"
                           f"换手 {turnover:.1f}%，一字倾向 → Q037 下排板买不进")}

    return {**base, "role": "unknown", "role_cn": ROLE_CN["unknown"],
            "reason": ("证据不足以区分一字/厂字："
                       f"broken={broken}、首封={first}、换手={turnover}；不强行归类")}


def pair_groups(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """按 `(day, industry)` 聚合二板委托单，找出同日同题材同身位的龟兔配对。

    只有组内**同时存在 rabbit 与 turtle** 才算真配对；行业缺失不强行凑对。
    返回纯描述性结果，不产生任何信号。
    """
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for o in orders:
        buckets[(str(o.get("day") or ""), str(o.get("industry") or ""))].append(o)

    pairs: list[dict[str, Any]] = []
    unpaired: list[dict[str, Any]] = []
    for (day, industry), rows in sorted(buckets.items()):
        def brief(o: dict[str, Any]) -> dict[str, Any]:
            g = o.get("guitu") or {}
            return {"ts_code": o.get("ts_code"), "name": o.get("name"),
                    "role": g.get("role"), "role_cn": g.get("role_cn"),
                    "status": o.get("status"), "reason": g.get("reason")}

        rabbits = [brief(o) for o in rows if (o.get("guitu") or {}).get("role") == "rabbit"]
        turtles = [brief(o) for o in rows if (o.get("guitu") or {}).get("role") == "turtle"]
        rec: dict[str, Any] = {
            "day": day, "industry": industry or "（行业缺失）",
            "n": len(rows), "rabbits": rabbits, "turtles": turtles,
            "is_pair": bool(rabbits and turtles and industry),
            "note": "同身位由 B1 只收 streak==2 保证；题材用涨停池 industry 近似",
        }
        if not industry:
            rec["unpaired_reason"] = "行业字段缺失，不强行按题材配对"
        elif not (rabbits and turtles):
            rec["unpaired_reason"] = ("组内未同时出现兔子与乌龟"
                                      "（真实市场并非每天都有龟兔配对，不硬凑）")
        (pairs if rec["is_pair"] else unpaired).append(rec)

    return {
        "n_pairs": len(pairs),
        "n_unpaired_groups": len(unpaired),
        "pairs": pairs,
        "unpaired": unpaired,
        "note": ("龟兔配对是用户命题「有连板行情 + 持续性题材 → 龟兔最适合操作」"
                 "的观察样本，不是交易信号；用户此前给的 7 组配对属事后选中成功案例，"
                 "本模块按真实当日全量二板自动识别，无对照组时如实记为未配对。"),
    }


def summarize_roles(orders: list[dict[str, Any]],
                    trades: list[dict[str, Any]]) -> dict[str, Any]:
    """按角色分组的描述性统计（纯函数，不做显著性检验、不做因果解释）。"""
    out: dict[str, Any] = {}
    for role in ("rabbit", "turtle", "unknown", "heavy"):
        os_ = [o for o in orders if (o.get("guitu") or {}).get("role", "unknown") == role]
        ts = [t for t in trades
              if (t.get("guitu") or {}).get("role", "unknown") == role
              and isinstance(t.get("gross_return"), (int, float))]
        vals = [float(t["gross_return"]) for t in ts]
        out[role] = {
            "role_cn": ROLE_CN[role],
            "n_orders": len(os_),
            "n_filled": sum(1 for o in os_ if o.get("status") == "filled"),
            "n_unfilled": sum(1 for o in os_ if o.get("status") == "queued_unfilled"),
            "n_unknown": sum(1 for o in os_ if o.get("status") == "unknown"),
            "n_closed": len(ts),
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 4) if vals else None,
            "gross_mean": round(sum(vals) / len(vals), 6) if vals else None,
            "note": ("兔子按 Q037 买不进，成交应集中在乌龟；"
                     "该差异是执行约束造成的机械结果，不是选股 alpha。"),
        }
    return out


CORE_DEFINITION = {
    "strategy": "B1 悟空真经·二板排板",
    "core": "二板 + 乌龟和兔子",
    "board": "只收 streak 1→2（二板）；4 板+ 对照另立为独立研究对象，不并入 B1",
    "rabbit": "一字兔子：开盘即封、全天不开板、低换手；Q037 下排板确定买不进，是同题材参照物",
    "turtle": "厂字乌龟：盘中开板/换手充分；B1 排板唯一可能成交的一侧",
    "execution": "二板当日涨停瞬间挂涨停价排板 → 首个未涨停日（断板）当日收盘卖 → 断板后不持有",
    "selection_rule": ("选股口径（用户 2026-11-16 裁定）：题材内有一字二板（兔子）→ "
                       "买同题材其他二板（乌龟）；无组合则当日不开仓。由 live_sim.collect_guitu_targets 执行。"),
    "no_new_filter": ("本次落地角色标注本身没有新增「只买乌龟」之类买卖过滤："
                      "兔子是选股锚点而非买入标的（选股口径见 selection_rule）；"
                      "挂单后兔子买不进是 Q037 的执行结果，角色不参与成交判定。"),
    "research_status": ("「有连板行情 + 持续性题材 → 龟兔最适合操作」仍登记为待检验假设"
                        "（B7），不进开仓规则；本模块的角色/配对只作研究对象与逐周核对。"),
}
