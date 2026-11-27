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

#: 排板主动撤单的**兜底参考值**（仅在缺少滚动市场分布时作先验锚点，实际运行时由
#: `adaptive_cancel_thresholds` 随市场量能、近期二板换手/封单分位、高标情绪与蚂蚁因子反馈动态计算）。
#: 注意：不做“排队久/14:30 一刀切撤单”——兔子与乌龟都封得很硬时，往往下午尾盘才能自然排进。
CANCEL_BLEED_RATIO = 0.50            # 常态先验：封单相对峰值失血
CANCEL_WITHDRAW_RATIO = 0.30         # 常态先验：推断主力主动撤单占峰值封单比
CANCEL_MIN_SEAL_YUAN = 20_000_000.0  # 常态先验：薄封底线
CANCEL_SEAL_TO_AMT_RATIO = 0.10      # 常态先验：封单/当日成交额比
CANCEL_MAX_BROKEN = 3                # 常态先验：烂板开板次数上限


def adaptive_cancel_thresholds(
    *,
    code: str | None = None,
    market_vol_ratio: float | None = None,
    ice_level: str | None = None,
    market_drag_sensitive: bool | None = None,
    rabbit_weak: bool = False,
    hist_turnovers: list[float] | None = None,
    hist_seals_yuan: list[float] | None = None,
    hist_broken_counts: list[int] | None = None,
    evo_high_turn_delta: float | None = None,
    evo_rotten_delta: float | None = None,
    cancel_feedback: dict[str, float] | None = None,
) -> dict[str, Any]:
    """随市场环境、近期二板滚动分布与蚂蚁因子反馈动态计算撤单阈值（绝不写死静态常数）。

    自适应机制：
      1. 市场量能比（vol_ratio）与历史封单 P25 共同决定薄封底线（缩量市放低、放量市抬高）；
      2. 近期成功二板的换手率滚动 P85 + 蚂蚁高换手因子反馈共同决定换手警戒线；
      3. 近期二板开板分布 + 情绪/蚂蚁烂板反馈共同决定开板上限（强势分歧期放宽、退潮期收紧）；
      4. 兔子走弱不写死一票撤单（保护卡位龙），仅按市场传导敏感度动态收紧乌龟自身失血门槛。
    """
    c = str(code or "").zfill(6)
    is_20 = c.startswith(("300", "301", "302", "688", "689", "43", "83", "87", "92"))
    vr = max(0.50, min(2.00, float(market_vol_ratio))) if (market_vol_ratio and market_vol_ratio > 0) else 1.0

    # 情绪松紧系数：冰点/高标拖累收紧，强势容错放宽
    mood = 1.0
    if ice_level == "L5":
        mood *= 0.78
    elif ice_level == "L4":
        mood *= 0.88
    if market_drag_sensitive is True:
        mood *= 0.85
    elif market_drag_sensitive is False:
        mood *= 1.12

    # 1) 封单失血 & 主力撤单阈值（随情绪 + 兔子联动 + 撤单反事实反馈动态调整，防被主力假撤单洗盘吓跑）
    fb = cancel_feedback or {}
    bleed_th = CANCEL_BLEED_RATIO * mood * (1.0 + float(fb.get("seal_bleed") or 0.0))
    withdraw_th = CANCEL_WITHDRAW_RATIO * mood * (1.0 + float(fb.get("main_withdraw") or 0.0))
    if rabbit_weak:
        bleed_th *= (0.70 if market_drag_sensitive is True else 0.78) * (1.0 + float(fb.get("rabbit_drag_bleed") or 0.0))
        withdraw_th *= 0.85
    bleed_th = round(max(0.22, min(0.75, bleed_th)), 3)
    withdraw_th = round(max(0.18, min(0.55, withdraw_th)), 3)

    # 2) 薄封底线：优先取近期二板封单分布 P25，否则按市场量能比缩放
    valid_seals = sorted(float(x) for x in (hist_seals_yuan or []) if x is not None and float(x) > 0)
    if len(valid_seals) >= 5:
        p25_seal = valid_seals[max(0, int(0.25 * len(valid_seals)) - 1)]
        min_seal = p25_seal * (0.85 if mood > 1.0 else 1.0)
        seal_src = f"近期封单P25({p25_seal / 1e8:.2f}亿)"
    else:
        min_seal = CANCEL_MIN_SEAL_YUAN * vr
        seal_src = f"量能缩放(×{vr:.2f})"
    min_seal = round(max(8_000_000.0, min(60_000_000.0, min_seal)), 2)
    seal_to_amt = round(max(0.05, min(0.18, CANCEL_SEAL_TO_AMT_RATIO / max(0.75, mood))), 3)

    # 3) 换手警戒线：优先取近期二板换手 P85，再结合市场量能与蚂蚁高换手因子反馈微调
    base_to = 30.0 if is_20 else 20.0
    to_lo, to_hi = (22.0, 50.0) if is_20 else (14.0, 36.0)
    valid_to = sorted(float(x) for x in (hist_turnovers or []) if x is not None and float(x) > 0)
    if len(valid_to) >= 5:
        p85_to = valid_to[min(len(valid_to) - 1, int(0.85 * len(valid_to)))]
        max_to = p85_to
        to_src = f"近期换手P85({p85_to:.1f}%)"
    else:
        max_to = base_to * (0.85 + 0.15 * vr) * (0.90 + 0.10 * mood)
        to_src = f"量能情绪缩放"
    if evo_high_turn_delta is not None:
        # 蚂蚁高换手因子赚钱 → 放宽换手上限；亏钱 → 收紧
        max_to *= (1.0 + max(-0.20, min(0.25, float(evo_high_turn_delta) * 0.8)))
    max_to = round(max(to_lo, min(to_hi, max_to)), 1)

    # 4) 烂板开板上限：优先取近期封住样本开板分布 P90，结合情绪与蚂蚁烂板反馈动态浮动
    valid_br = sorted(int(x) for x in (hist_broken_counts or []) if x is not None and int(x) >= 0)
    if len(valid_br) >= 5:
        base_br = float(valid_br[min(len(valid_br) - 1, int(0.90 * len(valid_br)))])
        br_src = f"近期开板P90({base_br:.0f}次)"
    else:
        base_br = float(CANCEL_MAX_BROKEN)
        br_src = "情绪自适应"
    adj_br = base_br * mood
    if evo_rotten_delta is not None:
        adj_br += max(-1.0, min(1.5, float(evo_rotten_delta) * 4.0))
    max_broken = max(2, min(6, int(round(adj_br))))

    return {
        "bleed_ratio": bleed_th,
        "withdraw_ratio": withdraw_th,
        "min_seal_yuan": min_seal,
        "seal_to_amt_ratio": seal_to_amt,
        "max_turnover": max_to,
        "max_broken": max_broken,
        "mood_factor": round(mood, 3),
        "basis": f"薄封={seal_src} · 换手={to_src} · 开板={br_src}",
    }


