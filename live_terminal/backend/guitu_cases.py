"""悟空真经 · 龟兔案例库 / 今日实时快照 / 命题检验（2026-11-12 用户补充）。

用户口径
------------------------------------------------
「只要有连板行情，有持续性题材，龟兔就最适合操作。」
兔子 = 一字板（买不进），乌龟 = 厂字板（有换手、能上车）；
同题材同身位，最后走出来的往往是乌龟。

用户给的案例（全部按仓库真实日线 / 实时留档核对，不合成数据）：
  · 机器人（2026-11-12）：日发精机一字兔子、三联锻造厂字板乌龟、宁波东力盘子偏大
  · 「光 / 洋」（2026-11-12）：青山纸业一字兔子、天洋新材厂字板乌龟（疑似叠加「洋」字辈）
  · AI 传媒：龙版传媒(乌龟) 胜 欢瑞世纪(兔子)；慈文传媒(乌龟) 胜 新经典(兔子)
  · 还有 大晟文化 / 浙文影业、大业股份 / 澳洋健康、华瓷股份 / 西陇科学 / 中晶科技（同时期）
  · 牛股高标周期：乐山电力 2025-04-08~07-08、华电辽能 2026-02-04~06-05、
    华电能源 2026-02-24~06-05（已并入 leader_ladder.CASE_PERIODS）

**关键边界（必须先读）**
------------------------------------------------
1. 「一字 / 厂字」是**分时形态**，仓库只有日线 OHLC + 当日快照（换手、封单、开板次数），
   没有逐笔/分时，无法还原「厂字板」。本模块只给**近似代理**：低换手+零开板+大封单 → 一字倾向；
   高换手/开板回封 → 换手充分倾向。**代理不等于用户标注的角色**，两者冲突时原样并列、不改写。
2. 用户给的都是「乌龟胜出」的**事后选中案例**，是典型选择性样本，不能当统计证据。
   命题检验必须拿全市场落盘统计做对照（guitu_sim.json / weekly_review.json）。
3. 「胜出」有两种口径，结论会相反，必须分开：
   - 单波连板高度 / 次日晋级：低换手（一字倾向）占优，但**排板买不进**；
   - 跨波趋势持续性（40 日 ALIVE/趋势龙头）：用户标注的「乌龟」在这些案例里占优。
4. 「持续性题材」目前没有统一量化定义；本模块用同板块高标数、同日同身位兔子作代理，
   并在命题检验中如实给出**反向**证据。结论不进入任何交易规则。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from backend import leader_ladder

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "data" / "panel_raw.parquet"
GUITU_SIM = ROOT / "data" / "guitu_sim.json"
WEEKLY = ROOT / "data" / "weekly_review.json"
LIVE = ROOT / "data" / "live"

# 用户标注的角色：rabbit=一字兔子，turtle=厂字乌龟，heavy=盘子偏大，none=未标注
ROLE_CN = {"rabbit": "一字兔子", "turtle": "厂字乌龟", "heavy": "盘子偏大", "none": "未标注"}

PAIR_GROUPS: list[dict[str, Any]] = [
    {
        "id": "robot_20261112", "theme": "机器人", "anchor": "2026-11-12",
        "title": "2026-11-12 · 机器人", "live": True,
        "note": "用户：日发一字兔子，三联标准厂字板乌龟，东力盘子大了点",
        "members": [
            {"ts_code": "002520", "name": "日发精机", "role": "rabbit"},
            {"ts_code": "001282", "name": "三联锻造", "role": "turtle"},
            {"ts_code": "002164", "name": "宁波东力", "role": "heavy"},
        ],
    },
    {
        "id": "yang_20261112", "theme": "光 /「洋」字辈", "anchor": "2026-11-12",
        "title": "2026-11-12 · 光 /「洋」", "live": True,
        "note": "用户：青山一字兔子，天洋厂字板乌龟叠加的洋；澳洋健康同日 1 板（「洋」字辈叠加为推测，待核对）",
        "members": [
            {"ts_code": "600103", "name": "青山纸业", "role": "rabbit"},
            {"ts_code": "603330", "name": "天洋新材", "role": "turtle"},
            {"ts_code": "002172", "name": "澳洋健康", "role": "none"},
        ],
    },
    {
        "id": "ai_media_20261009", "theme": "AI 传媒", "anchor": "2026-10-09",
        "title": "AI 传媒 · 2026-10-09 同日启动", "live": False,
        "note": "用户：龙版传媒(乌龟)胜欢瑞世纪(兔子)；慈文传媒(乌龟)战胜新经典(兔子)",
        "members": [
            {"ts_code": "605577", "name": "龙版传媒", "role": "turtle", "user_verdict": "胜出"},
            {"ts_code": "000892", "name": "欢瑞世纪", "role": "rabbit"},
            {"ts_code": "002343", "name": "慈文传媒", "role": "turtle", "user_verdict": "胜出"},
            {"ts_code": "603096", "name": "新经典", "role": "rabbit"},
        ],
    },
    {
        "id": "ai_media_20260831", "theme": "AI 传媒", "anchor": "2026-08-31",
        "title": "AI 传媒 · 2026-08-31 波次", "live": False,
        "note": "同一题材的前一波；大晟文化为同板块参照",
        "members": [
            {"ts_code": "605577", "name": "龙版传媒", "role": "turtle"},
            {"ts_code": "000892", "name": "欢瑞世纪", "role": "rabbit"},
            {"ts_code": "600892", "name": "大晟文化", "role": "none"},
        ],
    },
    {
        "id": "media_20261102", "theme": "传媒", "anchor": "2026-11-02",
        "title": "传媒 · 2026-11-02 波次", "live": False,
        "note": "用户补充：大晟文化、浙文影业",
        "members": [
            {"ts_code": "605577", "name": "龙版传媒", "role": "none"},
            {"ts_code": "600892", "name": "大晟文化", "role": "none"},
            {"ts_code": "601599", "name": "浙文影业", "role": "none"},
        ],
    },
    {
        "id": "same_period_20260915", "theme": "同时期（题材待核对）", "anchor": "2026-09-15",
        "title": "华瓷 / 西陇 / 中晶 · 2026-09-15 同时期", "live": False,
        "note": "用户：这三个都是同时期；未标注兔/龟，此处只做真实数据并列",
        "members": [
            {"ts_code": "001216", "name": "华瓷股份", "role": "none"},
            {"ts_code": "002584", "name": "西陇科学", "role": "none"},
            {"ts_code": "003026", "name": "中晶科技", "role": "none"},
        ],
    },
    {
        "id": "yang_20260812", "theme": "「洋」字辈", "anchor": "2026-08-12",
        "title": "澳洋 / 天洋 · 2026-08-12 同日 4 板", "live": False,
        "note": "用户补充：大业股份与澳洋健康；大业股份当日无同窗口连板，单列于下方",
        "members": [
            {"ts_code": "002172", "name": "澳洋健康", "role": "none"},
            {"ts_code": "603330", "name": "天洋新材", "role": "none"},
        ],
    },
]

# 用户提到但与其他票无同日启动窗口，单列展示（大业股份 2026-10-16 起 6 板）
EXTRA_CODES: dict[str, str] = {"603278": "大业股份"}


def _json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


#: 实际使用的面板来源（供 payload 与前端标注，避免「用旧数据却不说」）
_PANEL_SOURCE: str = "未读取"


def _panel_source() -> str:
    return _PANEL_SOURCE


def _panel() -> pl.DataFrame:
    """真实日线面板（**经过合并视图**，含 panel_live 的当日/近日行）。

    2026-11-18 验收修复（P0）
    ------------------------
    原实现是 `pl.read_parquet(PANEL)`，`PANEL = data/panel_raw.parquet`。
    `panel_raw` 只由离线流程重建，实测曾停在 **2026-11-12**
    （用户 11-18 投诉「悟空真经选出来的都不是今天的」的直接来源）：
    本模块的 `payload()["panel_last"]` 因此恒为 2026-11-12，落后 4 个交易日。

    改为 `feeds.build_panel()`：它读 panel_adj（含复权列）并**只读合并**
    panel_live（15:05 闸门后的当日行），是全项目「历史面板」的唯一正确入口
    （见 feeds.py:686 docstring）。

    保留**回退路径**：feeds 不可用时退回 panel_raw 直读，且把回退事实
    记在 `_PANEL_SOURCE` 里供 payload 标注，绝不静默。
    """
    global _PANEL_SOURCE
    try:
        from backend import feeds
        df = feeds.build_panel()
        if df is not None and df.height > 0:
            _PANEL_SOURCE = "feeds.build_panel（含 panel_live 合并视图）"
            return df
    except Exception as e:  # noqa: BLE001
        print(f"[guitu_cases] build_panel 不可用，回退 panel_raw: "
              f"{type(e).__name__}: {e}", flush=True)
    _PANEL_SOURCE = "panel_raw.parquet（直读回退，可能滞后）"
    return pl.read_parquet(PANEL)


def _member_metrics(panel: pl.DataFrame, code: str, anchor: str) -> dict[str, Any]:
    """单只票在 anchor 后的真实日线指标：单波 12 日 + 跨波 40 日。"""
    d = panel.filter(pl.col("ts_code") == code).sort("trade_date")
    if d.height < 45:
        return {"available": False, "reason": "日线不足"}
    c = d["close"].to_numpy().astype(float)
    pc = d["pct_chg"].to_numpy().astype(float)
    to = d["turnover"].to_numpy().astype(float)
    zt, streak = leader_ladder._zt_flags(code, c)
    m = leader_ladder.metrics_for(code, c, pc)
    dates = [str(x) for x in d["trade_date"].to_list()]
    i = next((k for k, x in enumerate(dates) if x >= anchor), None)
    if i is None or i < 1:
        return {"available": False, "reason": "anchor 超出面板范围"}

    # 单波：anchor 起 12 个交易日
    end12 = min(i + 12, len(c))
    w12 = list(range(i, end12))
    zdays = [j for j in w12 if zt[j]]
    base = float(c[i - 1])
    last_zt = zdays[-1] if zdays else i
    single = {
        "first_turnover": round(float(to[i]), 2),
        "zt_day_turnover_mean": round(float(np.mean([to[j] for j in zdays])), 2) if zdays else None,
        "max_streak": int(streak[w12].max()),
        "zt_days": len(zdays),
        "peak_gain": round(float(c[w12].max() / base - 1), 4),
        "after_break_5d": round(float(c[min(last_zt + 5, len(c) - 1)] / c[last_zt] - 1), 4),
    }
    # 跨波：anchor 起 40 个交易日
    end40 = min(i + 40, len(c))
    w40 = list(range(i, end40))
    wide = {
        "sessions": len(w40),
        "note": "不足 40 日表示 anchor 距面板末尾不足 40 个交易日",
        "alive_days": int(sum(1 for j in w40 if m["alive"][j])),
        "cooling_days": int(sum(1 for j in w40 if m["cooling"][j])),
        "trend_days": int(sum(1 for j in w40 if m["trend"][j])),
        "fwd_40d": round(float(c[end40 - 1] / base - 1), 4),
        "max_run20": round(float(np.nanmax(m["run"][w40])), 4),
    }
    # 近似代理：低换手 + 零开板倾向一字；高换手倾向换手充分
    t = single["zt_day_turnover_mean"]
    if t is None:
        proxy = "无涨停日"
    elif t < 5:
        proxy = "低换手 · 一字倾向"
    elif t >= 12:
        proxy = "高换手 · 换手充分"
    else:
        proxy = "中等换手 · 形态不可判"
    return {
        "available": True, "anchor_actual": dates[i],
        "single": single, "wide": wide, "proxy": proxy,
        "proxy_note": "日线换手率只是弱代理，不能反推分时一字/厂字",
        "trail": [
            {"date": dates[j], "turnover": round(float(to[j]), 1),
             "streak": int(streak[j]), "zt": bool(zt[j]), "close": round(float(c[j]), 2)}
            for j in w12[:10]
        ],
    }


def _live_snapshot() -> dict[str, Any]:
    """今日实时涨停池留档：全帧扫描，不只看最新帧（有票会中途退出池）。"""
    days = sorted([p for p in LIVE.iterdir() if p.is_dir() and p.name.isdigit()])
    if not days:
        return {"available": False, "reason": "无实时留档目录"}
    day = days[-1]
    fs = sorted((day / "pools").glob("*.jsonl"))
    if not fs:
        return {"available": False, "reason": f"{day.name} 无涨停池留档"}
    watch: dict[str, str] = {}
    for g in PAIR_GROUPS:
        if g.get("live"):
            for m in g["members"]:
                watch[m["ts_code"]] = m["name"]
    frames: dict[str, list[dict[str, Any]]] = defaultdict(list)
    first_ts = last_ts = ""
    for ln in fs[-1].read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        ts = str(d.get("ts", ""))
        if not first_ts:
            first_ts = ts
        last_ts = ts
        for r in (d.get("zt") or {}).get("top") or []:
            if r.get("ts_code") in watch:
                frames[r["ts_code"]].append({
                    "ts": ts, "streak": r.get("streak"), "price": r.get("price"),
                    "turnover": round(float(r.get("turnover") or 0), 2),
                    "seal_yi": round(float(r.get("seal_fund") or 0) / 1e8, 3),
                    "broken": r.get("broken"), "industry": r.get("industry"),
                })
    out: dict[str, Any] = {}
    for code, name in watch.items():
        v = frames.get(code, [])
        if not v:
            out[code] = {"name": name, "available": False, "reason": "今日涨停池留档中未出现"}
            continue
        a, b = v[0], v[-1]
        to = b["turnover"]
        proxy = ("低换手 · 一字倾向" if to < 3 and (b["broken"] or 0) == 0
                 else "高换手/开板 · 换手充分" if to >= 8 or (b["broken"] or 0) >= 2
                 else "中等换手 · 形态不可判")
        out[code] = {
            "name": name, "available": True, "n_frames": len(v),
            "first": a, "last": b,
            "turnover_delta": round(b["turnover"] - a["turnover"], 2),
            "seal_delta_yi": round(b["seal_yi"] - a["seal_yi"], 3),
            "proxy": proxy,
        }
    return {
        "available": True, "day": day.name,
        "first_ts": first_ts, "last_ts": last_ts,
        "n_frames": sum(1 for _ in fs[-1].read_text(encoding="utf-8").splitlines()),
        "members": out,
        "note": "封单额/开板次数/换手率来自涨停池快照；无分时与委托队列，"
                "「一字 / 厂字」只能近似，不能当作分时形态判定。",
    }


def _thesis() -> dict[str, Any]:
    """命题检验：只读取已落盘的全市场统计，不重算面板、不调参。"""
    g = _json(GUITU_SIM)
    w = _json(WEEKLY)
    q = (g.get("guitu") or {}).get("queue_counts") or {}
    rabbit = (g.get("guitu") or {}).get("by_rabbit") or {}
    main = w.get("main_strategy") or ""
    attr = ((w.get("contrast") or {}).get(main) or {}).get("attribution") or {}
    turn = {r["label"]: r for r in attr.get("换手率", [])}
    same = {r["label"]: r for r in attr.get("高标存亡·同板块", [])}
    has_r = rabbit.get("有兔子（同日同身位存在一字二板）") or {}
    no_r = rabbit.get("无兔子") or {}
    hr = round((has_r.get("win") or 0) * 100, 2)
    nr = round((no_r.get("win") or 0) * 100, 2)
    low_t = round((turn.get("低换手", {}).get("advance_rate") or 0) * 100, 1)
    high_t = round((turn.get("高换手", {}).get("advance_rate") or 0) * 100, 1)
    hot = round((same.get("3 只以上", {}).get("advance_rate") or 0) * 100, 1)
    cold = round((same.get("同板块无高标", {}).get("advance_rate") or 0) * 100, 1)

    return {
        "statement": "只要有连板行情，有持续性题材，龟兔就最适合操作",
        "status": "作为「执行前提」成立；作为「最优策略结论」证据不足，登记为待检验假设",
        "checks": [
            {
                "item": "可操作性：一字兔子买不进",
                "verdict": "支持",
                "evidence": f"全市场二板排板回放：一字板确定跳过 {q.get('one_word', '—')} 笔、"
                            f"全日封死 {q.get('sealed', '—')} 笔；"
                            f"「没买却晋级」漏买 100% 为机械不可得。乌龟能上车是执行前提，不是超额收益。",
            },
            {
                "item": "同身位有兔子作参照时，乌龟胜率更高",
                "verdict": "弱支持（描述性）",
                "evidence": f"「有兔子」组 n={has_r.get('n', '—')} 胜率 {hr}% vs "
                            f"「无兔子」组 n={no_r.get('n', '—')} 胜率 {nr}%（差 {round(hr - nr, 2)}pp）。"
                            "未控制混淆、未做显著性检验、样本内。",
            },
            {
                "item": "跨波趋势持续性：用户案例里乌龟胜出",
                "verdict": "支持（但为事后选样）",
                "evidence": "2026-10-09 波次后 40 日：慈文(乌龟) +75% / 10 天存活 vs "
                            "新经典(兔子) +12% / 3 天；2026-08-31：龙版 +74% / 13 天 vs 欢瑞 +13% / 4 天。"
                            "用户只给了胜出案例，无失败案例，属选择性样本。",
            },
            {
                "item": "单波晋级：换手充分是否更好",
                "verdict": "不支持",
                "evidence": f"同池分箱晋级率：低换手 {low_t}% > 高换手 {high_t}%。"
                            "一字倾向的票次日更容易继续涨停，但**排板买不进**，"
                            "这正是「晋级率高 ≠ 能吃到」的机械偏差。",
            },
            {
                "item": "题材热度（持续性题材代理）是否提升晋级",
                "verdict": "不支持",
                "evidence": f"同板块高标 3 只以上晋级 {hot}% < 同板块无高标 {cold}%。"
                            "同板块高标多也可能是拥挤/退潮信号；该代理未通过检验。",
            },
        ],
        "falsifiable": [
            "定义「持续性题材」的可计算判据（如题材连续 N 日有 ≥M 只连板、龙头跨波不破位），"
            "再在**样本外**时段统计龟兔组合相对基准的超额；当前无此定义，命题不可证伪。",
            "需要分时代理或 Level-2 委托队列才能把「厂字板」与「一字板」真正分开；"
            "当前日线换手率只是弱代理，且与用户标注的角色在多处冲突。",
            "需要纳入「乌龟失败」案例与全市场同题材配对，否则选择性样本会系统性高估胜出率。",
            "「最适合」需要与其他策略在同一资金/同一撮合规则下对比；仓库当前未做该对照。",
        ],
        "verdict_note": "方向可作为**操作前提**写进悟空真经：题材有连板行情时，优先找能成交的"
                        "厂字乌龟，跳过排不进的一字兔子。但不得写成「已验证最优策略」，也不进入开仓规则。",
    }


def similar_for(industry: str | None, streak: int | None = None,
                limit: int = 2) -> list[dict[str, Any]]:
    """内部案例匹配：当前候选行业/身位 → 历史相似案例（只作规律提示）。

    2026-11-16 用户口径：案例库只用于「举例总结规律 / 匹配当前市场相似股票」，
    不再整页展示。本函数只返回紧凑摘要，由作战清单/雷达按需读取。
    """
    ind = str(industry or "").strip()
    if not ind:
        return []
    try:
        if streak is not None and int(streak) < 2:
            return []          # 龟兔案例只适用于二板及以上
    except (TypeError, ValueError):
        pass

    def _hit(theme: str) -> bool:
        for tok in re.split(r"[ /·、（）()「」]+", str(theme or "")):
            tok = tok.strip()
            if len(tok) >= 2 and (tok in ind or ind in tok):
                return True
        return False

    out: list[dict[str, Any]] = []
    for g in PAIR_GROUPS:
        if not _hit(g.get("theme", "")):
            continue
        rabbits = [m["name"] for m in g.get("members", []) if m.get("role") == "rabbit"]
        turtles = [m["name"] for m in g.get("members", []) if m.get("role") == "turtle"]
        out.append({
            "theme": g.get("theme"),
            "title": g.get("title"),
            "rabbit": " / ".join(rabbits) or None,
            "turtle": " / ".join(turtles) or None,
            "note": "历史案例只作规律提示：同题材同身位优先找能成交的换手乌龟",
        })
        if len(out) >= limit:
            break
    return out


def payload() -> dict[str, Any]:
    panel = _panel()
    live = _live_snapshot()
    live_members: dict[str, Any] = live.get("members") or {}
    groups: list[dict[str, Any]] = []
    for g in PAIR_GROUPS:
        members = []
        for m in g["members"]:
            met = _member_metrics(panel, m["ts_code"], g["anchor"])
            row: dict[str, Any] = {**m, "role_cn": ROLE_CN.get(m["role"], m["role"]), **met}
            if g.get("live"):
                # 今日日线尚未收盘：不编造日线指标，改挂实时快照
                obs = live_members.get(m["ts_code"]) or {}
                row["live_obs"] = obs
                row["available"] = False
                row["reason"] = "今日日线未收盘；下面用涨停池实时快照核对"
                row["proxy"] = obs.get("proxy", "无实时快照")
            members.append(row)
        groups.append({**g, "members_payload": members})
    extras = []
    for code, name in EXTRA_CODES.items():
        d = panel.filter(pl.col("ts_code") == code).sort("trade_date")
        c = d["close"].to_numpy().astype(float)
        zt, st = leader_ladder._zt_flags(code, c)
        dates = [str(x) for x in d["trade_date"].to_list()]
        segs = []
        i = 0
        while i < len(c):
            if zt[i]:
                j = i
                while j + 1 < len(c) and zt[j + 1]:
                    j += 1
                if j - i + 1 >= 2:
                    segs.append({"start": dates[i], "end": dates[j], "boards": j - i + 1})
                i = j + 1
            else:
                i += 1
        extras.append({"ts_code": code, "name": name, "board_segments": segs[-6:],
                       "note": "用户提到但与本批配对无同日启动窗口，只列真实连板段"})
    return {
        "available": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "panel_last": str(panel["trade_date"].max()),
        "role_cn": ROLE_CN,
        "definitions": {
            "rabbit": "一字兔子：封板无开板、换手极低、封单大；涨停排板**确定买不进**",
            "turtle": "厂字乌龟：盘中开板换手后回封（分时形似「厂」），换手充分、排板可成交",
            "proxy": "仓库无分时/委托队列，日线换手率 + 快照封单/开板次数只是弱代理，"
                     "与用户按分时形态标注的角色冲突时两者并列、不改写",
            "win": "「胜出」分两种：单波连板高度/次日晋级（一字倾向占优但买不进）；"
                   "跨波 40 日趋势龙头持续性（用户案例里乌龟占优）",
        },
        "groups": groups,
        "extras": extras,
        "live_snapshot": live,
        "thesis": _thesis(),
        "bull_periods": [
            {"name": x["name"], "start": x["start"], "end": x["end"], "kind": x["kind"]}
            for x in leader_ladder.CASE_PERIODS
        ],
        "caveats": [
            "用户给的都是「乌龟胜出」的事后选中案例，无失败案例，属选择性样本，不能当统计证据；"
            "全市场对照见命题检验。",
            "「一字 / 厂字」是分时形态，仓库只有日线与当日快照；换手率代理在多处与用户标注角色冲突"
            "（如 2026-10-09 标注乌龟的龙版涨停日均换手 8.3%，标注兔子的欢瑞 26.3%），已原样并列。",
            "「盘子偏大」缺流通市值字段，只用封单衰减、开板次数、换手率近似，不构成市值判定。",
            "「叠加的洋」「光」为题材/字眼推测，未获确认，仅按用户原话登记。",
            "「2026-11-12 两组」的日线基准是用户标注当天的留档，不计算当日之后的单波/跨波指标；"
            "实时核对见 live_snapshot（其帧时刻已显式标注，不代表收盘状态）。",
            "本模块**不产生买卖信号、不改任何阈值、不做样本外推断**，只把新收获登记进悟空真经口径。",
        ],
        # 2026-11-18 验收新增：显式声明这份 payload 的「数据截止」与「面板来源」，
        # 供前端/调用方判断可信度。旧版只有 panel_last，没有来源，无法分辨
        # 「数据旧」是 by design 的回放锚点还是读取路径错了。
        "data_asof": {
            "panel_last": str(panel["trade_date"].max()),
            "panel_source": _panel_source(),
            "anchor_dates": sorted({g["anchor"] for g in PAIR_GROUPS}),
            "live_snapshot_day": (live or {}).get("day"),
            "live_snapshot_last_frame": (live or {}).get("last_ts"),
            "note": "anchor 是用户标注的历史案例日（不回写成今天）；"
                    "live_snapshot 是当日涨停池留档，帧时刻可能早于收盘。",
        },
    }


if __name__ == "__main__":
    r = payload()
    print("panel_last", r["panel_last"], "groups", len(r["groups"]))
    for g in r["groups"]:
        print("---", g["title"])
        for m in g["members_payload"]:
            if not m.get("available"):
                print("  ", m["name"], "N/A", m.get("reason"))
                continue
            print(f"   {m['name']:<8}{m['role_cn']:<8} 代理={m['proxy']:<18} "
                  f"单波{m['single']['max_streak']}板 峰值{m['single']['peak_gain']*100:+.0f}% "
                  f"换手{m['single']['zt_day_turnover_mean']}% | "
                  f"40日{m['wide']['fwd_40d']*100:+.0f}% ALIVE{m['wide']['alive_days']}天")
    lv = r["live_snapshot"]
    print("live", lv.get("day"), lv.get("first_ts"), "~", lv.get("last_ts"),
          "frames", lv.get("n_frames"))
    for c, v in (lv.get("members") or {}).items():
        print("  ", c, v["name"], v.get("available"), v.get("proxy"),
              (v.get("last") or {}).get("turnover"), (v.get("last") or {}).get("broken"))
    print("thesis", r["thesis"]["status"])
    for x in r["thesis"]["checks"]:
        print("  ", x["verdict"], x["item"])
