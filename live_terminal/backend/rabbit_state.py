# -*- coding: utf-8 -*-
"""兔子四态强度引擎 & 扫板影子探测器（2026-11-27 P1）。

一、兔子强度量化（全用真实可采盘口/留档数据，绝不编造）：
  1. 09:20–09:25 不可撤阶段竞价真实度：匹配额增减率（加单 vs 缩水）+ 09:25 封单/流通市值；
  2. 封单占流通市值（seal_to_float_mv）：比绝对封单额更准确衡量筹码锁定强度；
  3. 开盘后趋势：封单相对开盘/峰值的加单还是撤单；
  4. 同题材一字数量与连板身位。

二、兔子四态状态机（anchored / decaying / diverging / failed）：
  - anchored（锚定）：封死未开板，封单稳健或加单 → 正常锚定同题材乌龟；
  - decaying（衰减）：尚未开板但封单相对峰值缩水 ≥ 40% → 警示，暂停新开同题材乌龟；
  - diverging（分歧）：盘中首次开板但快速回封（broken==1 且当前在池）→ 观察不盲撤；
  - failed（失效）：
      * 竞价阶段失效（09:25 未封死一字）→ 取消锚定，当日不做该题材；
      * 盘中阶段失效（炸板离池未回封 / 多次炸板）→ 不武断强杀卡位强乌龟，
        交由动态撤单阈值收紧 + 反事实追踪「用市场说话」，已成交乌龟绝不强杀。

三、扫板影子（sweep_shadow）：
  针对兔子已锚定题材内的 5~15 只候选乌龟，定向高频（3 秒级）检查五档盘口：
  距涨停 ≤ 1 档、卖一挂单薄（< 500 万）且放量冲板时，记录为「扫板影子成交（队首）」，
  独立记账验证期望收益，不污染 B1 主账本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from backend import feature_engine

STATE_CN: dict[str, str] = {
    "anchored": "锚定",
    "decaying": "衰减",
    "diverging": "分歧",
    "failed": "失效",
    "pending": "待开盘",
}

#: 一字兔绝对封单厚度豁免与统一失血判据（Single Source of Truth：rabbit_state + fill_verify 共用）
RABBIT_THICK_SEAL_YUAN = 100_000_000.0
RABBIT_THICK_SEAL_FMV_RATIO = 0.02
RABBIT_BLEED_RATIO = 0.40


def is_rabbit_thick_exempt(
    seal_fund: float | None,
    seal_to_fmv: float | None = None,
    *,
    broken_max: int = 0,
) -> bool:
    """未开板一字兔若仍趴着 >=1亿 或 封流比>=2% 巨额封单，豁免相对回撤 decaying 误判。"""
    if int(broken_max or 0) > 0 or seal_fund is None:
        return False
    sf = float(seal_fund)
    return bool(
        sf >= RABBIT_THICK_SEAL_YUAN
        or (seal_to_fmv is not None and float(seal_to_fmv) >= RABBIT_THICK_SEAL_FMV_RATIO)
    )


def is_rabbit_bleeding(
    cur_seal: float | None,
    peak_seal: float | None,
    *,
    bleed_threshold: float = RABBIT_BLEED_RATIO,
    seal_to_fmv: float | None = None,
    broken_max: int = 0,
) -> bool:
    """统一判断一字兔是否发生真实失血（未开板巨单自动豁免）。"""
    if cur_seal is None or peak_seal is None or float(peak_seal) <= 0:
        return False
    if is_rabbit_thick_exempt(cur_seal, seal_to_fmv, broken_max=broken_max):
        return False
    bleed = (float(peak_seal) - float(cur_seal)) / float(peak_seal)
    return bleed >= float(bleed_threshold)


def _frame_hhmm(frame: dict[str, Any] | None) -> str:
    if not isinstance(frame, dict):
        return ""
    s = str(frame.get("ts") or "").strip()
    if "T" in s:
        s = s.split("T", 1)[1]
    elif " " in s:
        s = s.split(" ", 1)[1]
    return s[:5] if len(s) >= 5 and s[2] == ":" else ""


def estimate_float_mv_yuan(row: dict[str, Any]) -> float | None:
    """估算流通市值（元）：优先直接字段，其次用 amount / (turnover/100) 反推。"""
    for k in ("float_mv", "circ_mv", "float_market_cap", "lt_mv"):
        v = row.get(k)
        if v is not None:
            try:
                fv = float(v)
                if fv > 0:
                    return fv
            except (TypeError, ValueError):
                pass
    amt = row.get("amount")
    turn = row.get("turnover")
    try:
        if amt is not None and turn is not None and float(amt) > 0 and float(turn) > 0.05:
            return float(amt) / (float(turn) / 100.0)
    except (TypeError, ValueError):
        pass
    return None


def seal_to_float_mv_ratio(row: dict[str, Any]) -> float | None:
    """封单占流通市值比例（如 0.05 表示封单占流通市值 5%）。"""
    sf = row.get("seal_fund")
    if sf is None:
        return None
    try:
        sff = float(sf)
        if sff <= 0:
            return 0.0
    except (TypeError, ValueError):
        return None
    fmv = estimate_float_mv_yuan(row)
    if fmv and fmv > 0:
        return round(sff / fmv, 5)
    return None


def evaluate_rabbit_state(
    rabbit_codes: list[str],
    frames: list[dict[str, Any]],
    *,
    auction_snapshots: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """量化题材内锚定兔子的实时强度与四态（anchored / decaying / diverging / failed）。"""
    codes = [str(c).zfill(6) for c in (rabbit_codes or []) if str(c).strip()]
    if not codes or not frames:
        return {
            "state": "failed",
            "state_cn": STATE_CN["failed"],
            "strength_score": 0.0,
            "n_rabbits": len(codes),
            "rabbits": [],
            "note": "无锚定兔子或无盘中留档帧",
        }

    from backend import live_sim as _ls
    # 屏蔽 14:57 后收盘集合竞价 / 盘后东财清零帧对封单厚度的失真覆盖
    intraday_frames = [f for f in frames if not (_frame_hhmm(f) >= "14:57")]
    eff_frames = intraday_frames if intraday_frames else list(frames)

    # 追踪每只兔子在留档帧中的峰值封单、初始封单、最新状态
    per_code: dict[str, dict[str, Any]] = {
        c: {
            "ts_code": c, "name": "", "first_seal_fund": None, "first_raw_seal": None,
            "peak_seal_fund": 0.0, "latest_seal_fund": 0.0,
            "in_latest": False, "broken_max": 0, "streak": 2,
            "seal_to_fmv": None, "trend": "unknown", "state": "failed",
        }
        for c in codes
    }
    non_empty_zt_frames = 0
    for fr in eff_frames:
        rows_chk, _ = _ls._pool_rows(fr)
        if rows_chk:
            non_empty_zt_frames += 1
    if non_empty_zt_frames == 0:
        return {
            "state": "pending",
            "state_cn": STATE_CN["pending"],
            "strength_score": 50.0,
            "n_rabbits": len(codes),
            "n_anchored": 0,
            "rabbits": [],
            "note": "盘前竞价阶段涨停池尚未出数，暂不判定兔子失效",
        }

    last_idx = len(eff_frames) - 1
    last_frame_truncated = False
    last_zb_codes: set[str] = set()
    if eff_frames:
        zt_blk = eff_frames[last_idx].get("zt") if isinstance(eff_frames[last_idx], dict) else None
        if isinstance(zt_blk, dict):
            rows_last, _ = _ls._pool_rows(eff_frames[last_idx])
            tc_last = zt_blk.get("tc")
            all_comp = zt_blk.get("all_complete")
            last_frame_truncated = bool(
                all_comp is False
                or (len(rows_last) == 300 and (tc_last is None or int(tc_last or 0) != 300))
            )
        zb_blk = eff_frames[last_idx].get("zb") if isinstance(eff_frames[last_idx], dict) else None
        if isinstance(zb_blk, dict):
            for zr in (zb_blk.get("all") or zb_blk.get("top") or []):
                if isinstance(zr, dict) and zr.get("ts_code"):
                    last_zb_codes.add(str(zr["ts_code"]).zfill(6))

    day8 = ""
    ts0 = str(eff_frames[-1].get("ts") or "") if eff_frames else ""
    if len(ts0) >= 10 and ts0[:4].isdigit():
        day8 = ts0[:10].replace("-", "")

    for idx, fr in enumerate(eff_frames):
        rows, _ = _ls._pool_rows(fr)
        rmap = {str(r.get("ts_code") or "").zfill(6): r for r in rows if r.get("ts_code")}
        hhmm_fr = _frame_hhmm(fr)
        for c in codes:
            r = rmap.get(c)
            info = per_code[c]
            if r is None:
                if idx == last_idx:
                    if last_frame_truncated and c not in last_zb_codes and info["peak_seal_fund"] > 0:
                        # 末帧涨停池可能因 300 条页码截断溢出且不在炸板池，保留上一帧有效状态不冤判 failed
                        info["in_latest"] = True
                        info["truncated_protected"] = True
                    else:
                        info["in_latest"] = False
                continue
            info["name"] = str(r.get("name") or info["name"])
            sf = float(r.get("seal_fund") or 0.0)
            if info["first_raw_seal"] is None and sf > 0:
                info["first_raw_seal"] = sf
            # 忽略 09:25 竞价初期万级碎单噪音，从连续竞价或 >=100万 起锚定初始封单
            if info["first_seal_fund"] is None and (sf >= 1_000_000.0 or (hhmm_fr >= "09:30" and sf > 0)):
                info["first_seal_fund"] = sf
            info["peak_seal_fund"] = max(info["peak_seal_fund"], sf)
            info["latest_seal_fund"] = sf
            info["broken_max"] = max(info["broken_max"], int(r.get("broken") or 0))
            if r.get("streak"):
                try:
                    info["streak"] = max(info["streak"], int(r["streak"]))
                except (TypeError, ValueError):
                    pass
            s2m = seal_to_float_mv_ratio(r)
            if s2m is None and sf > 0 and r.get("turnover") and day8:
                try:
                    turn_v = float(r["turnover"])
                    if turn_v > 0.05:
                        from backend import fill_verify as _fv_rs
                        amt_d = _fv_rs.daily_amount(day8, c)
                        if amt_d and amt_d > 0:
                            s2m = round(sf / (amt_d / (turn_v / 100.0)), 5)
                except Exception:  # noqa: BLE001
                    s2m = None
            if s2m is not None:
                info["seal_to_fmv"] = s2m
            if idx == last_idx:
                info["in_latest"] = True

    rank_order = {"anchored": 3, "decaying": 2, "diverging": 1, "failed": 0}
    best_state = "failed"
    r_list: list[dict[str, Any]] = []
    strength_sum = 0.0

    for c in codes:
        info = per_code[c]
        pk = info["peak_seal_fund"]
        cur = info["latest_seal_fund"]
        f0 = info["first_seal_fund"] or info["first_raw_seal"] or pk
        bleed = ((pk - cur) / pk) if pk > 0 else 0.0
        thick_exempt = is_rabbit_thick_exempt(cur, info["seal_to_fmv"], broken_max=info["broken_max"])

        # 开盘后加单/撤单趋势（优先判相对峰值回落，避免 09:25 碎单导致与衰减文案矛盾）
        if thick_exempt and bleed >= RABBIT_BLEED_RATIO:
            info["trend"] = f"巨单稳封({cur / 1e8:.2f}亿,较峰-{bleed:.0%})"
        elif pk > 0 and bleed >= RABBIT_BLEED_RATIO:
            info["trend"] = f"封单衰减(-{bleed:.0%})"
        elif pk > 0 and bleed >= 0.20:
            info["trend"] = f"小幅回落(-{bleed:.0%})"
        elif f0 > 0 and cur >= f0 * 1.10:
            info["trend"] = "加单增强"
        else:
            info["trend"] = "封单平稳"

        # 09:20–09:25 竞价真实度（若外部采集了 09:20 与 09:25 快照则纳入）
        auc = (auction_snapshots or {}).get(c) or {}
        auc_growth = auc.get("match_amt_growth_920_925")  # >0 加单，<0 撤单缩水

        # 四态判定
        if not info["in_latest"] or info["broken_max"] >= 2:
            st = "failed"
        elif info["broken_max"] == 1:
            st = "diverging"
        elif not thick_exempt and (
            bleed >= RABBIT_BLEED_RATIO or (auc_growth is not None and float(auc_growth) <= -0.35)
        ):
            st = "decaying"
        else:
            st = "anchored"
        info["state"] = st
        info["state_cn"] = STATE_CN[st]
        info["bleed_ratio"] = round(bleed, 3)

        # 单兔量化强度（0~100）：客观量价指标，仅用于强弱排序与仓位/卖点参考
        sc = 50.0
        if st == "anchored":
            sc += 20.0
        elif st == "decaying":
            sc -= 10.0
        elif st == "diverging":
            sc -= 20.0
        else:
            sc -= 40.0
        if info["seal_to_fmv"] is not None:
            if info["seal_to_fmv"] >= 0.05:
                sc += 15.0
            elif info["seal_to_fmv"] >= 0.02:
                sc += 8.0
        elif cur >= 1.5e8:
            sc += 12.0
        elif cur >= 0.5e8:
            sc += 5.0
        if info["trend"] == "加单增强":
            sc += 10.0
        if info["streak"] >= 3:
            sc += 5.0
        if auc_growth is not None:
            sc += max(-10.0, min(10.0, float(auc_growth) * 20.0))
        info["strength_score"] = round(max(0.0, min(100.0, sc)), 1)
        strength_sum += info["strength_score"]

        if rank_order[st] > rank_order[best_state]:
            best_state = st
        r_list.append({
            "ts_code": c,
            "name": info["name"],
            "state": st,
            "state_cn": STATE_CN[st],
            "strength_score": info["strength_score"],
            "streak": info["streak"],
            "latest_seal_yi": round(cur / 1e8, 3),
            "peak_seal_yi": round(pk / 1e8, 3),
            "seal_to_fmv": info["seal_to_fmv"],
            "bleed_ratio": info["bleed_ratio"],
            "trend": info["trend"],
            "broken_max": info["broken_max"],
        })

    # 同题材多兔共振加成（≥2 只处于 anchored）
    n_anchored = sum(1 for x in r_list if x["state"] == "anchored")
    avg_strength = (strength_sum / len(r_list)) if r_list else 0.0
    if n_anchored >= 2:
        avg_strength = min(100.0, avg_strength + 8.0)

    return {
        "state": best_state,
        "state_cn": STATE_CN[best_state],
        "strength_score": round(avg_strength, 1),
        "n_rabbits": len(r_list),
        "n_anchored": n_anchored,
        "rabbits": r_list,
        "note": (
            f"兔子{STATE_CN[best_state]}（强度 {avg_strength:.0f}，"
            f"{n_anchored}/{len(r_list)} 只稳封）："
            + "；".join(f"{x['name'] or x['ts_code']}({x['state_cn']},{x['latest_seal_yi']:.2f}亿,{x['trend']})"
                       for x in r_list[:3])
        ),
    }


def scan_sweep_shadow(
    candidates: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    *,
    max_ask1_fmv_ratio: float = 0.0015,
    max_ask1_amt_ratio: float = 0.015,
    fallback_ask1_yuan: float = 5_000_000.0,
) -> list[dict[str, Any]]:
    """兼容入口：统一委托至 buy_signals.scan_first_to_second_sweep（消除双套扫板探测器重复）。"""
    from backend import buy_signals
    # 转换为 theme_rabbits / theme_members / quotes 调用统一主实现
    theme_members: dict[str, list[str]] = {}
    theme_rabbits: dict[str, list[str]] = {}
    for item in (candidates or []):
        c = str(item.get("ts_code") or "").zfill(6)
        th = str(item.get("theme") or item.get("guitu_theme") or "default")
        if c:
            theme_members.setdefault(th, []).append(c)
            theme_rabbits.setdefault(th, ["000000"])
    hits = buy_signals.scan_first_to_second_sweep(theme_rabbits, theme_members, quotes)
    for h in hits:
        h.setdefault("exec_mode", "sweep_shadow")
    return hits