def _ts_hhmm(ts: str | None) -> str:
    s = str(ts or "").strip()
    if "T" in s:
        s = s.split("T", 1)[1]
    elif " " in s:
        s = s.split(" ", 1)[1]
    return s[:5] if len(s) >= 5 and s[2] == ":" else ""


def _to_sec(ts: Any) -> int | None:
    """将 HHMMSS / HH:MM:SS / ISO 时间戳统一转为当日秒数。"""
    if ts is None:
        return None
    s = str(ts).strip()
    if "T" in s:
        s = s.split("T", 1)[1][:8]
    elif " " in s:
        s = s.split(" ", 1)[1][:8]
    digits = s.replace(":", "")
    if len(digits) == 5 and digits.isdigit():
        digits = "0" + digits
    if len(digits) >= 6 and digits[:6].isdigit():
        hh, mm, ss = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
        if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
            return hh * 3600 + mm * 60 + ss
    return None


def estimate_first_seal_queue(
    seal_at_entry: float | None,
    first_seal: Any,
    entry_ts: str | None,
) -> dict[str, Any]:
    """按首封时刻与采样帧的滞后秒数估算首封参考队列（仅作只读诊断对照，严禁向下折算实际排队 S 违背时间优先）。"""
    s_raw = float(seal_at_entry) if (seal_at_entry is not None and float(seal_at_entry) > 0) else 0.0
    sec_fs = _to_sec(first_seal)
    sec_et = _to_sec(entry_ts)
    if sec_fs is None or sec_et is None or s_raw <= 0:
        return {
            "lag_sec": None,
            "discount": 1.0,
            "raw_queue_yuan": s_raw,
            "adj_queue_yuan": s_raw,
            "is_auction_seal": bool(sec_fs is not None and sec_fs <= 9 * 3600 + 25 * 60),
        }
    lag = max(0, sec_et - sec_fs)
    is_auction = sec_fs <= 9 * 3600 + 25 * 60
    if is_auction or lag <= 5:
        disc = 1.0
    else:
        disc = max(0.20, 1.0 / (1.0 + min(lag, 180) / 45.0))
    return {
        "lag_sec": lag,
        "discount": round(disc, 3),
        "raw_queue_yuan": round(s_raw, 2),
        "adj_queue_yuan": round(s_raw * disc, 2),
        "is_auction_seal": is_auction,
    }


FILL_CATEGORY_CN: dict[str, str] = {
    "queue_head": "队首成交",
    "reopen_reseal": "开板后回封",
    "reopen_broken": "开板后没回封",
    "sweep_shadow": "扫板影子",
    "unfilled": "排板未成交",
    "cancelled": "主动撤单",
}


def classify_fill_category(
    order: dict[str, Any],
    *,
    closed_sealed: bool | None = None,
) -> dict[str, str]:
    """将单笔委托归入四类成交记账口径（队首成交 / 开板后回封 / 开板后没回封 / 扫板影子）。"""
    if str(order.get("exec_mode") or "") == "sweep_shadow":
        return {"category": "sweep_shadow", "category_cn": FILL_CATEGORY_CN["sweep_shadow"]}
    st = str(order.get("status") or "")
    if st == "cancelled":
        return {"category": "cancelled", "category_cn": FILL_CATEGORY_CN["cancelled"]}
    if st != "filled":
        return {"category": "unfilled", "category_cn": FILL_CATEGORY_CN["unfilled"]}

    qs = order.get("queue_sim") or {}
    bm = order.get("broken_max")
    bm_i = int(bm) if isinstance(bm, (int, float)) else None
    fill_round = qs.get("fill_round")

    if closed_sealed is False:
        return {"category": "reopen_broken", "category_cn": FILL_CATEGORY_CN["reopen_broken"]}
    if (fill_round is None and (bm_i is None or bm_i == 0)) or bm_i == 0:
        return {"category": "queue_head", "category_cn": FILL_CATEGORY_CN["queue_head"]}
    if closed_sealed is True or (bm_i is not None and bm_i <= 2):
        return {"category": "reopen_reseal", "category_cn": FILL_CATEGORY_CN["reopen_reseal"]}
    return {"category": "reopen_broken", "category_cn": FILL_CATEGORY_CN["reopen_broken"]}


def evaluate_cancel_trigger(
    *,
    seal_fund: float | None,
    peak_seal: float | None,
    prev_seal: float | None = None,
    interval_traded_amt: float | None = None,
    day_amount: float | None = None,
    broken: int | None = None,
    ts: str | None = None,
    rabbit_broken: bool = False,
    rabbit_bleed: bool = False,
    rabbit_holding: bool | None = None,
    float_mv: float | None = None,
    market_zt_count: int | None = None,
    market_drag_sensitive: bool | None = None,
    turnover: float | None = None,
    code: str | None = None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """评估当前时刻是否触发排板买单主动撤单（纯函数，全阈值随市场自适应，无未来函数）。

    核心原则：
    1. 区分「正常成交消耗」与「主力主动撤单」：
           inferred_withdraw = max(0, (prev_seal - seal_fund) - interval_traded_amt)
    2. 兔子走弱不写死一票撤单（保护「换手乌龟卡位上位」）：
       兔子炸板/失血时，若乌龟自身封单稳健（甚至加厚），说明资金在做卡位切换，继续排队；
       仅当「兔子走弱 + 乌龟自身也同步失血」时才按市场联动敏感度动态收紧阈值触发撤单。
    3. 所有阈值（失血/主力撤单/薄封/换手/开板）均由 `adaptive_cancel_thresholds` 随市场动态计算。
    """
    sf = float(seal_fund) if seal_fund is not None else None
    pk = float(peak_seal) if (peak_seal is not None and peak_seal > 0) else sf
    pv = float(prev_seal) if prev_seal is not None else None
    vt = max(0.0, float(interval_traded_amt)) if interval_traded_amt is not None else None

    bleed_ratio = ((pk - sf) / pk) if (pk and pk > 0 and sf is not None) else 0.0
    inferred_withdraw = 0.0
    if pv is not None and sf is not None and pv > sf and vt is not None:
        inferred_withdraw = max(0.0, (pv - sf) - vt)
    withdraw_ratio = (inferred_withdraw / pk) if (pk and pk > 0) else 0.0

    rabbit_weak = bool(rabbit_broken or rabbit_bleed)
    th = thresholds or adaptive_cancel_thresholds(
        code=code,
        market_drag_sensitive=market_drag_sensitive,
        rabbit_weak=rabbit_weak,
    )
    eff_bleed_th = float(th["bleed_ratio"])
    eff_withdraw_th = float(th["withdraw_ratio"])
    eff_min_seal = float(th["min_seal_yuan"])
    eff_seal_to_amt = float(th["seal_to_amt_ratio"])
    eff_max_to = float(th["max_turnover"])
    eff_max_broken = int(th["max_broken"])

    # 早盘缩量厚封防假撤单洗盘保护（09:30~09:50）：
    # 同期几乎无实际抛压成交（vt ≤ 3% 峰值封单）且剩余封单仍厚（≥ max(5000万, 2.5×薄封线)）时，
    # 属于典型主力盘口假撤单洗盘，抬高撤单/失血门槛至 65% 防被吓跑（如 002298 中电鑫龙）
    hhmm = _ts_hhmm(ts)
    # A 股 14:57–15:00 收盘集合竞价期间交易所已不可撤单，且五档盘口切换易误读为 0 封单失血
    if hhmm >= "14:57":
        return {
            "should_cancel": False,
            "kind": None,
            "bleed_ratio": round(bleed_ratio, 4),
            "inferred_withdraw_yi": round(inferred_withdraw / 1e8, 3),
            "thresholds": th,
            "reason": f"{hhmm} 已进入收盘集合竞价不可撤单窗口，禁止触发盘中撤单",
        }
    anti_washout = bool(
        "09:30" <= hhmm <= "09:50"
        and sf is not None and sf >= max(50_000_000.0, eff_min_seal * 2.5)
        and vt is not None and pk and pk > 0 and vt <= 0.03 * pk
    )
    # 封流比 + 兔子稳固度 + 市场热度联合护栏（防止 002298 剩 5600万厚封且兔子稳封时被 43% 回撤洗飞）
    seal_to_fmv = (sf / float(float_mv)) if (sf is not None and float_mv and float(float_mv) > 0) else None
    seal_to_amt_cur = (sf / float(day_amount)) if (sf is not None and day_amount and float(day_amount) > 0) else None
    rabbit_is_holding = (not rabbit_weak) if rabbit_holding is None else bool(rabbit_holding and not rabbit_weak)
    thick_seal_ok = bool(
        sf is not None
        and (
            sf >= max(50_000_000.0, eff_min_seal * 2.5)
            or (seal_to_fmv is not None and seal_to_fmv >= 0.015)
        )
        and (seal_to_amt_cur is None or seal_to_amt_cur >= eff_seal_to_amt)
    )
    thick_seal_shield = bool(rabbit_is_holding and thick_seal_ok)
    eff_withdraw_guard = eff_withdraw_th
    eff_bleed_guard = eff_bleed_th
    if anti_washout:
        eff_withdraw_guard = max(eff_withdraw_th, 0.65)
        eff_bleed_guard = max(eff_bleed_th, 0.65)
    elif thick_seal_shield:
        hot_mkt = bool(market_zt_count is not None and int(market_zt_count) >= 80)
        early_morning = bool("09:30" <= hhmm <= "09:55")
        shield_floor = 0.65 if (hot_mkt or early_morning) else 0.55
        eff_withdraw_guard = max(eff_withdraw_th, shield_floor)
        eff_bleed_guard = max(eff_bleed_th, max(0.60, shield_floor))

    # 绝对巨封豁免硬规则：未突破烂板上限且当前板上绝对封单 ≥ 1.5 亿（或 ≥ 8000 万且封流比 ≥ 2.5%），
    # 无论盘中天量峰值回撤多少，严禁触发主力撤单 / 封单失血误撤（防洗飞真龙中军如 002261 拓维信息）
    mega_seal_exempt = bool(
        sf is not None
        and (
            sf >= 150_000_000.0
            or (sf >= 80_000_000.0 and seal_to_fmv is not None and seal_to_fmv >= 0.025)
        )
        and (broken is None or int(broken) < eff_max_broken)
    )

    # 1) 主力主动撤单（最高优先级：没怎么成交但封单骤降）
    if not mega_seal_exempt and pk and pk > 0 and withdraw_ratio >= eff_withdraw_guard:
        return {
            "should_cancel": True,
            "kind": "main_withdraw",
            "bleed_ratio": round(bleed_ratio, 4),
            "inferred_withdraw_yi": round(inferred_withdraw / 1e8, 3),
            "thresholds": th,
            "reason": (
                f"推断主力主动撤单 {inferred_withdraw / 1e8:.2f} 亿"
                f"（占峰值封单 {withdraw_ratio:.0%} ≥ 动态阈值 {eff_withdraw_guard:.0%}，"
                f"封单 {pv / 1e8:.2f}→{sf / 1e8:.2f} 亿，同期仅成交 {(vt or 0) / 1e8:.2f} 亿）"
            ),
        }

    # 2) 封单相对失血（兔子走弱时仅当乌龟自身也同步失血触及动态门槛才撤，封单稳健则按卡位继续排队）
    if not mega_seal_exempt and pk and pk > 0 and sf is not None and bleed_ratio >= eff_bleed_guard:
        if rabbit_weak and bleed_ratio < CANCEL_BLEED_RATIO:
            why_r = "兔子炸板" if rabbit_broken else "兔子封单失血"
            return {
                "should_cancel": True,
                "kind": "rabbit_drag_bleed",
                "bleed_ratio": round(bleed_ratio, 4),
                "inferred_withdraw_yi": round(inferred_withdraw / 1e8, 3),
                "thresholds": th,
                "reason": (
                    f"{why_r}且乌龟自身同步失血 -{bleed_ratio:.0%}"
                    f"（峰值 {pk / 1e8:.2f}→{sf / 1e8:.2f} 亿 ≥ 动态联动阈值 {eff_bleed_th:.0%}）→ 确认受拖累撤单"
                ),
            }
        return {
            "should_cancel": True,
            "kind": "seal_bleed",
            "bleed_ratio": round(bleed_ratio, 4),
            "inferred_withdraw_yi": round(inferred_withdraw / 1e8, 3),
            "thresholds": th,
            "reason": (
                f"封单失血 -{bleed_ratio:.0%}（峰值 {pk / 1e8:.2f}→{sf / 1e8:.2f} 亿"
                f" ≥ 动态阈值 {eff_bleed_th:.0%}）→ 防炸板提前撤单"
            ),
        }

    # 3) 封单跌破动态薄封红线（原封单充足但滑落到动态底线以下）
    if pk and pk >= eff_min_seal and sf is not None:
        floor_yuan = max(
            eff_min_seal,
            float(day_amount) * eff_seal_to_amt if (day_amount and day_amount > 0) else 0.0,
        )
        if sf < floor_yuan:
            return {
                "should_cancel": True,
                "kind": "thin_seal",
                "bleed_ratio": round(bleed_ratio, 4),
                "inferred_withdraw_yi": round(inferred_withdraw / 1e8, 3),
                "thresholds": th,
                "reason": (
                    f"封单跌破动态薄封红线（当前 {sf / 1e8:.2f} 亿 < {floor_yuan / 1e8:.2f} 亿）→ 主动撤单"
                ),
            }

    # 4) 烂板恶化（开板次数 / 换手率按市场自适应阈值判定）
    if broken is not None and int(broken) >= eff_max_broken:
        return {
            "should_cancel": True,
            "kind": "rotten_broken",
            "bleed_ratio": round(bleed_ratio, 4),
            "inferred_withdraw_yi": round(inferred_withdraw / 1e8, 3),
            "thresholds": th,
            "reason": f"累计开板 {int(broken)} 次 ≥ 动态上限 {eff_max_broken} 次（烂板恶化）→ 放弃排队撤单",
        }
    if turnover is not None and float(turnover) > eff_max_to:
        return {
            "should_cancel": True,
            "kind": "high_turnover",
            "bleed_ratio": round(bleed_ratio, 4),
            "inferred_withdraw_yi": round(inferred_withdraw / 1e8, 3),
            "thresholds": th,
            "reason": f"换手率 {float(turnover):.1f}% 突破动态警戒线 {eff_max_to:.1f}% → 放弃排队撤单",
        }

    note_extra = "（兔子分歧但乌龟封单稳健，按卡位保护继续排队）" if rabbit_weak else ""
    return {
        "should_cancel": False,
        "kind": None,
        "bleed_ratio": round(bleed_ratio, 4),
        "inferred_withdraw_yi": round(inferred_withdraw / 1e8, 3),
        "thresholds": th,
        "reason": f"封单与锚定正常，继续排队{note_extra}",
    }


def check_live_quote_cancel(
    order: dict[str, Any],
    quote: dict[str, Any] | None,
    *,
    rabbit_quotes: dict[str, dict[str, Any]] | None = None,
    ts: str | None = None,
    thresholds: dict[str, Any] | None = None,
    market_vol_ratio: float | None = None,
    ice_level: str | None = None,
    cancel_feedback: dict[str, float] | None = None,
    evo_high_turn_delta: float | None = None,
    evo_rotten_delta: float | None = None,
) -> dict[str, Any]:
    """盘中高频盯盘：用实时五档 bid1_vol/ask1_vol 判定当前排队买单是否应撤单或已来不及。"""
    if not quote:
        return {"available": False, "should_cancel": False, "too_late": False, "reason": "无实时行情"}
    hhmm = _ts_hhmm(ts or str(quote.get("quote_time") or ""))
    if hhmm >= "14:57":
        return {
            "available": True,
            "should_cancel": False,
            "too_late": False,
            "reason": f"{hhmm} 收盘集合竞价不可撤单，忽略五档切换波动",
        }
    px = float(quote.get("price") or 0.0)
    entry_px = float(order.get("entry_price") or 0.0)
    bid1 = float(quote.get("bid1") or 0.0)
    bid1_vol = float(quote.get("bid1_vol") or 0.0)   # 手
    ask1_vol = float(quote.get("ask1_vol") or 0.0)
    # 已经开板（卖一有单或现价跌破涨停价）→ 来不及撤单（保守不作弊）
    if (entry_px > 0 and px > 0 and px < entry_px - 0.011) or ask1_vol > 0:
        return {
            "available": True,
            "should_cancel": False,
            "too_late": True,
            "live_seal_yi": round((bid1 * bid1_vol * 100.0) / 1e8, 3) if bid1 > 0 else 0.0,
            "reason": "盘口已开板（卖一有单或跌破涨停价），撤单已来不及",
        }
    live_seal = bid1 * bid1_vol * 100.0 if (bid1 > 0 and bid1_vol > 0) else 0.0
    prev_seal = float(order.get("live_last_seal") or order.get("seal_fund_at_entry") or (order.get("queue") or {}).get("seal_fund") or 0.0) or None
    peak_seal = max(
        live_seal,
        float(order.get("live_peak_seal") or 0.0),
        float(prev_seal or 0.0),
    ) or None

    rabbit_broken = False
    for rc, rq in (rabbit_quotes or {}).items():
        if not rq:
            continue
        if float(rq.get("ask1_vol") or 0.0) > 0:
            rabbit_broken = True
            break

    eff_th = thresholds or adaptive_cancel_thresholds(
        code=order.get("ts_code"),
        market_vol_ratio=market_vol_ratio,
        ice_level=ice_level,
        rabbit_weak=rabbit_broken,
        evo_high_turn_delta=evo_high_turn_delta,
        evo_rotten_delta=evo_rotten_delta,
        cancel_feedback=cancel_feedback,
    )
    from backend import rabbit_state as _rs
    fmv = _rs.estimate_float_mv_yuan(quote)
    ev = evaluate_cancel_trigger(
        seal_fund=live_seal,
        peak_seal=peak_seal,
        prev_seal=prev_seal,
        day_amount=float(quote.get("amount") or 0.0) or None,
        broken=order.get("broken_max"),
        ts=ts or str(quote.get("quote_time") or ""),
        rabbit_broken=rabbit_broken,
        rabbit_holding=not rabbit_broken,
        float_mv=fmv,
        turnover=quote.get("turnover"),
        code=order.get("ts_code"),
        thresholds=eff_th,
    )
    return {
        "available": True,
        "too_late": False,
        "live_seal_yuan": round(live_seal, 2),
        "live_seal_yi": round(live_seal / 1e8, 3),
        "peak_seal_yuan": round(peak_seal, 2) if peak_seal else None,
        "peak_seal_yi": round(peak_seal / 1e8, 3) if peak_seal else None,
        **ev,
    }


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------

def _frame_is_truncated_zt(frame: dict[str, Any] | None, n_rows: int) -> bool:
    """判定该帧涨停池是否发生单页 300 条硬截断（用于防止页码溢出被冤判炸板）。"""
    if not isinstance(frame, dict):
        return False
    zt_blk = frame.get("zt")
    if not isinstance(zt_blk, dict):
        return False
    if zt_blk.get("all_complete") is False:
        return True
    tc = zt_blk.get("tc")
    if n_rows == 300 and (tc is None or int(tc or 0) != 300):
        return True
    return False


def _zb_codes_in_frame(frame: dict[str, Any] | None) -> set[str]:
    if not isinstance(frame, dict):
        return set()
    zb_blk = frame.get("zb")
    if not isinstance(zb_blk, dict):
        return set()
    rows = zb_blk.get("all") or zb_blk.get("top") or []
    return {
        str(r.get("ts_code") or "").zfill(6)
        for r in rows
        if isinstance(r, dict) and r.get("ts_code")
    }


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
            vals["first_seal"] = r.get("first_seal")
            vals["last_seal"] = r.get("last_seal")
            vals["float_mv"] = r.get("float_mv")
            vals["amount"] = r.get("amount")
            m[c] = vals
        out.append((str(fr.get("ts") or ""), m))
    return out


def _boundary_adjusted_amount(
    code: str,
    day: str,
    t_from: str,
    t_to: str,
    *,
    first_seal: Any = None,
    last_seal: Any = None,
    limit_price: float | None = None,
    is_open_end: bool = False,
) -> dict[str, Any]:
    """计算区间成交额，并对水下回封/首封分钟与炸板分钟的跨分钟水下成交量打折，防止把水下扫货/砸盘当板上吃单。"""
    from backend import minute_flow as _mf

    mr = _mf.amount_between(code, day, t_from, t_to)
    if not mr.get("ok") or mr.get("amount") is None:
        return mr
    amt = max(0.0, float(mr["amount"]))
    discounted = False
    try:
        series = _mf.minute_series(code, day)
        if series and len(series) >= 2:
            # 构造每分钟增量金额、价格映射及有序 HHMM 序列
            ordered_hhmm: list[str] = []
            inc_map: dict[str, float] = {}
            px_map: dict[str, float] = {}
            prev_cum = 0.0
            for hhmm, px_v, cum_v in series:
                hs = str(hhmm)
                c_f = float(cum_v or 0.0)
                ordered_hhmm.append(hs)
                inc_map[hs] = max(0.0, c_f - prev_cum)
                try:
                    px_map[hs] = float(px_v) if px_v is not None else 0.0
                except (TypeError, ValueError):
                    px_map[hs] = 0.0
                prev_cum = c_f

            from_hhmm = mr.get("from_hhmm") or _mf._hhmm_of(t_from)
            to_hhmm = mr.get("to_hhmm") or _mf._hhmm_of(t_to)
            deducted_bars: set[str] = set()

            # 1) 分时价格校验：若起始分钟处于涨停价下方（水下回封/点火车程，如 0932 价 7.68 < 7.76），
            # 则紧邻下一分钟 bar（如 0933）包含大量水下扫货量，必须剔除挂单前与水下部分（如 002298）。
            lim_px = float(limit_price) if (limit_price is not None and float(limit_price) > 0) else None
            if from_hhmm and to_hhmm and lim_px is not None:
                from_px = px_map.get(from_hhmm)
                if from_px is not None and 0 < from_px < lim_px - 0.005:
                    next_bar = next(
                        (b for b in ordered_hhmm if from_hhmm < b <= to_hhmm),
                        None,
                    )
                    if next_bar and next_bar in inc_map:
                        sec_et = _to_sec(t_from)
                        eff_sec = (sec_et % 60) if sec_et is not None else 0
                        sec_ls = _to_sec(last_seal)
                        if sec_ls is not None:
                            ls_hhmm = f"{sec_ls // 3600:02d}{(sec_ls % 3600) // 60:02d}"
                            if ls_hhmm == from_hhmm:
                                eff_sec = max(eff_sec, sec_ls % 60)
                        on_board_ratio = max(0.0, (60.0 - eff_sec) / 60.0) * 0.5
                        deduct_uw = inc_map[next_bar] * (1.0 - on_board_ratio)
                        amt = max(0.0, amt - deduct_uw)
                        deducted_bars.add(next_bar)
                        discounted = True

            # 2) 首封分钟跨秒级打折（含滞后到下一分钟 bar 的边界情况）
            sec_fs = _to_sec(first_seal)
            if sec_fs is not None and from_hhmm:
                fs_hhmm = f"{sec_fs // 3600:02d}{(sec_fs % 3600) // 60:02d}"
                seal_sec = sec_fs % 60
                if seal_sec > 0 and fs_hhmm in inc_map and fs_hhmm not in deducted_bars:
                    if from_hhmm == fs_hhmm or (from_hhmm < fs_hhmm <= (to_hhmm or fs_hhmm)):
                        on_board_ratio = ((60.0 - seal_sec) / 60.0) * 0.5
                        deduct = inc_map[fs_hhmm] * (1.0 - on_board_ratio)
                        amt = max(0.0, amt - deduct)
                        deducted_bars.add(fs_hhmm)
                        discounted = True

            # 3) 炸板当分钟的后半段多为水下砸盘成交，打 0.5 折
            if is_open_end and to_hhmm and to_hhmm in inc_map and to_hhmm not in deducted_bars:
                deduct_open = inc_map[to_hhmm] * 0.5
                amt = max(0.0, amt - deduct_open)
                deducted_bars.add(to_hhmm)
                discounted = True
    except Exception:  # noqa: BLE001
        pass
    out = dict(mr)
    out["amount"] = amt
    out["boundary_discounted"] = discounted
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
                  *, entry_price: float | None = None,
                  enable_cancel: bool = False,
                  rabbit_codes: list[str] | None = None,
                  cancel_thresholds: dict[str, Any] | None = None,
                  cancel_feedback: dict[str, float] | None = None,
                  ice_level: str | None = None,
                  market_vol_ratio: float | None = None,
                  evo_high_turn_delta: float | None = None,
                  evo_rotten_delta: float | None = None) -> dict[str, Any]:
    """完整排板模拟：逐轮开板累积消耗队列（可选严格时序主动撤单）。

    返回：
      status       filled / queued_unfilled / cancelled / unknown
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

    S_raw = entry.get("seal_fund")            # 挂单时真实封单额（时间优先铁律：严禁向下折算）
    fs_est_init = estimate_first_seal_queue(S_raw, entry.get("first_seal"), entry_ts)
    S = float(S_raw) if S_raw is not None else None
    broken_last = entry.get("broken")
    to_last = entry.get("turnover")
    amt_day = daily_amount(day, c)
    total_to = max((v["turnover"] or 0.0)
                   for (_, m) in tmap for cc, v in m.items() if cc == c)
    if S is None or not amt_day or total_to <= 0 or to_last is None:
        return {**base, "detail": "封单/成交额/换手缺一，无法模拟队列消耗"}

    r_codes = [str(rc).zfill(6) for rc in (rabbit_codes or []) if str(rc).strip()]
    r_peak_seal: dict[str, float] = {}
    for rc in r_codes:
        rs = (tmap[i0][1].get(rc) or {}).get("seal_fund")
        if rs is not None and float(rs) > 0:
            r_peak_seal[rc] = float(rs)

    rounds: list[dict[str, Any]] = []
    s_entry = S                     # 上一轮结束时我们前面的队列
    peak_seal = float(S) if S else 0.0
    prev_seal_for_cancel = float(S) if S else 0.0
    i_prev = i0                     # 最后一次「在池」的帧下标
    while i_prev + 1 < len(tmap):
        # ── 向后找到下一个开板事件；途中不断用最新封单更新「当前队列」 ──
        i_open, open_why = None, None
        j = i_prev + 1
        while j < len(tmap):
            cur = tmap[j][1].get(c)
            if cur is None:
                fr_j = frames[j] if j < len(frames) else None
                if _frame_is_truncated_zt(fr_j, len(tmap[j][1])) and c not in _zb_codes_in_frame(fr_j):
                    # 单页 300 截断溢出且不在炸板池，不能把页面溢出冤判成「炸板离池」
                    j += 1
                    continue
                i_open, open_why = j, "炸板离池"
                break
            b_prev = tmap[i_prev][1][c].get("broken")
            b_cur = cur.get("broken")
            if (b_prev is not None and b_cur is not None
                    and int(b_cur) > int(b_prev)):
                i_open, open_why = j, f"开板 {b_prev}→{b_cur}"
                break

            # 仍在涨停池且未新增开板：若启用主动撤单，严格按「先判已吃穿，再判撤单」时序
            if enable_cancel:
                cur_sf = cur.get("seal_fund")
                if cur_sf is not None:
                    cur_sf_f = float(cur_sf)
                    peak_seal = max(peak_seal, cur_sf_f)
                else:
                    cur_sf_f = None

                # 1) 先检查截至当前帧是否已被封板期成交额吃穿（若已吃穿则来不及撤单 → 判成交）
                int_amt = None
                cum_amt = None
                try:
                    mr_int = _boundary_adjusted_amount(
                        c, day, tmap[i_prev][0], tmap[j][0],
                        first_seal=entry.get("first_seal"),
                        last_seal=entry.get("last_seal"),
                        limit_price=entry_price or entry.get("price"),
                    )
                    if mr_int.get("ok") and mr_int.get("amount") is not None:
                        int_amt = max(0.0, float(mr_int["amount"]))
                    if not rounds:
                        mr_cum = _boundary_adjusted_amount(
                            c, day, tmap[i0][0], tmap[j][0],
                            first_seal=entry.get("first_seal"),
                            last_seal=entry.get("last_seal"),
                            limit_price=entry_price or entry.get("price"),
                        )
                        if mr_cum.get("ok") and mr_cum.get("amount") is not None:
                            cum_amt = max(0.0, float(mr_cum["amount"]))
                except Exception:  # noqa: BLE001
                    pass

                if cum_amt is not None and s_entry and s_entry > 0 and cum_amt >= s_entry:
                    ratio_c = cum_amt / s_entry
                    conf_c = "strong" if ratio_c >= RATIO_STRONG else "edge"
                    return {
                        "status": "filled", "fill_ts": tmap[j][0], "fill_round": None,
                        "fill_price": entry_price or entry.get("price"),
                        "confidence": conf_c, "rounds": rounds,
                        "seal_fund_at_entry_yi": round((S or 0) / 1e8, 3),
                        "sealed_traded_yi": round(cum_amt / 1e8, 3),
                        "day_amount_yi": round(amt_day / 1e8, 3),
                        "detail": (
                            f"至 {tmap[j][0]} 封板期涨停价成交 {cum_amt / 1e8:.2f} 亿已吃穿前方队列 "
                            f"{s_entry / 1e8:.2f} 亿（消耗比 {ratio_c:.2f}）→ 已排到（撤单来不及）"
                        ),
                    }

                # 2) 检查同题材锚定一字兔是否炸板或真实失血（统一复用 rabbit_state 单一标准含巨封豁免）
                from backend import rabbit_state as _rs_fv_chk
                r_broken = False
                r_bleed = False
                for rc in r_codes:
                    r_row = tmap[j][1].get(rc)
                    if r_row is None or int(r_row.get("broken") or 0) > 0:
                        r_broken = True
                        break
                    rs_cur = r_row.get("seal_fund")
                    if rs_cur is not None:
                        rs_f = float(rs_cur)
                        pk_r = max(r_peak_seal.get(rc, 0.0), rs_f)
                        r_peak_seal[rc] = pk_r
                        r_s2m = _rs_fv_chk.seal_to_float_mv_ratio({
                            "seal_fund": rs_f,
                            "float_mv": r_row.get("float_mv"),
                            "amount": r_row.get("amount"),
                            "turnover": r_row.get("turnover"),
                        })
                        if _rs_fv_chk.is_rabbit_bleeding(
                            rs_f, pk_r,
                            bleed_threshold=CANCEL_BLEED_RATIO,
                            seal_to_fmv=r_s2m,
                            broken_max=int(r_row.get("broken") or 0),
                        ):
                            r_bleed = True

                # 3) 未吃穿且仍在涨停板上 → 评估主动撤单条件
                eff_th_c = cancel_thresholds
                if eff_th_c is None and any(x is not None for x in (
                    cancel_feedback, ice_level, market_vol_ratio, evo_high_turn_delta, evo_rotten_delta
                )):
                    eff_th_c = adaptive_cancel_thresholds(
                        code=c,
                        market_vol_ratio=market_vol_ratio,
                        ice_level=ice_level,
                        rabbit_weak=bool(r_broken or r_bleed),
                        evo_high_turn_delta=evo_high_turn_delta,
                        evo_rotten_delta=evo_rotten_delta,
                        cancel_feedback=cancel_feedback,
                    )
                from backend import rabbit_state as _rs_fv
                fmv_cur = _rs_fv.estimate_float_mv_yuan({
                    "float_mv": cur.get("float_mv"),
                    "amount": cur.get("amount") or amt_day,
                    "turnover": cur.get("turnover"),
                })
                ev_c = evaluate_cancel_trigger(
                    seal_fund=cur_sf_f,
                    peak_seal=peak_seal if peak_seal > 0 else None,
                    prev_seal=prev_seal_for_cancel if prev_seal_for_cancel > 0 else None,
                    interval_traded_amt=int_amt,
                    day_amount=amt_day,
                    broken=b_cur,
                    ts=tmap[j][0],
                    rabbit_broken=r_broken,
                    rabbit_bleed=r_bleed,
                    rabbit_holding=bool(r_codes and not r_broken and not r_bleed),
                    float_mv=fmv_cur,
                    market_zt_count=len(tmap[j][1]),
                    turnover=cur.get("turnover"),
                    code=c,
                    thresholds=eff_th_c,
                )
                if ev_c["should_cancel"]:
                    return {
                        "status": "cancelled",
                        "fill_ts": None, "fill_round": None,
                        "cancel_ts": tmap[j][0],
                        "cancel_kind": ev_c["kind"],
                        "cancel_reason": ev_c["reason"],
                        "cancel_verified": True,
                        "bleed_ratio": ev_c["bleed_ratio"],
                        "inferred_withdraw_yi": ev_c["inferred_withdraw_yi"],
                        "confidence": "none",
                        "rounds": rounds,
                        "seal_fund_at_entry_yi": round((S or 0) / 1e8, 3),
                        "seal_at_cancel_yi": round((cur_sf_f or 0.0) / 1e8, 3),
                        "day_amount_yi": round(amt_day / 1e8, 3),
                        "detail": (
                            f"{tmap[j][0]} 仍在涨停且队列未吃穿 → 主动撤单成功"
                            f"（{ev_c['reason']}）"
                        ),
                    }
                if cur_sf_f is not None:
                    prev_seal_for_cancel = cur_sf_f

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
            mr = _boundary_adjusted_amount(
                c, day, ts_before, ts_after,
                first_seal=entry.get("first_seal") if not rounds else None,
                last_seal=entry.get("last_seal") if not rounds else None,
                limit_price=entry_price or entry.get("price"),
                is_open_end=True,
            )
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
    # 2026-11-26 进一步精确化（解决「边吃边补单」导致的净封单低估）：
    # 若挂单时封单 1 亿，封板期间卖单打掉 1.2 亿（我方实际已排到），同时又有新买单补进 1 亿，
    # 仅看 S - S_end 只会看到 0.2 亿净差值而误判「未排到」。
    # 全天未开板时，挂单帧到末帧之间的**每一分 1 分钟成交额都发生在涨停价上**，直接消耗买一队列；
    # 因此优先取分钟级累计真实成交额，并与净封单降幅取较大值（均来自真实数据，绝不拍脑袋）。
    last_row = tmap[i_prev][1].get(c) if i_prev < len(tmap) else None
    s_end_raw = (last_row or {}).get("seal_fund")
    s_end = float(s_end_raw) if isinstance(s_end_raw, (int, float)) else s_entry
    net_consumed = max(0.0, float(S_raw or 0.0) - s_end)

    sealed_traded_amt: float | None = None
    sealed_amt_src = "封单净差值近似"
    if not rounds and i_prev > i0:
        try:
            mr = _boundary_adjusted_amount(
                c, day, tmap[i0][0], tmap[i_prev][0],
                first_seal=entry.get("first_seal"),
                last_seal=entry.get("last_seal"),
                limit_price=entry_price or entry.get("price"),
            )
            if mr.get("ok") and mr.get("amount") is not None:
                sealed_traded_amt = max(0.0, float(mr["amount"]))
                sealed_amt_src = "封板期1分钟累计真实成交额"
        except Exception:  # noqa: BLE001
            sealed_traded_amt = None

    eff_consumed = max(net_consumed, sealed_traded_amt) if sealed_traded_amt is not None else net_consumed
    ratio = eff_consumed / S if S and S > 0 else 0.0
    # 首封时刻滞后仅作只读审计对照（实际队列 S 严格取挂单时刻真实封单 S_raw）
    fs_est = fs_est_init
    adj_s = fs_est["adj_queue_yuan"]
    adj_ratio = (eff_consumed / adj_s) if adj_s > 0 else 0.0
    first_seal_audit = {
        "lag_sec": fs_est["lag_sec"],
        "discount": fs_est["discount"],
        "raw_queue_yi": round(float(S_raw or 0) / 1e8, 3),
        "adj_queue_yi": round(adj_s / 1e8, 3),
        "raw_ratio": round(eff_consumed / float(S_raw), 3) if S_raw and float(S_raw) > 0 else 0.0,
        "adj_ratio": round(adj_ratio, 3),
        "adj_filled": bool(not rounds and adj_ratio >= RATIO_FILL),
        "is_auction_seal": fs_est["is_auction_seal"],
    }
    if not rounds and ratio >= RATIO_FILL:
        conf = "edge" if ratio < RATIO_STRONG else "strong"
        traded_txt = (
            f"封板期涨停价真实成交 {sealed_traded_amt/1e8:.2f} 亿"
            f"（封单 {(S or 0)/1e8:.2f}→{s_end/1e8:.2f} 亿，含新补单）"
            if sealed_traded_amt is not None and sealed_traded_amt >= net_consumed
            else f"封单从 {(S or 0)/1e8:.2f} 亿被吃到 {s_end/1e8:.2f} 亿（净消耗 {net_consumed/1e8:.2f} 亿）"
        )
        return {
            "status": "filled", "fill_ts": None, "fill_round": None,
            "fill_price": entry_price or entry.get("price"),
            "confidence": conf, "rounds": rounds,
            "queue_left_yi": round(max(0.0, (S or 0) - eff_consumed) / 1e8, 3),
            "seal_fund_at_entry_yi": round((S or 0) / 1e8, 3),
            "seal_consumed_yi": round(eff_consumed / 1e8, 3),
            "sealed_traded_yi": round(sealed_traded_amt / 1e8, 3) if sealed_traded_amt is not None else None,
            "amount_src": sealed_amt_src,
            "day_amount_yi": round(amt_day / 1e8, 3),
            "first_seal_audit": first_seal_audit,
            "detail": (f"全天未开板，但{traded_txt}，消耗比 {ratio:.2f} ≥ 1.0 → "
                       f"涨停价真实成交已吃穿前方队列，排板"
                       + ("高置信成交" if conf == "strong" else "边缘成交（缺逐笔撤单明细）")),
        }
    traded_note = (
        f"，封板期涨停价累计成交 {sealed_traded_amt/1e8:.2f} 亿"
        if sealed_traded_amt is not None else ""
    )
    return {
        "status": "queued_unfilled",
        "fill_ts": None, "fill_round": None, "confidence": "none",
        "rounds": rounds,
        "queue_left_yi": round(max(0.0, s_end) / 1e8, 3),
        "seal_fund_at_entry_yi": round((S or 0) / 1e8, 3),
        "seal_consumed_yi": round(eff_consumed / 1e8, 3),
        "sealed_traded_yi": round(sealed_traded_amt / 1e8, 3) if sealed_traded_amt is not None else None,
        "amount_src": sealed_amt_src if not rounds else "逐轮开板消耗",
        "day_amount_yi": round(amt_day / 1e8, 3),
        "first_seal_audit": first_seal_audit,
        "detail": (f"共 {len(rounds)} 轮开板，累计只消耗部分队列，"
                   f"收盘仍剩 {s_end/1e8:.2f} 亿排在前面 → 未排到"
                   if rounds else
                   f"全天未开板，封单 {(S or 0)/1e8:.2f}→{s_end/1e8:.2f} 亿{traded_note}"
                   f"（消耗比 {ratio:.2f} < 1.0），涨停价虽有成交，但没吃穿前面的封单 → 排板未成交"),
    }


