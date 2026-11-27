"""实时模拟账本 · B1 悟空真经（用户 2026-11-12 / 11-13 裁定①）

模块边界（用户 2026-11-15 裁定「方案 B」）
------------------------------------------------
本模块就是 **E5「悟空真经·二板」这一条策略的实现本体**，不是"通用的模拟账本"。

  * **选股逻辑（唯一权威口径，用户 2026-11-16 裁定）**：
    情绪闸门通过后，在题材内找「一字二板（兔子）」→ 买同题材其他二板（乌龟）；
    **无组合则当日不开仓**，不硬凑、不退回「全部二板」旧口径。
    实现见 :func:`collect_guitu_targets`（L975+）；本文件旧注释若出现
    「全部二板」均为历史描述，一律以此条为准。
  * **不要把它改成"只处理外部传入的票"**：那样 E5 就失去了自己的选股，
    变成纯结算器。若将来要改成策略驱动，需先给 E5 一个独立的选股函数。

由此产生的必然结果（必须让用户看见，不能当成 bug 藏起来）：
  休市 / 无留档帧 / 情绪闸门拦截 / 题材内没有「一字兔+其他二板」组合时，
  本模块当日 **0 委托** —— 这是「策略今日无信号」的正确表达，不是故障。

口径（唯一，不与其他模块共用买点）
------------------------------------------------
> 买点＝二板当日涨停瞬间挂涨停价排板。
> 断板后不持有，人不可能全部吃下利润，吃一部分就行了，增加成功率，
> 断板当天收盘前卖出。

即：**二板封板瞬间以涨停价限价委托排队 → 首个未涨停日（断板）当日收盘卖出**。

用户 2026-11-13 裁定：「悟空·二板的核心就是二板，乌龟和兔子」；
2026-11-16 进一步裁定选股为「题材内有一字兔 → 买同题材其他二板」。
兔子（一字二板）是**选股触发条件**（锚点），同时按 Q037 买不进；
乌龟（厂字/换手二板）是被买入的一侧，排板可能成交。
**角色标注不参与成交判定**——能否成交仍由 Q037 逐帧独立判定。

精简版只做 B1；4 板+ 对照与其他策略不在本终端内。

真实性约束（不可协商）
------------------------------------------------
1. 数据源只有两类真实落盘：`data/live/<day>/pools/pools.jsonl`（东财涨停池
   原样留档）与 `data/panel_raw.parquet`（真实日线）。不合成、不插值、不补齐。
2. **不假设排到就成交**。按东财 Q037：涨停价委托单，涨停打开且卖一有单才成交；
   不开板不成交。用留档帧的 `broken`（当日开板次数）判定，缺字段即「不可判定」，
   不可判定 = 不建仓。
3. 断板日与收盘价必须来自**已落盘的真实日线**；日线未覆盖时状态是
   `pending_daily`，绝不拿盘中最新价冒充收盘价结算。
4. 停牌 = 静默跨日（日线无行则跳过），并在 caveat 中说明。
5. 到右端仍连板 = `censored` 右删失，不算盈利也不算亏损。
6. 成本单一口径：万 0.84 免五（用户 2026-11-23 裁定），见 `costs.DEFAULT`。
7. 本模块只写本地 JSON 账本，**永不向任何账户/接口发送交易指令**。

状态机
------------------------------------------------
委托单  queued_unfilled ──(当日出现 broken>0)──> filled ──> position(h holding)
        unknown ────────(补齐 broken 字段)──────> 同上        │
                                                             ├─ 首个未涨停日 → closed
                                                             ├─ 日线未覆盖 → holding
                                                             └─ 到末端仍连板 → censored
单向迁移、按 `day|code|B1` 幂等，重复 sync 不重复开仓、不重复结算。
"""
from __future__ import annotations

import copy
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from backend import archive, costs, feature_engine, guitu_roles, paths, queue_match

ROOT = Path(__file__).resolve().parents[1]
# 数据与代码分离：所有数据路径统一由 backend.paths 解析（支持 WUKONG_DATA_HOME）。
# 这些模块级常量仍保留（测试用 monkeypatch 重定向写入点），但初值来自 paths，
# 不再自己拼 ROOT / "data"。
DATA = paths.data_dir()
LIVE_DIR = DATA / "live"
PANEL = paths.panel_raw_file()
PAPER = DATA / "paper"
STATE_FILE = PAPER / "sim_state.json"
SNAPSHOT = PAPER / "live_sim.json"

_lock = threading.Lock()

STRATEGY = "B1 悟空真经·二板排板"
ENTRY_RULE = "二板封板瞬间挂涨停价限价委托排队（不默认成交）"
# 2026-11-25 裁定：卖点按「买入日收盘是否仍炸板」分两条路径；盘中信号卖价
# 仍处研究阶段（阈值未做样本外验证），时间与兜底价是当前实际执行口径。
EXIT_RULE = (
    "两路径自适应卖点：买入日收盘仍炸板→次日10:00前卖；买入日收盘封住→断板日收盘前卖；"
    "盘中按板块幅度/冰点动态阈值监控急跌与冲高回落，未触发到时间窗口兜底"
)

# 成本单一口径（用户 2026-11-23 裁定）：万 0.84 免五，见 costs.DEFAULT。
# 实际成本率由 costs.DEFAULT.round_trip_rate() 计算，不在此硬编码百分比。
COST_LABEL = "万0.84免五（佣金0.0084%/边·无最低5元·印花0.05%·过户0.001%·滑点15bp/边）"

# ============================ 仓位管理（用户 2026-11-23 裁定） ============================
# 「钱只有一百万，仓位自行管理，冰点就降仓位」
#
# 设计：
#   总资金 CAPITAL = 100 万
#   常态单笔仓位    = 总资金 × 10%  = 10 万（与既有「单笔≤10%」口径一致）
#   冰点降仓       = 单笔仓位 × size_factor（L5→0.30、L4→0.50）
#   总仓上限       = 总资金 × 80%（留 20% 现金缓冲，不满仓）
#
# 为什么需要总仓上限：冰点日有多个题材同时命中时，单笔各自按 10% 下单
# 可能超出总资金。这里用「可用资金 / 剩余候选数」做均分，保证不超杠杆。
# 仓位规划与账户熔断已解耦至 backend.position_sizing，此处原名导出保持 100% 兼容
from backend.position_sizing import (
    BOARD_20CM_RISK_MULT,
    CAPITAL_YUAN,
    GRADE_BASE_PCT,
    LOT_SIZE,
    MAINLINE_BOOST_MULT,
    MAX_DAILY_ORDERS,
    MAX_GROSS_PCT,
    MAX_PER_THEME_ORDERS,
    MAX_SINGLE_MAINLINE_PCT,
    MAX_SINGLE_PCT,
    POSITION_PCT,
    POSITION_SIZE_YUAN,
    account_circuit_breaker,
    clean_trades,
    is_invalid_record,
    plan_orders,
    position_size,
)


def sanitize_invalid_positions(state: dict[str, Any]) -> int:
    """一次性与幂等冲销历史 bug 脏单：将 invalid_historical_bug 持仓移出持仓池释放资金，并隔离脏交割单。"""
    orders = state.get("orders") or {}
    invalid_oids = {
        str(oid) for oid, o in orders.items()
        if isinstance(o, dict) and o.get("invalid_historical_bug")
    }
    positions = state.get("positions") or {}
    voided = state.setdefault("voided_positions", {})
    removed = 0
    for pid in list(positions.keys()):
        pos = positions.get(pid)
        if not isinstance(pos, dict):
            continue
        if pos.get("invalid_historical_bug") or str(pid) in invalid_oids or str(pos.get("id") or "") in invalid_oids:
            vp = dict(pos)
            vp["status"] = "voided"
            vp["invalid_historical_bug"] = True
            vp["void_reason"] = vp.get("void_reason") or "历史时序穿越脏单无损冲销回退现金"
            voided[pid] = vp
            positions.pop(pid, None)
            removed += 1
    trades = state.get("trades")
    if isinstance(trades, list):
        closed_parent_ids = {
            str(pid) for pid, pos in (state.get("positions") or {}).items()
            if isinstance(pos, dict) and pos.get("status") == "closed"
        }
        closed_parent_ids.update(
            str(t.get("id")) for t in trades
            if isinstance(t, dict) and t.get("id") and not t.get("is_partial_leg")
        )
        clean_list = []
        for t in trades:
            if is_invalid_record(t, invalid_oids):
                continue
            # 清理历史双记账遗留的已合并 PARTIAL 子单
            if isinstance(t, dict) and t.get("is_partial_leg") and str(t.get("parent_id") or "") in closed_parent_ids:
                continue
            clean_list.append(t)
        if len(clean_list) != len(trades):
            state["trades"] = clean_list
    return removed


def _fill_size_fields(row: dict[str, Any]) -> None:
    """给一条委托/持仓/成交补 `qty` / `notional`（历史账本回填，2026-11-17 Fix A）。

    只补**字段**，不重算已写入的 `r_net` / `cost_rate`——历史结论保持可追溯；
    新结算走 `settle()` 的 notional 口径。缺 entry_price 时如实留空。
    已结算成交只有在「按新 notional 重算的 cost_rate 与已记录值一致」时才补
    `cost_notional`，否则留空并写明原因，绝不改历史数字。
    """
    entry = row.get("entry_price")
    if row.get("notional") is None or row.get("qty") is None:
        if row.get("notional") is None and row.get("qty") is not None:
            try:
                row["notional"] = round(float(entry) * int(float(row["qty"])), 2)
                row.setdefault(
                    "notional_basis",
                    "历史回填：entry_price×qty（仅补字段，不重算已结算 r_net）")
            except (TypeError, ValueError):
                pass
        else:
            qty, notional = position_size(entry)
            if qty is not None:
                row["qty"] = qty
                row["notional"] = notional
                row.setdefault(
                    "notional_basis",
                    "历史回填：单笔上限 10 万元口径（仅补字段，不重算已结算 r_net）")
    if row.get("status") == "closed" and row.get("cost_notional") is None:
        try:
            n = row.get("notional")
            cr = row.get("cost_rate")
            if n and cr is not None:
                from backend import costs as _costs
                if abs(_costs.DEFAULT.round_trip_rate(float(n))
                       - float(cr)) <= 1e-9:
                    row["cost_notional"] = float(n)
                    row.setdefault(
                        "cost_note",
                        "历史回填：已记录 cost_rate 与 notional 口径一致，"
                        "补 cost_notional（未重算 r_net）")
                else:
                    row["cost_notional_note"] = (
                        "历史 cost_rate 与当前 notional 口径不一致，"
                        "保持 cost_notional=None，不重算历史 r_net")
        except (TypeError, ValueError):
            pass


def _backfill_size_fields(state: dict[str, Any]) -> int:
    """把 qty/notional 回填到全部委托、持仓与成交；返回补齐的条数。"""
    n = 0
    rows: list[dict[str, Any]] = []
    rows.extend((state.get("orders") or {}).values())
    rows.extend((state.get("positions") or {}).values())
    rows.extend(state.get("trades") or [])
    for r in rows:
        before = (r.get("qty"), r.get("notional"))
        _fill_size_fields(r)
        n += int((r.get("qty"), r.get("notional")) != before)
        if r.get("status") == "closed" and not r.get("exit_price_basis"):
            r["exit_price_basis"] = "断板当日收盘价（真实日线）"
            r.setdefault("exit_blocked", False)
            r.setdefault("exit_blocked_days", 0)
    return n


CAVEATS: list[str] = [
    "委托单状态依据东财 Q037：涨停价委托，涨停打开且卖一有单才成交；不开板不成交。"
    "成交价取委托价（涨停价），队列位置按封单尾部，逐笔封板时刻不可得，以留档帧时刻近似。",
    "卖点口径（2026-11-25 裁定）：买入日收盘仍炸板→次日 10:00 前卖；买入日收盘封住→断板日收盘前卖。"
    "盘中快跌≥2%/冲高回落≥3%信号优先（参数研究中、未做样本外验证）；未触发按时间边界兜底价。"
    "历史回放仍按「断板当日收盘价」口径，与实盘账本不是同一卖法，不可直接对照。",
    "涨停池留档在 2026-11-13 中午前只保留按连板高度降序的 top-30，二板可能被截断；"
    "此后帧带全量 `all` 字段。历史截断帧已标注，不可回溯补全。",
    "停牌无日线行时静默跨日；若右端到最新日线仍连续涨停，标记 censored（右删失），不计盈亏。",
    "成本单一口径：万 0.84 免五（佣金 0.0084%/边、无最低 5 元、印花 0.05%、过户 0.001%、滑点 15bp/边），"
    "见 costs.DEFAULT；历史已结算记录保留当时口径，不重算。",
    "本账本为纯本地模拟：零账号、零费用，不向雪球/东财/券商发送任何下单或撤单指令。",
    "精简版终端只有 B1 一条策略；不再保留 E1/E3/E4 对照口径，也不存在「全策略合计」。",
    "选股口径（用户 2026-11-16 裁定）：情绪闸门通过后，题材内有一字二板（兔子）→ "
    "买同题材其他二板（乌龟）；无组合则当日不开仓，不硬凑、不退回「全部二板」旧口径。"
    "一字兔子按 Q037 买不进，角色标注不参与成交判定。",
    guitu_roles.PROXY_NOTE,
    guitu_roles.HEAVY_NOTE,
    "龟兔配对按同日 + 涨停池 industry 近似题材分组，industry 不是严格题材分类；"
    "组内未同时出现兔子与乌龟时如实记为未配对，不硬凑、不用案例库倒推当日结论。",
    "后复权层经 2026-11-13 审计存在污染，本账本只使用**不复权收盘价**与涨停池真实价格，"
    "不涉及复权收益；但涨停判定仍受交易所舍入与 ST 历史状态影响（见 README 已知限制）。",
]


# ---------------------------------------------------------------------------
# 纯函数：留档帧 → 二板委托单素材
# ---------------------------------------------------------------------------

def _pool_rows(frame: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """取该帧涨停池全量行；返回 (rows, truncated)。

    `all` 为 2026-11-13 新增的全量留档；老帧只有 `top`（head(30)）。
    """
    zt = frame.get("zt") or {}
    if isinstance(zt.get("all"), list) and zt.get("all_complete"):
        return list(zt["all"]), False
    return list(zt.get("top") or []), True


def collect_two_board(frames: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """从当日全部留档帧汇总 streak==2 标的的排板素材（纯函数）。

    首次出现帧 = 排板时刻近似；当日所有帧 `broken` 的最大值 = 全天是否开板过
    （不能只看首帧：首帧未开板、尾盘炸板开板后按 Q037 应成交）。
    """
    notes: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    n_trunc = 0
    for fr in frames:
        rows, truncated = _pool_rows(fr)
        if truncated:
            n_trunc += 1
        for r in rows:
            try:
                if int(r.get("streak") or 0) != 2:
                    continue
            except (TypeError, ValueError):
                continue
            raw_code = str(r.get("ts_code") or "").strip()
            # 2026-11-14 修复（审计 2.9）：空代码 zfill(6) 会变成 "000000"，
            # 生成幽灵委托并污染涨停池计数。缺失/非数字代码直接跳过。
            if not raw_code or not raw_code.isdigit():
                continue
            code = raw_code.zfill(6)
            name = str(r.get("name") or "")
            if feature_engine.is_st(name):
                continue
            broken = r.get("broken")
            try:
                bi = int(broken) if broken is not None else None
            except (TypeError, ValueError):
                bi = None
            g = out.get(code)
            if g is None:
                out[code] = {
                    "ts_code": code, "name": name,
                    "first": r, "last": r,
                    "frame_ts": fr.get("ts"), "last_frame_ts": fr.get("ts"),
                    "broken_max": bi, "broken_seen": bi is not None,
                    "n_frames": 1, "industry": r.get("industry"),
                    "turnover": r.get("turnover"),
                    # 封板时刻字段自 2026-11-13 午后全量 `all` 帧才有；
                    # 老帧缺该列，故在全日所有帧里取首个/末个非空值。
                    "first_seal": r.get("first_seal"),
                    "last_seal": r.get("last_seal"),
                }
            else:
                g["last"] = r
                g["last_frame_ts"] = fr.get("ts")
                g["n_frames"] += 1
                # 末帧的换手 / 行业 / 封单更接近全日状态，供龟兔角色代理判定
                g["turnover"] = r.get("turnover")
                g["industry"] = r.get("industry") or g.get("industry")
                if g.get("first_seal") is None and r.get("first_seal") is not None:
                    g["first_seal"] = r.get("first_seal")
                if r.get("last_seal") is not None:
                    g["last_seal"] = r.get("last_seal")
                if bi is not None:
                    g["broken_seen"] = True
                    g["broken_max"] = max(g["broken_max"] or 0, bi)
    if n_trunc:
        notes.append(
            f"当日 {n_trunc}/{len(frames)} 帧为 top-30 截断留档（无全量 `all`），"
            "二板可能不全；全量留档自 2026-11-13 午后生效，历史帧不可回溯补全。"
        )
    return out, notes


#: 换手率过滤的观察阈值（2026-11-25 实证研究得出，**仅用于分级展示，不参与选股**）。
#: 依据：首帧换手 ≤3% 封住率 80.6%、≤5% 65.2%、>12% 仅 20%（77 样本/6 日）。
SEAL_TURNOVER_GOOD = 3.0      # 优：封住率 80%+
SEAL_TURNOVER_OK = 8.0        # 中：封住率 50-65%
SEAL_FIRST_SEAL_GOOD = 93000  # 首封 ≤09:30 封住率 88.9%


def _seal_quality_fields(first: dict[str, Any], g: dict[str, Any]
                         ) -> dict[str, Any]:
    """封板质量观察字段（只读分级，不参与选股/成交判定）。

    2026-11-25 用户提出用换手率提高「封住不开板」概率。实证（77 样本）
    显示首帧换手率与封住率强相关，但样本量不足以改策略，故先只展示：
      turn_at_seal  封板瞬间换手（首帧可得，**不是**未来函数）
      seal_quality  good / ok / weak（按换手 + 首封时刻综合）
    """
    tv = first.get("turnover")
    try:
        tvf = float(tv) if tv is not None else None
    except (TypeError, ValueError):
        tvf = None
    fs = guitu_roles._hhmmss(first.get("first_seal"))
    reasons = []
    if tvf is None:
        quality = "unknown"
        reasons.append("缺换手率")
    elif tvf <= SEAL_TURNOVER_GOOD:
        quality = "good"
    elif tvf <= SEAL_TURNOVER_OK:
        quality = "ok"
    else:
        quality = "weak"
    if tvf is not None:
        reasons.append(f"首帧换手 {tvf:.2f}%")
    if fs is not None:
        early = fs <= SEAL_FIRST_SEAL_GOOD
        reasons.append(f"首封 {str(int(fs)).zfill(6)[:2]}:{str(int(fs)).zfill(6)[2:4]}"
                       + ("（早盘）" if early else "（非早盘）"))
        if quality == "good" and not early:
            quality = "ok"          # 换手好但封得晚 → 降一档
    return {
        "turn_at_seal": round(tvf, 4) if tvf is not None else None,
        "first_seal_hhmmss": int(fs) if fs is not None else None,
        "seal_quality": quality,
        "seal_quality_note": " · ".join(reasons) or "无可用字段",
        "seal_quality_basis": ("实证观察值（77 样本/6 日）：换手≤3% 封住率 80.6%、"
                               "≤5% 65.2%、>12% 仅 20%；首封≤09:30 封住率 88.9%。"
                               "**仅展示，不参与选股过滤**，样本不足以调参上线。"),
    }


def build_order(day: str, code: str, g: dict[str, Any], *,
                size_factor: float = 1.0,
                size_yuan: float | None = None) -> dict[str, Any]:
    """生成**挂单**（不判成交）。

    2026-11-16 修复未来函数：旧实现用 `g["broken_max"]`（全天最大开板次数）
    判定能否成交 —— 那是收盘后才知道的数据，等于挂单时就未卜先知。
    现在这里只产生「挂单」，成交由 :func:`advance_fills` 逐帧推进决定，
    每次只用当前帧及之前的信息。

    Args:
        size_factor: 仓位系数（冰点小仓试错，见 `ice_gate.size_factor`）。
                     只缩放单笔买入金额，不改变选股、成交判定与买卖点。
        size_yuan: 显式指定本笔买入金额（由 :func:`plan_orders` 按总资金
                   与总仓上限分配）。给出时优先于 `size_factor` 的默认计算。
    """
    first = g["first"]
    b0 = first.get("broken")
    try:
        b0 = int(b0) if b0 is not None else None
    except (TypeError, ValueError):
        b0 = None
    probe = {
        "price": first.get("price"),
        "seal_fund": g["last"].get("seal_fund"),
        # 只给「挂单时刻」的开板数，不给全天最大值
        "broken": b0,
    }
    q = queue_match.classify_live(probe)
    # 挂单起点：有开板数字段才能推进判定；否则不可判定
    status = "queued" if b0 is not None else "unknown"

    # 2026-11-15：用个股分时复核「首封时刻 / 开板次数」
    # 涨停池快照的 `broken` 只是一个汇总数；分时能还原**何时**封、**几次**开，
    # 这直接决定排板能否成交（Q037：开板才有机会）。
    # 取不到分时 → 保留原判定，不猜。
    intraday_check = None
    try:
        from backend import intraday_signal
        lu = first.get("price")
        if lu:
            d = intraday_signal.fetch(code)
            if d.get("available"):
                intraday_check = intraday_signal.judge_b1(
                    d["points"], float(lu))
                # 分时与快照交叉核对：不一致时以分时为准并留痕
                if intraday_check.get("available") and g["broken_seen"]:
                    snap_broken = int(g["broken_max"] or 0)
                    isd_open = int(intraday_check.get("open_times") or 0)
                    if isd_open != snap_broken:
                        intraday_check["mismatch"] = (
                            f"快照开板 {snap_broken} 次，分时还原 {isd_open} 次；"
                            "以分时为准（分钟级近似）")
                    # 分时说「从未封板」而快照说涨停 → 数据冲突，标记不可判定
                    if not intraday_check.get("ever_sealed"):
                        intraday_check["conflict"] = (
                            "涨停池标记为涨停，但分时全天未封住涨停价")
    except Exception as e:  # noqa: BLE001
        intraday_check = {"available": False,
                          "reason": f"{type(e).__name__}: {e}"}
    # 龟兔角色：只用真实留档字段做近似代理，纯标注，不参与上面的成交判定
    role_row = {
        "broken": g["broken_max"] if g["broken_seen"] else None,
        "first_seal": g.get("first_seal"),
        "last_seal": g.get("last_seal"),
        "turnover": g["turnover"],
        "seal_fund": g["last"].get("seal_fund"),
    }
    # 2026-11-17：把价格换算成「数量 × 成交额」，成本模型才能计入最低 5 元佣金。
    # 2026-11-23：冰点降仓 + 总资金 100 万预算；size_yuan 显式给出时优先。
    factor = max(0.0, min(1.0, float(size_factor or 1.0)))
    budget = (float(size_yuan) if size_yuan is not None and float(size_yuan) > 0
              else POSITION_SIZE_YUAN * factor)
    qty, notional = position_size(first.get("price"), size_yuan=budget)
    return {
        "id": f"{day}|{code}|B1",
        "day": day, "strategy": STRATEGY,
        "ts_code": code, "name": g["name"],
        "industry": g["industry"], "turnover": g["turnover"],
        # 交易题材（来自题材池的板块级归并），与申万行业分开存：
        # 行业是分类归属，题材才是「同题材有一字兔 → 买其他二板」的分组依据。
        "guitu_theme": g.get("guitu_theme"),
        "guitu_rabbits": g.get("guitu_rabbits"),
        "guitu_rabbit_codes": g.get("guitu_rabbit_codes"),
        "rabbit_snapshot_at_entry": g.get("rabbit_snapshot_at_entry"),
        "seal_fund_at_entry": first.get("seal_fund"),
        "entry_date": day, "entry_price": first.get("price"),
        # 数量与成交额：让 settle 的成本口径能计入最低 5 元佣金
        "qty": qty, "notional": notional,
        "size_factor": factor,
        "size_budget_yuan": round(budget, 2),
        "notional_basis": (f"预算 {budget:,.0f} 元（总资金 {CAPITAL_YUAN:,.0f}"
                           f"×{POSITION_PCT:.0%}×系数 {factor:.2f}）："
                           f"qty=floor({budget:,.0f}/(价×100))×100"),
        "entry_frame_ts": g["frame_ts"],
        "entry_rule": ENTRY_RULE, "exit_rule": EXIT_RULE,
        "queue": q,
        "intraday": intraday_check,
        "status": status,
        "guitu": guitu_roles.classify_role(role_row),
        "n_frames": g["n_frames"],
        # 2026-11-25 用户提出「买点加换手率考量以提高封板不开板概率」。
        # 先做**只读监控**（不参与选股/过滤，避免 77 样本就改策略）：
        # turn_at_seal = 首次封板帧的换手（事前可得，非未来函数）
        # seal_quality = 按实证阈值给的分级，仅作观察
        **_seal_quality_fields(first, g),
        # 事后复盘证据（不参与成交判定）
        "broken_max": g["broken_max"],
        # 挂单时刻的开板数 = 逐帧推进的起点
        "broken_at_entry": b0,
        # 成交留痕（由 advance_fills 填写）
        "fill_frame_ts": None,
        "fill_price": None,
        "fill_reason": None,
        "frame_last_ts": g["last_frame_ts"],
        "caveats": ["委托单状态为规则判定，不是真实成交回报；不开板/不可判定一律不建仓。"],
    }


def _frame_maps(frames: list[dict[str, Any]]) -> list[tuple[str, dict[str, int | None]]]:
    """把每帧的涨停池压成 {code: broken}，供逐帧推进用（只算一次）。"""
    out: list[tuple[str, dict[str, int | None]]] = []
    for fr in frames:
        rows, _ = _pool_rows(fr)
        m: dict[str, int | None] = {}
        for r in rows:
            raw = str(r.get("ts_code") or "").strip()
            if not raw or not raw.isdigit():
                continue
            c = raw.zfill(6)
            try:
                br = int(r.get("broken")) if r.get("broken") is not None else None
            except (TypeError, ValueError):
                br = None
            m[c] = br
        out.append((str(fr.get("ts") or ""), m))
    return out


def advance_fills(state: dict[str, Any],
                  frames: list[dict[str, Any]],
                  day: str,
                  *,
                  cancel_feedback: dict[str, float] | None = None,
                  ice_level: str | None = None,
                  market_vol_ratio: float | None = None,
                  evo_high_turn_delta: float | None = None,
                  evo_rotten_delta: float | None = None) -> int:
    """逐帧推进当日挂单的成交判定，返回本次新成交笔数。

    **无未来函数**：每张挂单只从「挂单帧之后」开始扫描，
    每次只用该帧当时的开板数 / 是否仍在涨停池。
    """
    maps = _frame_maps(frames)
    ts_pos = {ts: i for i, (ts, _) in enumerate(maps)}
    prefix = f"{day}|"
    n_new = 0
    for oid, o in list(state.get("orders", {}).items()):
        if not oid.startswith(prefix):
            continue
        # 2026-11-24 用户裁定：矛盾单以精确模拟为准。
        # 2026-11-25 审计 P0 加固：只有**数据完整**的精确模拟（有封单额/成交额，
        # 即 queue_sim 有 seal_fund_at_entry_yi）才能推翻 filled；
        # 盘中数据不全造成的 unknown/假阴性不锁定，后续帧继续重判。
        prior_sim = o.get("queue_sim") or {}
        sim_has_data = prior_sim.get("seal_fund_at_entry_yi") is not None
        if (o.get("status") == "filled"
                and prior_sim.get("status") == "queued_unfilled"
                and sim_has_data):
            o.update(status="queued_unfilled",
                     fill_price=None, fill_frame_ts=None, fill_round=None,
                     fill_confidence="none",
                     fill_reason="[以精确模拟为准] " + str(prior_sim.get("detail") or "封单未吃穿"))
            continue
        if o.get("status") in ("filled", "cancelled"):
            continue                                  # 已成交/已撤单不回退
        b0 = o.get("broken_at_entry")
        if b0 is None:
            if o.get("status") not in ("filled", "cancelled"):
                o["status"] = "unknown"
            continue
        i0 = ts_pos.get(str(o.get("entry_frame_ts") or ""))
        if i0 is None:
            o["status"] = "unknown"
            o["fill_reason"] = "挂单帧不在留档帧序列中，无法推进"
            continue
        code = o.get("ts_code")

        # 2026-11-16：优先用**完整排板模拟**（逐轮开板累积消耗队列 + 严格时序主动撤单）。
        # 拿不到封单/成交额/换手时，退回二元判定（开板即成交）并标 unverified。
        try:
            from backend import fill_verify as _fv
            sim = _fv.simulate_fill(day, str(code),
                                    str(o.get("entry_frame_ts") or ""),
                                    frames,
                                    entry_price=o.get("entry_price"),
                                    enable_cancel=True,
                                    rabbit_codes=o.get("guitu_rabbit_codes"),
                                    cancel_feedback=cancel_feedback,
                                    ice_level=ice_level,
                                    market_vol_ratio=market_vol_ratio,
                                    evo_high_turn_delta=evo_high_turn_delta,
                                    evo_rotten_delta=evo_rotten_delta)
            o["queue_sim"] = sim
            if sim.get("status") == "filled":
                o.update(status="filled",
                         fill_frame_ts=sim.get("fill_ts"),
                         fill_price=sim.get("fill_price") or o.get("entry_price"),
                         fill_reason=sim.get("detail"),
                         fill_confidence=sim.get("confidence"),
                         fill_round=sim.get("fill_round"))
                n_new += 1
                continue
            if sim.get("status") == "cancelled":
                o.update(status="cancelled",
                         fill_price=None,
                         fill_frame_ts=None,
                         cancel_frame_ts=sim.get("cancel_ts"),
                         cancel_kind=sim.get("cancel_kind"),
                         cancel_reason=sim.get("cancel_reason"),
                         fill_reason=sim.get("detail"),
                         fill_confidence="none")
                continue
            if sim.get("status") == "queued_unfilled":
                o.update(status="queued_unfilled",
                         fill_reason=sim.get("detail"),
                         fill_confidence="none")
                continue
        except Exception:  # noqa: BLE001
            pass

        # 2026-11-24 用户裁定：矛盾单**以精确模拟为准**。
        # 若已有 queue_sim 且它明确说 queued_unfilled（封单没吃穿），
        # 就不再走二元判定（开板即成交）——二元判定是数据缺失时的粗略回退，
        # 不能推翻后来算出的精确结论（如江顺科技：二元说成交、精确说未排到）。
        prior_sim = o.get("queue_sim") or {}
        if prior_sim.get("status") == "queued_unfilled":
            o.update(status="queued_unfilled",
                     fill_reason=prior_sim.get("detail") or "精确模拟：封单未吃穿，排板未成交",
                     fill_confidence="none")
            continue

        filled = False
        for i in range(i0 + 1, len(maps)):
            ts, m = maps[i]
            if code not in m:
                # 已不在涨停池 = 炸板 → 涨停价委托可成交
                o.update(status="filled", fill_frame_ts=ts,
                         fill_price=o.get("entry_price"),
                         fill_confidence="unverified",
                         fill_reason=f"{ts} 已不在涨停池（炸板），按开板即成交判定"
                                     "（缺队列数据，未核验）")
                filled = True
                n_new += 1
                break
            br = m[code]
            if br is not None and int(br) > int(b0):
                o.update(status="filled", fill_frame_ts=ts,
                         fill_price=o.get("entry_price"),
                         fill_confidence="unverified",
                         fill_reason=(f"{ts} 开板次数 {b0}→{br}，按开板即成交判定"
                                      "（缺封单/成交额数据，未做队列核验）"))
                filled = True
                n_new += 1
                break
        if not filled:
            o["status"] = "queued_unfilled"
            o["fill_reason"] = "全天未开板，封单未释放，排板未成交"
            o.setdefault("fill_confidence", "none")
    return n_new


def poll_queued_cancel_watch(state: dict[str, Any], day: str,
                             *, quotes_map: dict[str, dict[str, Any]] | None = None,
                             now_hm: str | None = None,
                             market_vol_ratio: float | None = None,
                             ice_level: str | None = None,
                             cancel_feedback: dict[str, float] | None = None,
                             evo_high_turn_delta: float | None = None,
                             evo_rotten_delta: float | None = None) -> list[dict[str, Any]]:
    """盘中轻量高频盯盘：仅对当日排队中的极少数挂单（≤5只）检查实时买一封单与主动撤单信号。"""
    prefix = f"{day}|"
    queued = [
        (oid, o) for oid, o in (state.get("orders") or {}).items()
        if oid.startswith(prefix) and o.get("status") in ("queued", "queued_unfilled")
    ]
    if not queued:
        return []
    need_codes: set[str] = set()
    for _oid, o in queued:
        c = str(o.get("ts_code") or "").zfill(6)
        if c.isdigit():
            need_codes.add(c)
        for rc in (o.get("guitu_rabbit_codes") or []):
            r6 = str(rc).zfill(6)
            if r6.isdigit():
                need_codes.add(r6)
    qmap = quotes_map
    if qmap is None and need_codes:
        try:
            from backend import feeds
            qmap = feeds.batch_quotes(sorted(need_codes), ttl=5.0)
        except Exception:  # noqa: BLE001
            qmap = {}
    qmap = qmap or {}
    from backend import fill_verify as _fv
    out: list[dict[str, Any]] = []
    hm = now_hm or _cn_now_hm()
    for oid, o in queued:
        c = str(o.get("ts_code") or "").zfill(6)
        rq = {str(rc).zfill(6): qmap.get(str(rc).zfill(6)) for rc in (o.get("guitu_rabbit_codes") or [])}
        res = _fv.check_live_quote_cancel(
            o, qmap.get(c), rabbit_quotes=rq, ts=hm,
            market_vol_ratio=market_vol_ratio,
            ice_level=ice_level,
            cancel_feedback=cancel_feedback,
            evo_high_turn_delta=evo_high_turn_delta,
            evo_rotten_delta=evo_rotten_delta,
        )
        if res.get("live_seal_yuan") is not None:
            o["live_last_seal"] = res["live_seal_yuan"]
            o["live_peak_seal"] = max(float(o.get("live_peak_seal") or 0.0), float(res["live_seal_yuan"]))
        o["cancel_watch"] = res
        if res.get("should_cancel") and not res.get("too_late"):
            o.update(
                status="cancelled",
                fill_price=None,
                fill_frame_ts=None,
                cancel_frame_ts=hm,
                cancel_kind=res.get("kind"),
                cancel_reason=res.get("reason"),
                fill_reason=f"{hm} 高频盯盘仍在涨停且未开板 → 主动撤单（{res.get('reason')}）",
                fill_confidence="none",
            )
        out.append({"id": oid, "ts_code": c, "name": o.get("name") or "", **res})
    return out


# ---------------------------------------------------------------------------
# 纯函数：日线 → 断板结算
# ---------------------------------------------------------------------------

def _pct_from_prev(close: Any, prev_close: Any) -> float | None:
    """(今收/前收 - 1)；任一价格不可用返回 None。"""
    try:
        c = float(close)
        pc = float(prev_close)
    except (TypeError, ValueError):
        return None
    if not (c > 0 and pc > 0):
        return None
    return c / pc - 1.0


def _is_limit_down(close: Any, prev_close: Any, lim_pct: float | None) -> bool | None:
    """该日收盘是否跌停封死。

    Args:
        lim_pct: 涨跌幅（0.10 / 0.20 / 0.30）。None = 不做判定（返回 None）。

    判据（2026-11-17 修复，lead 指定）：`pct_chg <= -(limit_pct - 0.3%)`，
    主板即收盘跌幅 ≤ -9.7%。留 0.3 个百分点容差吸收交易所舍入/停牌复牌
    等异常，避免把 -9.99% 漏判成可成交。

    Returns:
        True=跌停（按收盘价卖不出去）、False=可成交、None=缺前收无法判定。
    """
    if lim_pct is None or not (float(lim_pct) > 0):
        return None
    pct = _pct_from_prev(close, prev_close)
    if pct is None:
        return None
    return bool(pct <= -(float(lim_pct) - 0.003))


def find_exit(entry_date: str,
              daily: list[dict[str, Any]],
              *,
              code: str | None = None,
              lim_pct: float | None = None,
              ) -> tuple[dict[str, Any] | None, str]:
    """按真实日线找**可成交的**断板卖点。

    返回 (行, 状态)：closed / pending_daily / censored。

    - 首个未涨停日 = 断板日；若断板日**收盘跌停封死**，按收盘价卖不出去，
      顺延到下一个「收盘不跌停」的交易日（仍按收盘价；涨不涨停不影响卖出）；
    - 顺延信息写在返回行上：`exit_blocked` / `exit_blocked_days` /
      `exit_blocked_dates` / `exit_candidate_date`；
    - 若日线还没覆盖到可成交日 → 返回 (最后一次跌停行, "pending_daily")，
      **不伪造价格**（settle 会标 holding 并写明在等下一可成交日）。

    2026-11-17 修复（lead 验收 P1）：旧实现只判「不是涨停」就按收盘价卖，
    跌停封死也算成交 —— 单边乐观偏差。跌停封死当日真实卖盘排不到，
    只能等次日；本函数把这个约束显式化。

    2026-11-15 修复（G-0062，功能性死亡 bug）
    ----------------------------------------
    旧实现 `[d for d in daily if str(d["date"]) > entry_date]` 做**字符串比较**：
    持仓 entry_date 是 `'20261113'`（无分隔符），日线是 `'2026-11-13'`，
    逐字符比较 `'2' < '6'` → `'2026-11-14' > '20261113'` 为 False，
    于是 after 恒为空、永远返回 pending_daily。
    现统一走 `dates.norm_day()` 归一成整数再比，两种格式入参都能正确判定。
    """
    from backend import dates

    entry_norm = dates.norm_day(entry_date)
    if not entry_norm:
        return None, "pending_daily"
    after = [d for d in daily
             if dates.is_after(d.get("date"), entry_norm)]
    if not after:
        return None, "pending_daily"

    # 涨跌停幅度：优先显式传入；否则按代码板块；两者都没有 → 不做跌停判定
    # （保持旧行为，避免影响只关心「断板日」的纯函数调用）。
    lim = lim_pct
    if lim is None and code:
        try:
            lim = float(feature_engine.limit_pct(str(code)))
        except Exception:  # noqa: BLE001
            lim = None

    idx_of = {str(d.get("date")): i for i, d in enumerate(daily)}

    def _prev_close(row: dict[str, Any]) -> float | None:
        i = idx_of.get(str(row.get("date")))
        if i is None or i <= 0:
            return None
        try:
            v = float(daily[i - 1].get("close"))
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    # 1) 首个未涨停日 = 断板日（原口径）
    cand_i = None
    for k, row in enumerate(after):
        if not bool(row.get("zt")):
            cand_i = k
            break
    if cand_i is None:
        return None, "censored"

    cand = after[cand_i]
    blocked: list[str] = []
    j = cand_i
    cur = cand
    while True:
        ld = _is_limit_down(cur.get("close"), _prev_close(cur), lim)
        if ld is not True:
            out = dict(cur)
            out["exit_blocked"] = bool(blocked)
            out["exit_blocked_days"] = len(blocked)
            out["exit_blocked_dates"] = list(blocked)
            out["exit_limit_down"] = ld
            out["exit_candidate_date"] = str(cand.get("date"))
            return out, "closed"
        # 跌停封死：按收盘价卖不出去，顺延
        blocked.append(str(cur.get("date")))
        j += 1
        if j >= len(after):
            info = dict(cur)
            info["exit_blocked"] = True
            info["exit_blocked_days"] = len(blocked)
            info["exit_blocked_dates"] = list(blocked)
            info["exit_limit_down"] = True
            info["exit_candidate_date"] = str(cand.get("date"))
            return info, "pending_daily"
        cur = after[j]



def _cn_now_hm() -> str:
    """A 股口径当前时刻 'HH:MM'，用于盘中兜底卖出时间门控。"""
    try:
        from backend import market_clock
        tz = market_clock.TZ_CN
        return (datetime.now(tz) if tz else datetime.now()).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%H:%M")


def _compute_atr5_from_daily(daily: list[dict[str, Any]] | None) -> float | None:
    """用最近 5 根真实日线收盘涨跌幅均值估算短期波动率（防 20cm/高波标的被固定阈值洗出）。"""
    if not daily or len(daily) < 3:
        return None
    tail = daily[-6:]
    moves: list[float] = []
    for i in range(1, len(tail)):
        try:
            c0 = float(tail[i - 1].get("close") or 0.0)
            c1 = float(tail[i].get("close") or 0.0)
            if c0 > 0 and c1 > 0:
                moves.append(abs(c1 / c0 - 1.0))
        except (TypeError, ValueError):
            continue
    return round(sum(moves) / len(moves), 4) if moves else None


def _hm_to_min(hm: str | None) -> int | None:
    s = str(hm or "").strip()
    if "T" in s:
        s = s.split("T", 1)[1][:5]
    elif " " in s:
        s = s.split(" ", 1)[1][:5]
    if len(s) >= 5 and s[2] == ":" and s[:2].isdigit() and s[3:5].isdigit():
        return int(s[:2]) * 60 + int(s[3:5])
    return None


def _is_intraday_limit_down_locked(code: str | None,
                                   px: float | None,
                                   prev_close: float | None,
                                   *,
                                   signal_time: str | None = None,
                                   latest_time: str | None = None) -> bool:
    """盘中跌停封死流动性核验（时空对齐，绝不拿 10:15 实时买一量去倒签 09:32 历史卖点）。"""
    if not code or px is None or not prev_close or prev_close <= 0:
        return False
    try:
        c6 = str(code).zfill(6)
        lim = float(feature_engine.limit_pct(c6))
        ld_px = round(float(prev_close) * (1.0 - lim), 2)
        if float(px) > ld_px + 0.011:
            return False
        # 时空对齐校验：若信号发生在历史分钟（距最新分钟/当前时刻 > 3 分钟），
        # 绝不查当前 single_quote() 造成时空错位，仅按触发价已打至跌停价判锁死
        sm = _hm_to_min(signal_time)
        lm = _hm_to_min(latest_time)
        if sm is not None and lm is not None and abs(lm - sm) > 3:
            return float(px) <= ld_px + 0.005
        from backend import feeds
        sq = feeds.single_quote(c6, ttl=5.0)
        if not sq:
            return False
        b1v = sq.get("bid1_vol")
        if b1v is not None and float(b1v) <= 0.0:
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def build_sell_plan(pos: dict[str, Any], daily: list[dict[str, Any]],
                    *, code: str | None = None,
                    ice_level: str | None = None,
                    now_hm: str | None = None,
                    atr5: float | None = None,
                    rabbit_weak: bool = False,
                    rabbit_strong: bool | None = None,
                    rabbit_nuclear: bool = False,
                    peer_turtle_strong: bool = False) -> dict[str, Any]:
    """按用户 2026-11-25 裁定生成卖出计划（自适应分钟级信号 + 时间门控兜底）。

    两条路径：
      A. 买入日**收盘仍炸板**（未回封）→ 次日 10:00 前卖
      B. 买入日收盘封住（未炸板）    → 首个断板日收盘前卖

    优先级：窗口内触发自适应信号（急跌/冲高回落/竞价抢跑/尾盘防核按钮）→ 按信号价卖；
            盘中未到时间边界（A<10:00 / B<14:55）绝不提前按兜底价强平，继续持仓监控。
    """
    from backend import dates, sell_rules

    entry = float(pos.get("entry_price") or 0.0)
    entry_day = str(pos.get("entry_date") or "")
    if not (entry > 0) or not entry_day:
        return {"available": False, "reason": "缺买入价或买入日"}

    # --- 买入日归类：收盘时是否仍炸板 ---
    entry_row = next((d for d in daily
                      if dates.norm_day(d.get("date")) == dates.norm_day(entry_day)),
                     None)
    if entry_row is None:
        return {"available": False, "reason": f"日线无买入日 {entry_day}"}
    sealed_at_close = bool(entry_row.get("zt"))
    broken = pos.get("broken_max")
    if broken is None:
        broken = (pos.get("order") or {}).get("broken_max")
    kind = sell_rules.classify_entry_day(broken, sealed_at_close)
    eff_atr5 = atr5 if atr5 is not None else _compute_atr5_from_daily(daily)
    th = sell_rules.adaptive_thresholds(
        code, atr5=eff_atr5, rabbit_weak=rabbit_weak, ice_level=ice_level,
    )

    plan: dict[str, Any] = {
        "available": True,
        "engine": "adaptive",
        "entry_kind": kind,
        "entry_sealed_at_close": sealed_at_close,
        "entry_broken_times": broken,
        "thresholds": th,
    }

    def _run_adaptive(pts: list[dict[str, Any]], *,
                      prev_close: float | None,
                      fallback_price: float | None,
                      fallback_time: str,
                      end: str | None = None,
                      pending_reason: str | None = None,
                      check_intraday_ld: bool = False) -> dict[str, Any]:
        open_px = float(pts[0]["price"]) if pts and pts[0].get("price") else None
        dec = sell_rules.decide_adaptive(
            kind, points=pts, code=code,
            prev_close=prev_close, open_price=open_px,
            atr5=eff_atr5, rabbit_weak=rabbit_weak,
            rabbit_strong=rabbit_strong,
            rabbit_nuclear=rabbit_nuclear,
            peer_turtle_strong=peer_turtle_strong,
            ice_level=ice_level,
            fallback_price=fallback_price, fallback_time=fallback_time,
            end=end,
        )
        latest_pt_t = str(pts[-1].get("t") or "") if pts else None
        if (check_intraday_ld and dec.get("action") == "sell"
                and _is_intraday_limit_down_locked(
                    code, dec.get("price"), prev_close,
                    signal_time=dec.get("signal_time"), latest_time=latest_pt_t)):
            return {
                "action": "hold",
                "price": None,
                "kind": "exit_blocked_intraday",
                "thresholds": th,
                "partial_signal": dec.get("partial_signal"),
                "reason": (
                    f"盘中封死跌停（触发时刻 {dec.get('signal_time') or latest_pt_t} 价 {dec.get('price')}），"
                    "无承接流动性不可成交，顺延等待开板或次日"
                ),
            }
        # P0-1 修复：传入 code 与 end，不吞异常；pending 态保留 partial_signal / partial_profit 不覆盖抹除
        psig = dec.get("partial_signal")
        if psig is None and dec.get("action") == "hold" and kind == "sealed":
            psig = sell_rules.find_partial_profit_signal(
                pts, code=code, prev_close=prev_close, end=end,
            )
        if psig:
            dec = {**dec, "partial_signal": psig, "partial_profit": psig}
        if dec.get("action") == "hold" and pending_reason and fallback_price is None:
            dec = {**dec, "action": "hold", "price": None, "kind": "pending",
                   "thresholds": th, "reason": pending_reason}
        return dec

    cur_hm = now_hm or _cn_now_hm()
    after = [d for d in daily
             if dates.is_after(d.get("date"), dates.norm_day(entry_day))]
    if not after:
        # 2026-11-25 盘中：买入日的次日就是今天，但当日日线还没落盘。
        today = _cn_today_str()
        is_t1 = bool(today and dates.is_after(today, dates.norm_day(entry_day)))
        prev_c = float(entry_row.get("close") or 0) or entry
        if kind == "broke_and_stayed" and is_t1:
            # A 路径：卖出窗口是「今天 10:00 前」；未到 10:00 不触发兜底卖出
            plan["window"] = {
                "date": today, "until": sell_rules.FALLBACK_TIME_A,
                "rule": "买入日收盘仍炸板 → 次日 10:00 前卖出（盘中自适应）",
                "intraday": True,
            }
            pts = _minute_points(today, code, live=True)
            win_pts = [p for p in pts
                       if str(p.get("t")) <= sell_rules.FALLBACK_TIME_A]
            due_fallback = cur_hm >= sell_rules.FALLBACK_TIME_A
            fallback_px = ((float(win_pts[-1]["price"]) if win_pts else _live_price(code))
                           if due_fallback else None)
            plan["decision"] = _run_adaptive(
                win_pts, prev_close=prev_c,
                fallback_price=fallback_px,
                fallback_time=f"{today} 10:00 前",
                end=sell_rules.FALLBACK_TIME_A,
                pending_reason=f"盘中监控急跌/冲高回落信号（当前 {cur_hm} < 10:00，未触发不提前兜底）",
                check_intraday_ld=True,
            )
            plan["minute_source"] = "1min_live" if pts else "live_quote"
            return plan
        if kind == "sealed" and is_t1:
            # B 路径：今天可能就是断板日（实时价已离开涨停）；未到 14:55 不触发收盘兜底
            px_now = _live_price(code)
            lu = _limit_up_of(code, entry_row,
                              prev_close=prev_c)
            if px_now and lu and px_now < lu - 1e-9:
                plan["window"] = {
                    "date": today, "until": "收盘",
                    "rule": "买入日未炸板 → 今日断板，收盘前卖出（盘中自适应）",
                    "intraday": True,
                }
                pts = _minute_points(today, code, live=True)
                due_fallback = cur_hm >= "14:55"
                plan["decision"] = _run_adaptive(
                    pts, prev_close=prev_c,
                    fallback_price=(px_now if due_fallback else None),
                    fallback_time=f"{today} 收盘前（现价）",
                    pending_reason=f"今日断板，盘中监控自适应卖点（当前 {cur_hm} < 14:55，未触发不提前收盘兜底）",
                    check_intraday_ld=True,
                )
                plan["minute_source"] = "1min_live" if pts else "live_quote"
                return plan
            plan["window"] = {"date": None, "until": "断板日收盘",
                              "rule": "买入日未炸板 → 等待首个断板日"}
            plan["decision"] = {
                "action": "hold", "price": None, "kind": "pending",
                "reason": (f"今日实时价 {px_now or '—'} 仍封涨停"
                           f"（涨停价 {lu or '—'}），尚未断板；断板当日收盘前卖出")}
            return plan
        plan.update(available=False, reason="买入日之后暂无日线（T+1 未到）")
        return plan

    if kind == "broke_and_stayed":
        # A：次一交易日 10:00 前
        nxt = after[0]
        nxt_day = str(nxt.get("date"))
        plan["window"] = {"date": nxt_day, "until": sell_rules.FALLBACK_TIME_A,
                          "rule": "买入日收盘仍炸板 → 次日 10:00 前卖出（自适应）"}
        pts = _minute_points(nxt_day, code)
        win_pts = [p for p in pts if str(p.get("t")) <= sell_rules.FALLBACK_TIME_A]
        fallback_px = (float(win_pts[-1]["price"]) if win_pts
                       else float(nxt.get("open") or nxt.get("close") or 0) or None)
        plan["decision"] = _run_adaptive(
            win_pts, prev_close=float(entry_row.get("close") or 0) or entry,
            fallback_price=fallback_px,
            fallback_time=f"{nxt_day} 10:00 前",
            end=sell_rules.FALLBACK_TIME_A,
        )
        plan["minute_source"] = "1min_archive" if pts else "daily_fallback"
        return plan

    if kind == "sealed":
        # B：首个断板日收盘前（复用 find_exit 定位断板日，含跌停顺延）
        exit_row, status = find_exit(entry_day, daily, code=code)
        if status == "closed" and exit_row is not None:
            exit_day = str(exit_row.get("date"))
            plan["window"] = {"date": exit_day, "until": "收盘",
                              "rule": "买入日未炸板 → 首个断板日收盘前卖出（自适应）"}
            pts = _minute_points(exit_day, code)
            prev_c = _prev_close_before(daily, exit_day)
            plan["decision"] = _run_adaptive(
                pts, prev_close=prev_c,
                fallback_price=float(exit_row.get("close") or 0) or None,
                fallback_time=f"{exit_day} 收盘",
            )
            plan["minute_source"] = "1min_archive" if pts else "daily_fallback"
            return plan

        # 2026-11-25 盘中：日线还没覆盖断板日，但今天可能就是断板日。
        # 若 today 已经在 daily 里且上面 find_exit 返回 censored，说明今日日线已落盘且确认仍封涨停，
        # 绝不可再走盘中判定（否则会把 today 的收盘价当成昨收误算明日涨停价导致误平仓）。
        today = _cn_today_str()
        has_today_bar = bool(
            today and any(dates.norm_day(d.get("date")) == dates.norm_day(today) for d in daily)
        )
        if has_today_bar and status == "censored":
            last_close = daily[-1].get("close") if daily else None
            plan["window"] = {"date": None, "until": "断板日收盘",
                              "rule": "买入日未炸板 → 等待首个断板日"}
            plan.update(decision={
                "action": "hold", "price": None, "kind": "pending",
                "reason": (f"今日（{today}）收盘价 {last_close or '—'} 仍封涨停（日线已确认连板），"
                           "尚未断板；断板当日收盘前卖出")})
            return plan
        if today and not has_today_bar and dates.is_after(today, dates.norm_day(entry_day)):
            px_now = _live_price(code)
            prev_c = float(daily[-1].get("close") or 0) if daily else entry
            lu = _limit_up_of(code, None, prev_close=prev_c)
            if px_now and lu and px_now < lu - 1e-9:      # 已离开涨停 → 今天断板
                plan["window"] = {"date": today, "until": "收盘",
                                  "rule": "买入日未炸板 → 今日断板，收盘前卖出（盘中自适应）",
                                  "intraday": True}
                pts = _minute_points(today, code, live=True)
                due_fallback = cur_hm >= "14:55"
                plan["decision"] = _run_adaptive(
                    pts, prev_close=prev_c,
                    fallback_price=(px_now if due_fallback else None),
                    fallback_time=f"{today} 收盘前（现价）",
                    pending_reason=f"今日断板，盘中监控自适应卖点（当前 {cur_hm} < 14:55，未触发不提前收盘兜底）",
                    check_intraday_ld=True,
                )
                plan["minute_source"] = "1min_live" if pts else "live_quote"
                return plan
            # 仍封在涨停 → 今天还没断板，继续持有等待
            plan.update(decision={
                "action": "hold", "price": None, "kind": "pending",
                "reason": (f"今日实时价 {px_now or '—'} 仍封涨停"
                           f"（涨停价 {lu or '—'}），尚未断板；断板当日收盘前卖出")})
            plan["window"] = {"date": None, "until": "断板日收盘",
                              "rule": "买入日未炸板 → 等待首个断板日"}
            return plan

        plan.update(decision={"action": "hold", "price": None,
                              "kind": "undecidable",
                              "reason": f"尚未出现可结算断板日（{status}）"})
        return plan

    plan.update(decision={"action": "hold", "price": None, "kind": "undecidable",
                          "reason": "买入日炸板状态不可判定（缺 broken/封板字段）"})
    return plan


def _cn_today_str() -> str:
    """A 股口径今天（YYYYMMDD）。"""
    try:
        from backend import market_clock
        tz = market_clock.TZ_CN
        return (datetime.now(tz) if tz else datetime.now()).strftime("%Y%m%d")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y%m%d")


def _limit_up_of(code: str | None, entry_row: dict[str, Any] | None,
                 prev_close: float | None = None) -> float | None:
    """买入日涨停价。

    优先取 entry_row 里的 limit_up；否则用「前收 × (1 + 板块幅度)」推算
    （日线 panel 只有 OHLC，没有 limit_up 列 —— 2026-11-25 实测）。
    """
    if entry_row:
        for k in ("limit_up",):
            v = entry_row.get(k)
            try:
                f = float(v)
                if f > 0:
                    return f
            except (TypeError, ValueError):
                pass
    # 兜底：前收 ×(1+幅度)
    try:
        pc = float(prev_close) if prev_close else None
        if pc and pc > 0 and code:
            from backend import feature_engine
            return round(pc * (1.0 + float(feature_engine.limit_pct(str(code)))), 2)
    except Exception:  # noqa: BLE001
        return None
    return None


def _prev_close_before(daily: list[dict[str, Any]], day: str) -> float | None:
    """某日的前一交易日收盘价（用于推算涨停价）。"""
    from backend import dates
    nd = dates.norm_day(day)
    prev = None
    for d in daily:
        if dates.is_after(d.get("date"), nd):
            break
        prev = d.get("close")
    try:
        return float(prev) if prev else None
    except (TypeError, ValueError):
        return None


def _live_price(code: str | None) -> float | None:
    """取该票最新实时价（优先单股快查 feeds.single_quote，避免重拉全市场 5580 只）。"""
    if not code:
        return None
    try:
        from backend import feeds
        sq = feeds.single_quote(str(code).zfill(6), ttl=5.0)
        if sq and sq.get("price") and float(sq["price"]) > 0:
            return float(sq["price"])
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend import feeds
        q = feeds.all_quotes(force=False)
        r = q.filter(pl.col("ts_code") == str(code).zfill(6))
        if r.height:
            px = r["price"][0]
            return float(px) if px and px > 0 else None
    except Exception:  # noqa: BLE001
        return None
    return None


def _minute_points(day: str, code: str | None,
                   *, live: bool = False) -> list[dict[str, Any]]:
    """取某日某票的分钟序列（优先 1min 含 VWAP/量能，失败回落 quotes 5min 快照）。"""
    if not code:
        return []
    c = str(code).zfill(6)
    out: list[dict[str, Any]] = []
    try:
        from backend import minute_flow
        if live:
            ser = minute_flow.live_series(c, str(day), persist=True)
            if ser:
                for it in ser:
                    t = str(it.get("t") or "")
                    px = it.get("price")
                    if px and px > 0 and ":" in t:
                        out.append({
                            "t": t,
                            "price": float(px),
                            "avg_price": it.get("avg_price"),
                            "volume": it.get("volume"),
                        })
                if out:
                    return sorted(out, key=lambda x: x["t"])
        persisted = minute_flow.load_persisted_minute(c, str(day))
        if persisted:
            for it in persisted:
                t = str(it.get("t") or "")
                px = it.get("price")
                if px and px > 0 and ":" in t:
                    out.append({
                        "t": t,
                        "price": float(px),
                        "avg_price": it.get("avg_price"),
                        "volume": it.get("volume"),
                    })
            if out:
                return sorted(out, key=lambda x: x["t"])
        ser = minute_flow.minute_series(c, str(day))
        if ser:
            for t, px, _amt in ser:
                if px and px > 0:
                    hh, mm = str(t)[:2], str(t)[2:4]
                    out.append({"t": f"{hh}:{mm}", "price": float(px)})
    except Exception:  # noqa: BLE001
        out = []
    if out:
        return sorted(out, key=lambda x: x["t"])
    # 回落：当日 quotes 留档（5 分钟一帧）——逐帧/合并归档统一读取
    try:
        frames = archive.read_frames(str(day), "quotes")
        if frames is not None and frames.height:
            cols = [x for x in ("frame", "ts_code", "price")
                    if x in frames.columns]
            sub = frames.select(cols).filter(pl.col("ts_code") == c)
            for r in sub.iter_rows(named=True):
                px = r.get("price")
                stem = str(r.get("frame") or "")
                if px and px > 0 and len(stem) == 6:
                    out.append({"t": f"{stem[:2]}:{stem[2:4]}",
                                "price": float(px)})
    except Exception:  # noqa: BLE001
        return []
    return sorted(out, key=lambda x: x["t"])


def _close_by_plan(pos: dict[str, Any], plan: dict[str, Any],
                   decision: dict[str, Any],
                   daily: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """按 sell_plan 的决策价真正平仓（2026-11-25 用户要求及时显示卖出）。

    - 卖出价 = decision.price（信号价 / 时间兜底价）
    - 成本仍走 costs.DEFAULT（万0.84免五单一口径）
    - 写入 exit_source / signal 说明，前端据此区分「信号卖出」与「兜底卖出」
    """
    from backend import costs

    p = dict(pos)
    entry = float(p.get("entry_price") or 0.0)
    exit_px = float(decision.get("price") or 0.0)
    if not (entry > 0 and exit_px > 0):
        p["status"] = "holding"
        p["state_note"] = "卖出计划价格非法，保持持有（不编造价格）"
        return p

    notional = p.get("notional") or p.get("order_amount") or None
    qty = p.get("qty")
    try:
        notional = float(notional) if notional else None
    except (TypeError, ValueError):
        notional = None
    try:
        qty = int(float(qty)) if qty else None
    except (TypeError, ValueError):
        qty = None
    if notional is None and qty and entry > 0:
        notional = round(entry * qty, 2)
        p["notional"] = notional

    cost_rate = costs.DEFAULT.round_trip_rate(notional)
    gross = exit_px / entry - 1.0
    win = plan.get("window") or {}
    kind = decision.get("kind")  # signal / fallback

    # 2026-11-25 审计 P2：盘中卖出曾硬编码 exit_blocked=False，抹掉跌停顺延事实。
    # 这里不改卖出价（盘中信号/兜底价已定格），但把「卖出日是否跌停封死」
    # 按真实日线如实留痕；日线未落盘则标 unknown，不再假装"未受阻"。
    blocked: list[str] = []
    break_ld: bool | None = None
    daily_rows = daily or []
    exit_date = str(win.get("date") or _cn_today_str())
    exit_row = next((d for d in daily_rows
                     if str(d.get("date")).replace("-", "") ==
                     exit_date.replace("-", "")), None)
    if exit_row is not None:
        code = str(p.get("ts_code") or "") or None
        idx = daily_rows.index(exit_row)
        prev_close = None
        if idx > 0:
            try:
                pv = float(daily_rows[idx - 1].get("close"))
                prev_close = pv if pv > 0 else None
            except (TypeError, ValueError):
                pass
        from backend import feature_engine as _fe
        lim = None
        if code:
            try:
                lim = float(_fe.limit_pct(code))
            except Exception:  # noqa: BLE001
                lim = None
        break_ld = _is_limit_down(exit_row.get("close"), prev_close, lim)
        if break_ld is True:
            blocked = [str(exit_row["date"])]
    blocked_note = (
        "跌停日按盘中信号价卖出（无逐笔承接保证）" if blocked else
        "卖出日跌停状态不可判定（日线未落盘）" if break_ld is None else
        "卖出日非跌停，正常成交"
    )

    is_signal = kind not in ("fallback", "undecidable", "pending", None)
    p.update({
        "status": "closed",
        "exit_date": str(win.get("date") or _cn_today_str()),
        "exit_price": round(exit_px, 3),
        "exit_price_basis": (
            f"分钟级自适应信号卖出（{decision.get('signal_time') or '—'}）："
            f"{decision.get('reason')}" if is_signal
            else f"时间边界兜底卖出：{decision.get('reason')}"),
        "exit_source": ("signal" if is_signal else "fallback"),
        "exit_signal_time": decision.get("signal_time"),
        "exit_intraday": bool(win.get("intraday")),
        "gross_return": round(gross, 6),
        "r_net": round(gross - cost_rate, 6),
        "net_return": round(gross - cost_rate, 6),
        "net_basis": "万0.84免五单一口径",
        "cost_model": "backend/costs.py::DEFAULT",
        "cost_rate": round(cost_rate, 6),
        "cost_notional": notional,
        "exit_rule_actual": (
            "买入日收盘仍炸板 → 次日10:00前（信号/兜底）" if
            plan.get("entry_kind") == "broke_and_stayed"
            else "买入日未炸板 → 断板日收盘前（信号/兜底）"),
        "state_note": (f"按卖出规则平仓：{decision.get('reason')}"),
        # 与 settle() 保持字段一致（历史消费者/测试依赖）
        "board_days": 0,
        "exit_blocked": bool(blocked),
        "exit_blocked_days": len(blocked),
        "exit_blocked_dates": list(blocked),
        "exit_blocked_note": blocked_note,
        "last_checked": datetime.now().strftime("%Y-%m-%d"),
        # 卖法对照/环境分组：按卖出日真实日线判定跌停，未知则留 None。
        "break_is_limit_down": break_ld,
        "exit_env": {
            "rule": ("broke_and_stayed_intraday_sell"
                     if plan.get("entry_kind") == "broke_and_stayed"
                     else "sealed_intraday_sell"),
            "signal_time": decision.get("signal_time"),
            "signal_kind": kind,
        },
    })
    p = _merge_partial_into_closed_trade(p)
    return p


def _merge_partial_into_closed_trade(closed_pos: dict[str, Any]) -> dict[str, Any]:
    """将已兑现的半仓锁利与余仓全平按股数/名义金额加权合并，确保 1 笔委托只落 1 条顶层交割单。"""
    p = dict(closed_pos)
    if not p.get("partial_closed") or not isinstance(p.get("partial_leg"), dict):
        return p
    leg1 = dict(p["partial_leg"])
    try:
        q1 = int(float(leg1.get("qty") or 0))
        n1 = float(leg1.get("notional") or 0.0)
        px1 = float(leg1.get("exit_price") or 0.0)
        g1 = float(leg1.get("gross_return") or 0.0)
        r1 = float(leg1.get("net_return") if leg1.get("net_return") is not None else leg1.get("r_net") or 0.0)
        c1 = float(leg1.get("cost_rate") or 0.0)
        q2 = int(float(p.get("qty") or 0))
        n2 = float(p.get("notional") or 0.0)
        px2 = float(p.get("exit_price") or 0.0)
        g2 = float(p.get("gross_return") or 0.0)
        r2 = float(p.get("r_net") if p.get("r_net") is not None else p.get("net_return") or 0.0)
        c2 = float(p.get("cost_rate") or 0.0)
    except (TypeError, ValueError):
        return p
    if not (q1 > 0 and q2 > 0 and (n1 + n2) > 0):
        return p
    tot_q = q1 + q2
    tot_n = round(n1 + n2, 2)
    w_px = round((px1 * q1 + px2 * q2) / tot_q, 3)
    w_gross = round((g1 * n1 + g2 * n2) / tot_n, 6)
    w_net = round((r1 * n1 + r2 * n2) / tot_n, 6)
    w_cost = round((c1 * n1 + c2 * n2) / tot_n, 6)
    leg2_rec = {
        "leg": "final",
        "qty": q2,
        "notional": round(n2, 2),
        "exit_date": p.get("exit_date"),
        "exit_price": px2,
        "exit_signal_time": p.get("exit_signal_time"),
        "exit_source": p.get("exit_source"),
        "gross_return": round(g2, 6),
        "net_return": round(r2, 6),
        "cost_rate": round(c2, 6),
    }
    p["qty"] = tot_q
    p["notional"] = tot_n
    p["cost_notional"] = tot_n
    p["exit_price"] = w_px
    p["gross_return"] = w_gross
    p["r_net"] = w_net
    p["net_return"] = w_net
    p["cost_rate"] = w_cost
    p["legs"] = [leg1, leg2_rec]
    p["exit_price_basis"] = f"分批合并平仓（半仓{q1}股@{px1:.2f} + 余仓{q2}股@{px2:.2f}，加权均价 {w_px:.2f}）"
    return p


def _apply_partial_take_profit(
    pos: dict[str, Any],
    plan: dict[str, Any],
    daily: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """真实双段式分批止盈拆单记账：按 100 股整手兑现 50% 锁利，同步扣减剩余持仓股数与成本（不污染顶层 trades 计数）。"""
    p = dict(pos)
    if p.get("partial_closed") or p.get("status") == "closed":
        return p, None
    dec = (plan.get("decision") or {}) if plan.get("available") else {}
    psig = dec.get("partial_profit") or dec.get("partial_signal")
    if not isinstance(psig, dict) or psig.get("action") != "partial_sell":
        return p, None
    win = (plan.get("window") or {}) if plan.get("available") else {}
    if not win.get("intraday"):
        return p, None

    # 若同一分钟或更早已经触发全平信号，直接整笔全平，绝不拆成两笔同分钟同价单
    if dec.get("action") == "sell" and dec.get("price"):
        psig_t = str(psig.get("t") or "").strip()
        full_t = str(dec.get("signal_time") or "").strip()
        if not full_t and dec.get("kind") == "fallback":
            full_t = "10:00" if plan.get("entry_kind") == "broke_and_stayed" else "14:55"
        if not psig_t or not full_t or psig_t >= full_t:
            return p, None

    entry = float(p.get("entry_price") or 0.0)
    sell_px = float(psig.get("price") or 0.0)
    try:
        qty = int(float(p.get("qty") or 0))
    except (TypeError, ValueError):
        qty = 0
    if not (entry > 0 and sell_px > 0 and qty >= 2 * LOT_SIZE):
        return p, None

    sell_lots = max(1, (qty // LOT_SIZE) // 2)
    sell_qty = sell_lots * LOT_SIZE
    rem_qty = qty - sell_qty
    if sell_qty <= 0 or rem_qty <= 0:
        return p, None

    from backend import costs
    p_notional = round(entry * sell_qty, 2)
    cost_rate = costs.DEFAULT.round_trip_rate(p_notional)
    gross = sell_px / entry - 1.0
    net_r = gross - cost_rate
    exit_date = str(win.get("date") or _cn_today_str())
    child_id = f"{p['id']}|PARTIAL"
    realized_pnl = round(net_r * p_notional, 2)
    child_leg = {
        "leg": "partial",
        "id": child_id,
        "parent_id": p["id"],
        "is_partial_leg": True,
        "strategy": p.get("strategy") or STRATEGY,
        "ts_code": p.get("ts_code"),
        "name": p.get("name"),
        "industry": p.get("industry"),
        "guitu": p.get("guitu"),
        "guitu_theme": p.get("guitu_theme"),
        "entry_date": p.get("entry_date"),
        "entry_price": entry,
        "qty": sell_qty,
        "notional": p_notional,
        "status": "closed",
        "exit_date": exit_date,
        "exit_price": round(sell_px, 3),
        "exit_source": "partial_take_profit",
        "exit_signal_time": psig.get("t"),
        "exit_intraday": True,
        "exit_price_basis": f"分批锁利兑现50%（{psig.get('t') or '—'}）：{psig.get('detail')}",
        "gross_return": round(gross, 6),
        "net_return": round(net_r, 6),
        "r_net": round(net_r, 6),
        "realized_pnl": realized_pnl,
        "cost_model": "backend/costs.py::DEFAULT",
        "cost_rate": round(cost_rate, 6),
        "cost_notional": p_notional,
        "exit_blocked": False,
        "exit_blocked_days": 0,
        "exit_blocked_dates": [],
        "break_is_limit_down": False,
    }
    p["orig_qty"] = qty
    p["orig_notional"] = round(entry * qty, 2)
    p["qty"] = rem_qty
    p["notional"] = round(entry * rem_qty, 2)
    p["partial_closed"] = True
    p["partial_leg"] = child_leg
    p["partial_realized_pnl"] = realized_pnl
    p["partial_exit_date"] = exit_date
    p["partial_note"] = f"已兑现半仓 {sell_qty}股 @ {sell_px:.2f}（落袋 {net_r:+.2%}，余仓 {rem_qty}股博连板/断板）"
    return p, child_leg


def settle(pos: dict[str, Any], daily: list[dict[str, Any]],
           *, ice_level: str | None = None,
           rabbit_weak: bool = False,
           rabbit_strong: bool | None = None,
           rabbit_nuclear: bool = False,
           peer_turtle_strong: bool = False) -> dict[str, Any]:
    """按真实日线更新一笔持仓（纯函数，返回新 dict，不改入参）。"""
    from backend import dates

    p = dict(pos)
    code = str(p.get("ts_code") or "") or None
    # 2026-11-25 用户裁定：卖出按「买入日收盘是否仍炸板」分流，接入自适应卖点引擎。
    try:
        p["sell_plan"] = build_sell_plan(
            p, daily, code=code, ice_level=ice_level,
            rabbit_weak=rabbit_weak,
            rabbit_strong=rabbit_strong,
            rabbit_nuclear=rabbit_nuclear,
            peer_turtle_strong=peer_turtle_strong,
        )
    except Exception as exc:  # noqa: BLE001 - 卖出计划失败不影响既有结算
        p["sell_plan"] = {"available": False,
                          "reason": f"{type(exc).__name__}: {exc}"}
    _plan = p.get("sell_plan") or {}
    # 若盘中触发分批止盈且尚未拆单 → 真实扣减半仓股数与成本，实时记录 partial_realized_pnl（待全平后合并为1条trade）
    p, _child_leg = _apply_partial_take_profit(p, _plan, daily)
    _dec = (_plan.get("decision") or {}) if _plan.get("available") else {}
    _win = (_plan.get("window") or {}) if _plan.get("available") else {}
    if (_dec.get("action") == "sell" and _dec.get("price")
            and _win.get("intraday")):
        return _close_by_plan(p, _plan, _dec, daily)

    exit_row, status = find_exit(str(p["entry_date"]), daily, code=code)
    p["last_checked"] = datetime.now().strftime("%Y-%m-%d")
    if status != "closed" or exit_row is None:
        p["status"] = "holding"
        if exit_row is not None and exit_row.get("exit_blocked"):
            # 断板日跌停封死、后续日线还没覆盖到 → 如实 pending，不给 exit_price
            p.update({
                "exit_blocked": True,
                "exit_blocked_days": int(exit_row.get("exit_blocked_days") or 0),
                "exit_blocked_dates": list(exit_row.get("exit_blocked_dates") or []),
                "exit_candidate_date": exit_row.get("exit_candidate_date"),
                "exit_date": None,
                "exit_price": None,
                "exit_price_basis": ("断板日跌停封死，等待下一可成交交易日；"
                                     "日线未覆盖，不伪造价格"),
                "exit_rule_actual": "跌停不可成交 → 顺延（尚未取到可成交日）",
            })
            p["state_note"] = ("断板日收盘跌停封死，按收盘价卖不出，"
                               "顺延等待下一可成交日；日线未覆盖到该日，"
                               "不猜价格（不给 exit_price）")
        else:
            p["state_note"] = (
                "到最新日线仍连续涨停（右删失），未触发断板" if status == "censored"
                else "买入当日尚未有断板日（T+1 起逐日判定，日线未覆盖后不猜测）"
            )
        return p
    entry = float(p["entry_price"])
    exit_px = float(exit_row["close"])
    if not (entry > 0 and exit_px > 0):
        p["status"] = "holding"
        p["state_note"] = "买入价或断板日收盘价非正，拒绝结算（不编造价格）"
        return p
    gross = exit_px / entry - 1.0
    # 阶段 1：单一成本事实源给出真实 r_net（保留 gross_return 兼容旧消费者）。
    # 2026-11-17：notional 优先取建仓时写入的 price*qty；历史持仓缺字段时用
    # entry_price*qty 兜底；两者都没有才退回「不含最低佣金」并如实标注。
    from backend import costs
    notional = p.get("notional") or p.get("order_amount") or None
    qty = p.get("qty")
    try:
        notional = float(notional) if notional else None
    except (TypeError, ValueError):
        notional = None
    try:
        qty = int(float(qty)) if qty else None
    except (TypeError, ValueError):
        qty = None
    if notional is None and qty and entry > 0:
        notional = round(entry * qty, 2)
        p["notional"] = notional
        p["notional_basis"] = (p.get("notional_basis")
                               or "settle 兜底：entry_price*qty")
    if qty is not None:
        p["qty"] = qty
    cost_rate = costs.DEFAULT.round_trip_rate(notional)
    cost_note = (
        f"万0.84免五：{COST_LABEL}；成本率 {cost_rate:.4%}/笔往返"
        f"（notional={notional:,.2f} 元；免五口径下与成交额无关）"
        if notional is not None else
        f"万0.84免五：{COST_LABEL}；成本率 {cost_rate:.4%}/笔往返（成交额未知）")
    blocked_days = int(exit_row.get("exit_blocked_days") or 0)
    blocked_dates = list(exit_row.get("exit_blocked_dates") or [])
    if blocked_days:
        exit_price_basis = (
            f"断板日 {exit_row.get('exit_candidate_date')} 收盘跌停封死 → "
            f"顺延 {blocked_days} 个交易日至 {exit_row['date']} 收盘价（真实日线）")
    else:
        exit_price_basis = "断板当日收盘价（真实日线）"
    p.update({
        "status": "closed",
        "r_net": round(gross - cost_rate, 6),
        "cost_model": "backend/costs.py::DEFAULT",
        "cost_rate": round(cost_rate, 6),
        "cost_notional": notional,
        "cost_note": cost_note,
        "exit_date": str(exit_row["date"]),
        "exit_price": exit_px,
        "exit_price_basis": exit_price_basis,
        "exit_blocked": bool(blocked_days),
        "exit_blocked_days": blocked_days,
        "exit_blocked_dates": blocked_dates,
        "exit_candidate_date": exit_row.get("exit_candidate_date") or str(exit_row["date"]),
        "exit_rule_actual": exit_price_basis,
        # G-0062 同类：board_days 也用归一化整数比较（旧写法同样踩字符串字典序）
        "board_days": sum(1 for d in daily
                          if dates.is_after(d.get("date"), p["entry_date"])
                          and not dates.is_after(d.get("date"), exit_row["date"])
                          and bool(d.get("zt"))),
        "gross_return": round(gross, 6),
        "net_return": round(gross - cost_rate, 6),  # 单一口径：万0.84免五
        "state_note": ("首个未涨停日以真实收盘价结算；断板后不再持有"
                       if not blocked_days else
                       "断板日跌停封死，顺延到首个可成交交易日收盘价结算"),
    })
    return _merge_partial_into_closed_trade(p)



# ---------------------------------------------------------------------------
# I/O：留档帧 / 日线 / 账本
# ---------------------------------------------------------------------------

def read_frames(day: str) -> list[dict[str, Any]]:
    """读当日涨停池全部留档帧。

    2026-11-25 瘦身：改走 `backend.archive`，明文的 `pools.jsonl` 与
    盘后压缩的 `pools.jsonl.gz` 都能读；调用方无需感知形态。
    """
    from backend import archive
    # 传入模块级 LIVE_DIR（测试会 monkeypatch 它指到临时目录），保持隔离语义。
    return archive.read_pool_frames(day, root=LIVE_DIR)


def load_daily(codes: list[str]) -> dict[str, list[dict[str, Any]]]:
    """按真实日线算逐日是否涨停（只读不复权收盘价，列裁剪）。

    2026-11-13 晚：改用 `feeds.build_panel()` 的合并视图——panel_raw 停在
    T-1 时，收盘后的当日真实日线在 panel_live，只读合并视图已含当日行，
    断板结算不再依赖手工离线重建。数据源仍是真实日线，不合成、不插值。
    """
    if not codes:
        return {}
    try:
        from backend import feeds
        panel = feeds.build_panel()
    except Exception:
        panel = None
    if panel is None or panel.height == 0:
        if not PANEL.is_file():
            return {}
        panel = pl.read_parquet(PANEL)
    df = (panel.lazy()
          .filter(pl.col("ts_code").is_in(codes))
          .select(["ts_code", pl.col("trade_date").cast(pl.Utf8).alias("date"), "close"])
          .sort(["ts_code", "date"])
          .collect())
    out: dict[str, list[dict[str, Any]]] = {}
    if df.height == 0:
        return out
    for code, grp in df.group_by("ts_code", maintain_order=True):
        code = str(code[0] if isinstance(code, tuple) else code)
        c = grp["close"].to_numpy().astype(float)
        zt, _ = feature_engine.zt_flags(code, c)
        out[code] = [{"date": d, "close": float(px), "zt": bool(z)}
                     for d, px, z in zip(grp["date"].to_list(), c, zt)]
    return out


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {"version": 1, "orders": {}, "positions": {}, "trades": [],
                "days": [], "updated_at": None}
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "orders": {}, "positions": {}, "trades": [],
                "days": [], "updated_at": None}
    for k, default in (("orders", {}), ("positions", {}), ("trades", []), ("days", [])):
        if not isinstance(d.get(k), type(default)):
            d[k] = default
    return d


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    from backend import process_guard
    if not process_guard.can_write("live_sim._atomic_write", path):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.json")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 2026-11-16 用户裁定：B1 改为「题材内有一字板 → 买该题材其他二板」
# ---------------------------------------------------------------------------

#: 一字板判定：换手上限（%）。高于此值说明有充分换手，不是"一字"。
ONEWORD_MAX_TURNOVER = 5.0
#: 一字板判定：首封时刻上限（HHMMSS）。09:30:00 及之前视为竞价即封。
ONEWORD_SEAL_CUTOFF = 93000


def _day_dash(day: str) -> str:
    """20261116 -> 2026-11-16（题材池文件名用带横线的日期）。"""
    d = str(day)
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


def _theme_tags(day: str, *, codes: list[str] | set[str] | None = None) -> dict[str, list[str]]:
    """当日涨停股的题材标签（涨停/连板/炸板三池并集 + 历史个股题材记忆兜底，唯一口径）。

    2026-11-26 升级（统一口径 + 消除静态词表与单日缺失盲区）：
    1. 读取同花顺当日三池（zt + lianban + zb）的完整原始标签，交由 `theme_match`
       做「精确轴 → 语义词根 → 同票共现自学习 → 新概念前缀聚类」四层归并；
    2. 对当日缺失标签、仅标「其它」或仅业绩/属性标签的股票，严格因果（date < day）
       回查 `themes.parquet` 近 60 日真实历史标签（例如 11-25 中京电子从「其它」恢复为算力/PCB），
       同时解决早盘题材池刷新滞后问题。
    """
    try:
        from backend import theme_match, theme_pool
        d_dash = _day_dash(day)
        df = theme_pool.load(d_dash)
        raw_map: dict[str, list[str]] = {}
        if df.height > 0:
            pool_order = {"zt": 0, "lianban": 1, "zb": 2}
            rows = sorted(
                df.iter_rows(named=True),
                key=lambda r: pool_order.get(str(r.get("pool") or ""), 9),
            )
            for r in rows:
                c = str(r.get("code") or "").zfill(6)
                if not c or c == "000000":
                    continue
                cur = raw_map.setdefault(c, [])
                for t in (r.get("tags") or []):
                    ts = str(t or "").strip()
                    if ts and ts not in cur:
                        cur.append(ts)
        if not raw_map and not codes:
            return {}
        return theme_match.enrich_stock_tags(
            raw_map,
            before_day=d_dash,
            codes=codes,
            allow_remote=True,
        )
    except Exception:  # noqa: BLE001
        return {}


def collect_all_zt(frames: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """当日全部涨停股的全帧汇总（不限板位），供一字板判定用。

    一字板不一定是二板，所以不能只从 `collect_two_board` 里找。
    """
    out: dict[str, dict[str, Any]] = {}
    for fr in frames:
        rows, _ = _pool_rows(fr)
        for r in rows:
            raw = str(r.get("ts_code") or "").strip()
            if not raw or not raw.isdigit():
                continue
            code = raw.zfill(6)
            name = str(r.get("name") or "")
            if feature_engine.is_st(name):
                continue
            try:
                streak = int(r.get("streak") or 0)
            except (TypeError, ValueError):
                streak = 0
            try:
                br = int(r.get("broken")) if r.get("broken") is not None else None
            except (TypeError, ValueError):
                br = None
            g = out.get(code)
            if g is None:
                out[code] = {
                    "ts_code": code, "name": name, "streak": streak,
                    "first": r, "last": r, "frame_ts": fr.get("ts"),
                    "first_seal": r.get("first_seal"),
                    "broken_max": br, "broken_seen": br is not None,
                    "turnover": r.get("turnover"),
                    "industry": r.get("industry"),
                    "in_last_frame": False,
                }
            else:
                g["last"] = r
                g["streak"] = max(g["streak"], streak)
                if g.get("first_seal") is None and r.get("first_seal") is not None:
                    g["first_seal"] = r.get("first_seal")
                g["turnover"] = r.get("turnover") or g.get("turnover")
                if br is not None:
                    g["broken_seen"] = True
                    g["broken_max"] = max(g["broken_max"] or 0, br)
    # 标记"尾盘是否仍在涨停池"——10:30 封 5 分钟就炸板的票不该算强势
    if frames:
        last_rows, _ = _pool_rows(frames[-1])
        last_codes = {str(r.get("ts_code") or "").zfill(6) for r in last_rows}
        for c in out:
            out[c]["in_last_frame"] = c in last_codes
    return out


def is_oneword(g: dict[str, Any]) -> bool:
    """一字板形态：当日 0 次开板 + 首封 ≤ 09:30:00 + 换手 < 5%。

    注意：这只是「形态」，不含板位。要当兔子还要额外满足 streak==2，
    见 :func:`is_rabbit`（用户 2026-11-16 确认：兔子必须是二板）。
    """
    br = g.get("broken_max")
    if br is None or int(br) != 0:
        return False
    if g.get("broken_seen") and int(g.get("broken_max") or 0) != 0:
        return False
    fs = guitu_roles._hhmmss(g.get("first_seal"))
    if fs is None or fs > ONEWORD_SEAL_CUTOFF:
        return False
    tv = g.get("turnover")
    try:
        tv = float(tv) if tv is not None else None
    except (TypeError, ValueError):
        tv = None
    if tv is None:
        return False
    return tv < ONEWORD_MAX_TURNOVER


def is_rabbit(g: dict[str, Any]) -> bool:
    """兔子 = 一字板形态 + **二板**（streak==2）。

    用户 2026-11-16 确认：「一字板必须限定是「二板」」。
    一字首板（streak==1）只是首板一字，不构成龟兔结构里的锚点。
    """
    if int(g.get("streak") or 0) != 2:
        return False
    return is_oneword(g)


def is_rabbit_at_open(g: dict[str, Any]) -> bool:
    """判定该股在当日首帧（09:25/开盘）是否即为二板一字兔。

    一旦开盘是一字兔，当日永久锁定兔子身份：即便盘中炸板也不得转性为「乌龟」被回溯买入
    （修复 2026-11-27 603386 骏亚科技炸板后时序穿越变身乌龟的 P0 漏洞）。
    """
    if int(g.get("streak") or 0) != 2:
        return False
    first = g.get("first") or {}
    br0 = first.get("broken")
    if br0 is None:
        return False
    try:
        if int(br0) != 0:
            return False
    except (TypeError, ValueError):
        return False
    fs = guitu_roles._hhmmss(first.get("first_seal") or g.get("first_seal"))
    if fs is None or fs > ONEWORD_SEAL_CUTOFF:
        return False
    tv0 = first.get("turnover")
    try:
        tvf = float(tv0) if tv0 is not None else None
    except (TypeError, ValueError):
        tvf = None
    if tvf is None:
        return False
    return tvf < ONEWORD_MAX_TURNOVER


def collect_guitu_targets(
        frames: list[dict[str, Any]], day: str
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """按用户规则选出当日该买的票。

    返回 (code -> 素材, notes)。素材结构与 `collect_two_board` 一致，
    可直接喂给 `build_order`。
    """
    notes: list[str] = []
    if not frames:
        return {}, [f"{day} 无涨停池留档帧；本日不产生任何委托（不补数、不猜测）。"]

    allzt = collect_all_zt(frames)
    tags = _theme_tags(day, codes=list(allzt.keys()))
    if not tags:
        notes.append(
            "当日无题材池标签 → 无法按题材分组，按规则不产生委托"
            "（不退回「全部二板」的旧口径）。")
        return {}, notes

    # ------------------------------------------------------------------
    # 2026-11-24 接入 theme_match 多标签匹配层（用户裁定：同题材同板块
    # = 同一个概念；一只票可属多个板块，旧单标签口径会丢关联）。
    # 组结构升级：
    #   axis 级（题材轴板块）—— 与旧口径同权，直接采用；
    #   tag  级（轴未收录但原始标签相同）—— 同样采用（轴长尾兜底，
    #          当天新标签不用等补轴就能配对）；
    #   industry 级 —— theme_match 已按裁定禁止给无标签票开后门，
    #          此处不采用（不硬凑）。
    # ------------------------------------------------------------------
    from backend import theme_match as TM

    items = [{"code": c, "name": g.get("name") or "",
              "tags": tags.get(c) or [],
              "industry": None,          # 数据真实边界：涨停池无行业
              "streak": int(g.get("streak") or 0)}
             for c, g in allzt.items()]
    grouped = TM.group_by_theme(items)
    # 覆盖率监控（用户关心"会不会漏"）：漏了谁如实进 notes
    cov = TM.coverage_stats(items)
    if cov["unlabeled"] or cov["unmapped"]:
        miss = cov["unlabeled"][:5] + cov["unmapped"][:5]
        notes.append(
            f"题材覆盖监控：涨停 {cov['n']} 只，映射到板块 {cov['n_axis']}"
            f"（{cov['axis_pct']}%）；未映射 {len(cov['unmapped'])}、"
            f"无标签 {len(cov['unlabeled'])}，其中二板/主力:"
            + ("、".join(f"{m['code']}{m.get('name')}" for m in miss[:6])
               or "无"))

    # 题材 -> 该题材下的涨停股（新层：多标签展开 + tag 级兜底）
    from collections import defaultdict
    by_theme: dict[str, list[str]] = defaultdict(list)
    for g in grouped.get("groups") or []:
        if g.get("level") == "axis" or g.get("level") == "tag":
            by_theme[g["theme"]].extend(g.get("codes") or [])

    two_map, _ = collect_two_board(frames)   # 二板素材（含全帧 broken_max 等）

    picked: dict[str, dict[str, Any]] = {}
    hit_themes: list[str] = []
    frozen_themes: list[str] = []
    purity_skips: list[str] = []
    for theme, codes in sorted(by_theme.items()):
        # 多标签展开会重复：一只票同时出现在「机器人」「消费」两组 → 去重保序
        seen: set[str] = set()
        uniq: list[str] = []
        for c in codes:
            if c not in seen and c in allzt:
                seen.add(c)
                uniq.append(c)
        # 用户确认：兔子必须是「一字板 + 二板」，一字首板不算
        # 09:25 开盘一字兔永久锁定兔子身份，即便盘中炸板也不得转性为乌龟被买入
        opened_rabbits = {c for c in uniq if is_rabbit_at_open(allzt[c])}
        ones = [c for c in uniq if is_rabbit(allzt[c])]
        if not ones:
            continue

        others = [c for c in uniq
                  if c not in ones and c not in opened_rabbits
                  and int(allzt[c].get("streak") or 0) == 2]
        if not others:
            continue

        # 汇总兔子 tags 供二级细分簇与主营纯度校验
        rabbit_tags_union: list[str] = []
        for rc in ones:
            for rt in (tags.get(rc) or []):
                if rt not in rabbit_tags_union:
                    rabbit_tags_union.append(rt)

        theme_matched_any = False
        theme_frozen_state: str | None = None
        for c in others:
            # 1) 因果时序校验：必须满足「兔先于或同时于龟封板」(rabbit_fs <= turtle_fs)
            t_fs = guitu_roles._hhmmss(allzt[c].get("first_seal"))
            valid_ones: list[str] = []
            for rc in ones:
                r_fs = guitu_roles._hhmmss(allzt[rc].get("first_seal"))
                if t_fs is not None and r_fs is not None and r_fs > t_fs:
                    continue
                valid_ones.append(rc)
            if not valid_ones:
                continue

            # 2) 严格因果帧切片：只取截至该乌龟候选首封留档帧 <= t_entry_ts 的帧来评估兔子状态（严禁拿 15:00 末帧回溯封杀早盘）
            t_entry_ts = str(allzt[c].get("frame_ts") or "")
            causal_frames = [f for f in frames if (str(f.get("ts") or "") <= t_entry_ts)] if t_entry_ts else frames
            if not causal_frames:
                causal_frames = frames[:1]
            try:
                from backend import rabbit_state as _rs_gate
                r_eval = _rs_gate.evaluate_rabbit_state(valid_ones, causal_frames)
            except Exception:  # noqa: BLE001
                r_eval = {"state": "anchored", "rabbits": []}
            if r_eval.get("state") in ("decaying", "failed"):
                theme_frozen_state = str(r_eval.get("state_cn") or r_eval.get("state"))
                continue

            # 3) 二级细分簇 + 主营纯度校验（拦截大筐拉郎配与跨传统行业蹭概念杂毛）
            purity = TM.check_pair_purity(
                rabbit_tags_union,
                tags.get(c) or [],
                turtle_name=str(allzt[c].get("name") or ""),
                turtle_industry=allzt[c].get("industry"),
                axis_name=theme,
            )
            if not purity.get("pure", True):
                purity_skips.append(f"{c}{allzt[c].get('name') or ''}({theme}:{purity.get('reason')})")
                continue

            theme_matched_any = True
            g = dict(two_map.get(c) or {})
            g.setdefault("ts_code", c)
            g.setdefault("name", allzt[c]["name"])
            g.setdefault("first", allzt[c]["first"])
            g.setdefault("last", allzt[c]["last"])
            g.setdefault("frame_ts", allzt[c].get("frame_ts"))
            g.setdefault("last_frame_ts", frames[-1].get("ts"))
            g.setdefault("broken_max", allzt[c].get("broken_max"))
            g.setdefault("broken_seen", allzt[c].get("broken_seen"))
            g.setdefault("n_frames", 1)
            g.setdefault("industry", allzt[c].get("industry"))
            g.setdefault("turnover", allzt[c].get("turnover"))
            g.setdefault("first_seal", allzt[c].get("first_seal"))
            g.setdefault("last_seal", allzt[c]["last"].get("last_seal"))
            g["guitu_theme"] = theme
            g["guitu_rabbits"] = [allzt[c2]["name"] for c2 in valid_ones]
            g["guitu_rabbit_codes"] = list(valid_ones)
            # 固化决策时刻兔子因果快照（严格使用 t_entry_ts 之前帧的最新封单，绝不偷窥 15:00 收盘帧）
            r_snap_map = {str(x.get("ts_code") or "").zfill(6): x for x in (r_eval.get("rabbits") or [])}
            r_snaps: list[dict[str, Any]] = []
            rabbit_seals: list[float] = []
            for c2 in valid_ones:
                rs_item = r_snap_map.get(c2) or {}
                if rs_item.get("latest_seal_yi") is not None:
                    sf_yi = float(rs_item["latest_seal_yi"])
                else:
                    sf_yi = round(float((allzt[c2].get("first") or {}).get("seal_fund") or 0.0) / 1e8, 3)
                r_snaps.append({
                    "ts_code": c2,
                    "name": allzt[c2].get("name") or "",
                    "first_seal": allzt[c2].get("first_seal"),
                    "seal_fund_yi": sf_yi,
                    "state": rs_item.get("state") or r_eval.get("state") or "anchored",
                })
                if sf_yi > 0:
                    rabbit_seals.append(sf_yi)
            g["rabbit_snapshot_at_entry"] = r_snaps
            if rabbit_seals:
                g["rabbit_max_seal_yi"] = round(max(rabbit_seals), 3)
            picked[c] = g
        if theme_matched_any:
            hit_themes.append(theme)
        elif theme_frozen_state:
            frozen_themes.append(f"{theme}({theme_frozen_state})")

    if frozen_themes:
        notes.append(f"兔子衰减/失效门控冻结新开仓：{'、'.join(frozen_themes)}")
    if purity_skips:
        notes.append(f"细分题材纯度过滤剔除 {len(purity_skips)} 只跨界/边缘票：{'；'.join(purity_skips[:4])}")

    if hit_themes:
        notes.append(
            f"按「题材内有一字板 → 买该题材其他二板」命中 {len(hit_themes)} 个题材："
            + "、".join(hit_themes)
            + f"；共 {len(picked)} 只候选。")
    elif frozen_themes or purity_skips:
        n_zt = len(allzt)
        n_two = sum(1 for g in allzt.values() if g.get("streak") == 2)
        notes.append(
            f"今日实时涨停池已读取（涨停 {n_zt} 只 / 其中二板 {n_two} 只），"
            "虽存在同题材龟兔组合，但均被兔子状态门控或细分题材纯度过滤拦截 → 按风控纪律暂不开仓。"
        )
    else:
        # 2026-11-20 改善：说清楚"数据是有的、只是策略按规则没找到组合"，
        # 避免用户把「不开仓」误读成「数据没实时更新」。
        n_zt = len(allzt)
        n_two = sum(1 for g in allzt.values() if g.get("streak") == 2)
        notes.append(
            f"今日实时涨停池已读取（涨停 {n_zt} 只 / 其中二板 {n_two} 只），"
            "但没有「题材内同时存在一字板与其他二板」的组合 → "
            "按裁定 J11「不硬凑、不退回全部二板旧口径」不开仓。"
            "这是策略判断结果，不是数据未更新。")
    return picked, notes


#: emotion_gate 按日缓存（它是全市场 O(N) 扫描，实测 ~4s）。
#: 同一交易日内反复调用结果不变（只依赖已落盘日线），因此按 day 缓存；
#: sync / _payload / API 多处调用只算一次。
_GATE_CACHE: dict[str, dict[str, Any]] = {}
_GATE_CACHE_MAX = 8


def _emotion_close_lists(p: pl.DataFrame, prev: str, today: str, *,
                         include_hi3: bool = False):
    """收盘口径：昨日高标(≥2板)/昨日涨停 → 今日收盘涨幅（可选返回 ≥3板高标梯队明细）。"""
    from backend import feature_engine
    hi_ret: list[float] = []
    prem_vals: list[float] = []
    hi3_rows: list[dict[str, Any]] = []
    name_map: dict[str, str] = {}
    if include_hi3:
        try:
            from backend import news_feed as _nf_st
            name_map, _ = _nf_st._stock_name_maps()
        except Exception:  # noqa: BLE001
            name_map = {}
    all_days = sorted(p["trade_date"].unique().to_list())
    day_strs = [str(d) for d in all_days]
    if prev not in day_strs or today not in day_strs:
        return (hi_ret, prem_vals, hi3_rows) if include_hi3 else (hi_ret, prem_vals)
    end_idx = day_strs.index(today)
    win_days = all_days[max(0, end_idx - 34):end_idx + 1]
    sub = (p.filter(pl.col("trade_date").is_in(win_days))
           .select(["ts_code", "trade_date", "close"])
           .sort(["ts_code", "trade_date"]))
    for (code,), g in sub.group_by("ts_code", maintain_order=True):
        ds = [str(x) for x in g["trade_date"].to_list()]
        if prev not in ds or today not in ds:
            continue
        jp, jt = ds.index(prev), ds.index(today)
        if jt < 1:
            continue
        c = g["close"].to_numpy().astype(float)
        _, st = feature_engine.zt_flags(code, c)
        lim = float(feature_engine.limit_pct(str(code))) * 100.0
        r = float(c[jt] / c[jt - 1] - 1.0) * 100.0
        if r != r:
            continue
        if int(st[jp]) >= 1:                 # 昨日涨停 → 进溢价样本
            prem_vals.append(r)
        if int(st[jp]) < 2:                  # 昨日高标（≥2 板）
            continue
        hi_ret.append(r)
        if include_hi3 and int(st[jp]) >= 3:
            c6 = str(code).zfill(6)
            if feature_engine.is_st(name_map.get(c6, "")):
                continue
            hi3_rows.append({
                "ts_code": c6,
                "prev_streak": int(st[jp]),
                "pct": round(r, 2),
                "is_zt": bool(r >= lim - 0.3),
                "is_dt": bool(r <= -(lim - 0.3)),
            })
    return (hi_ret, prem_vals, hi3_rows) if include_hi3 else (hi_ret, prem_vals)


def _emotion_intraday_lists(day: str, p: pl.DataFrame, quotes: pl.DataFrame | None = None, *,
                            include_hi3: bool = False):
    """盘中口径：昨日高标/昨日涨停 → 最新价涨幅（可选返回 ≥3板高标梯队明细）。"""
    from backend import feature_engine, feeds, ice_gate
    q = quotes if quotes is not None else feeds.all_quotes()
    if q is None or q.height == 0:
        return None
    stamp = ice_gate._quote_stamp(q)
    if not stamp.startswith(_day_dash(day)):
        return None                          # 行情不是当天的，不冒充
    prev = ice_gate.prev_day_state(p)
    if prev.height == 0:
        return None
    q_cols = [pl.col("ts_code").cast(pl.Utf8), pl.col("pct")]
    if "name" in q.columns:
        q_cols.append(pl.col("name").cast(pl.Utf8))
    qq = q.select(q_cols).filter(pl.col("pct").is_not_null())
    j = prev.join(qq, on="ts_code", how="inner")
    if j.height == 0:
        return None
    hi_ret = [float(x) for x in
              j.filter(pl.col("prev_streak") >= 2)["pct"].to_list() if x == x]
    prem_vals = [float(x) for x in
                 j.filter(pl.col("prev_is_zt"))["pct"].to_list() if x == x]
    if not include_hi3:
        return hi_ret, prem_vals, stamp
    name_map_i: dict[str, str] = {}
    if "name" not in j.columns:
        try:
            from backend import news_feed as _nf_sti
            name_map_i, _ = _nf_sti._stock_name_maps()
        except Exception:  # noqa: BLE001
            name_map_i = {}
    hi3_rows: list[dict[str, Any]] = []
    for r in j.filter(pl.col("prev_streak") >= 3).iter_rows(named=True):
        code = str(r.get("ts_code") or "").zfill(6)
        nm = str(r.get("name") or name_map_i.get(code, "") or "")
        if feature_engine.is_st(nm):
            continue
        pct = float(r["pct"])
        if pct != pct:
            continue
        lim = float(feature_engine.limit_pct(code)) * 100.0
        hi3_rows.append({
            "ts_code": code,
            "prev_streak": int(r["prev_streak"]),
            "pct": round(pct, 2),
            "is_zt": bool(pct >= lim - 0.3),
            "is_dt": bool(pct <= -(lim - 0.3)),
        })
    return hi_ret, prem_vals, hi3_rows, stamp


def _rolling_high_board_spillover(p: pl.DataFrame, target_day: str, *,
                                  lookback_days: int = 60) -> dict[str, Any]:
    """严格因果计算近 60 个交易日「高标(≥3板)走强/跌停」对次日二板梯队的真实传导率。

    不写死「高标涨就一定带动」或「一定无影响」，用最近 60 日真实日线滚动度量市场传导强度。
    """
    try:
        from backend import ice_gate
        d_dash = _day_dash(target_day)
        all_days = sorted({str(d) for d in p["trade_date"].unique().to_list() if str(d) < d_dash})
        if len(all_days) < 15:
            return {"available": False, "window_days": len(all_days)}
        win_days = all_days[-(lookback_days + 10):]
        fl = ice_gate._flag_frame(p.filter(pl.col("trade_date").cast(pl.Utf8).is_in(win_days)))
        # 逐日统计：昨日≥3板今日涨停/跌停数，以及当日「可买换手二板（剔除一字买不进）」次日收盘均涨
        fl = fl.with_columns(
            pl.col("pct").shift(-1).over("ts_code").alias("next_pct")
        )
        if all(col in fl.columns for col in ("open", "high", "low", "close")):
            fl = fl.with_columns(
                ((pl.col("high") <= pl.col("low") * 1.002)
                 & (pl.col("open") >= pl.col("close") * 0.998)).fill_null(False).alias("_is_ow")
            )
        else:
            fl = fl.with_columns(pl.lit(False).alias("_is_ow"))
        # 严格因果：用于计算 next_pct 的最后一天不能泄露 target_day，因此去掉最后一个交易日
        eval_days = set(all_days[-lookback_days:-1])
        sub = fl.filter(pl.col("trade_date").cast(pl.Utf8).is_in(list(eval_days)))
        agg = sub.group_by("trade_date").agg([
            ((pl.col("prev_streak") >= 3) & pl.col("is_zt")).sum().alias("hi3_zt"),
            ((pl.col("prev_streak") >= 3) & pl.col("is_dt")).sum().alias("hi3_dt"),
            (pl.col("prev_streak") >= 3).sum().alias("hi3_n"),
            pl.when((pl.col("streak") == 2) & (~pl.col("_is_ow"))).then(pl.col("next_pct")).otherwise(None).mean().alias("b2_next_avg"),
            ((pl.col("streak") == 2) & (~pl.col("_is_ow"))).sum().alias("b2_n"),
        ]).to_dicts()

        lead_days = [r for r in agg if (r.get("hi3_zt") or 0) >= 1 and (r.get("hi3_dt") or 0) == 0 and r.get("b2_next_avg") is not None]
        drag_days = [r for r in agg if (r.get("hi3_dt") or 0) >= 1 and r.get("b2_next_avg") is not None]
        lead_win = round(sum(1 for r in lead_days if float(r["b2_next_avg"]) > 0) / len(lead_days), 3) if lead_days else None
        lead_avg = round(sum(float(r["b2_next_avg"]) for r in lead_days) / len(lead_days), 2) if lead_days else None
        drag_win = round(sum(1 for r in drag_days if float(r["b2_next_avg"]) > 0) / len(drag_days), 3) if drag_days else None
        drag_avg = round(sum(float(r["b2_next_avg"]) for r in drag_days) / len(drag_days), 2) if drag_days else None
        return {
            "available": True,
            "window_days": len(eval_days),
            "lead_n": len(lead_days),
            "lead_b2_win_rate": lead_win,
            "lead_b2_next_avg": lead_avg,
            "drag_n": len(drag_days),
            "drag_b2_win_rate": drag_win,
            "drag_b2_next_avg": drag_avg,
        }
    except Exception:  # noqa: BLE001
        return {"available": False}


def _build_high_board_pulse(hi3_rows: list[dict[str, Any]], day: str, p: pl.DataFrame) -> dict[str, Any]:
    """构建「高标股对连板梯队影响」动态评估（不对称 + 60日滚动实证传导率，不写死）。"""
    if not hi3_rows:
        return {
            "available": False,
            "state": "none",
            "state_cn": "昨日无≥3板高标",
            "size_mult": 1.0,
            "lead_themes": [],
            "leaders": [],
            "note": "昨日无 ≥3 板连板高标，梯队无高位压制或引领。",
        }
    # 补充股票简称与题材轴（复用统一口径）
    name_map: dict[str, str] = {}
    try:
        from backend import news_feed
        name_map, _ = news_feed._stock_name_maps()
    except Exception:  # noqa: BLE001
        pass
    tags_map = _theme_tags(day, codes=[r["ts_code"] for r in hi3_rows])
    axis_name_by_code: dict[str, list[str]] = {}
    try:
        from backend import theme_match
        idx = theme_match._index()
        for r in hi3_rows:
            c = r["ts_code"]
            keys = theme_match.map_to_axes(tags_map.get(c) or [])
            axis_name_by_code[c] = [idx.key_name.get(k, k) for k in keys]
    except Exception:  # noqa: BLE001
        pass

    sorted_rows = sorted(hi3_rows, key=lambda x: (-x["prev_streak"], -x["pct"]))
    max_h = sorted_rows[0]["prev_streak"]
    n_dt = sum(1 for r in sorted_rows if r["is_dt"])
    n_zt = sum(1 for r in sorted_rows if r["is_zt"])
    avg3 = round(sum(r["pct"] for r in sorted_rows) / len(sorted_rows), 2)
    space_rows = [r for r in sorted_rows if r["prev_streak"] == max_h]
    space_dt = any(r["is_dt"] for r in space_rows)
    space_zt = any(r["is_zt"] for r in space_rows)

    spill = _rolling_high_board_spillover(p, day, lookback_days=60)
    lead_win = spill.get("lead_b2_win_rate")
    drag_win = spill.get("drag_b2_win_rate")

    # 不对称动态判定：高标跌停负反馈概率高（动态降仓），高标走强传导不定（按近60日实证传导率决定是否给同题材小幅加分）
    lead_themes: list[str] = []
    if n_dt >= 2 or (space_dt and avg3 <= -3.0):
        state = "drag"
        state_cn = "高标核按钮拖累"
        # 近 60 日高标跌停日若二板确实容易亏钱（胜率<45%），收紧至 0.70，否则 0.80
        size_mult = 0.70 if (drag_win is not None and drag_win < 0.45) else 0.80
        note = (
            f"昨日≥3板高标 {len(sorted_rows)} 只中 {n_dt} 只跌停"
            + ("（含最高空间板）" if space_dt else "")
            + f"，均涨 {avg3:+.2f}%；负反馈扩散概率高，动态降仓 ×{size_mult:.2f}"
            + (f"（近60日高标跌停日二板次日胜率 {drag_win:.0%}）" if drag_win is not None else "")
        )
    elif n_dt == 1:
        state = "warn"
        state_cn = "单高标跌停预警"
        size_mult = 0.85
        note = (
            f"昨日≥3板高标有 1 只跌停（均涨 {avg3:+.2f}%），梯队存在局部负反馈风险；"
            f"温和控仓 ×{size_mult:.2f}（不直接一刀切空仓）"
        )
    elif n_zt >= 1 and avg3 >= 2.0:
        state = "lead"
        state_cn = "高标强势晋级"
        size_mult = 1.0
        for r in sorted_rows:
            if r["is_zt"]:
                for th in axis_name_by_code.get(r["ts_code"], []):
                    if th not in lead_themes:
                        lead_themes.append(th)
        effective_spill = (lead_win is not None and lead_win >= 0.50 and (spill.get("lead_n") or 0) >= 4)
        if effective_spill:
            note = (
                f"昨日≥3板高标 {n_zt}/{len(sorted_rows)} 只晋级（空间 {max_h}→{max_h + 1 if space_zt else max_h}板）；"
                f"近60日高标走强对二板正传导胜率 {lead_win:.0%}({spill.get('lead_n')}日)，同题材给 +0.5 催化加分"
            )
        else:
            lead_themes = []  # 近期传导不稳定时不硬加分，诚实告知用户
            lead_avg = spill.get("lead_b2_next_avg")
            avg_part = f" / 均涨 {float(lead_avg):+.2f}%" if lead_avg is not None else ""
            note = (
                f"昨日≥3板高标 {n_zt}/{len(sorted_rows)} 只晋级，但近60日对二板传导不定"
                + (f"（胜率 {lead_win:.0%}{avg_part}）" if lead_win is not None else "（样本不足）")
                + "，暂不盲目加仓"
            )
    else:
        state = "neutral"
        state_cn = "高标分歧平稳"
        size_mult = 1.0
        note = f"昨日≥3板高标 {len(sorted_rows)} 只（晋级 {n_zt} / 跌停 0 / 均涨 {avg3:+.2f}%），梯队平稳。"

    leaders_brief = []
    for r in sorted_rows[:5]:
        c = r["ts_code"]
        st_lbl = "晋级" if r["is_zt"] else ("跌停" if r["is_dt"] else "分歧")
        leaders_brief.append({
            "ts_code": c,
            "name": name_map.get(c, c),
            "prev_streak": r["prev_streak"],
            "pct": r["pct"],
            "status": st_lbl,
            "themes": axis_name_by_code.get(c, [])[:2],
        })

    return {
        "available": True,
        "state": state,
        "state_cn": state_cn,
        "max_prev_streak": max_h,
        "n_hi3": len(sorted_rows),
        "n_zt": n_zt,
        "n_dt": n_dt,
        "space_dt": space_dt,
        "space_zt": space_zt,
        "avg_ret": avg3,
        "size_mult": size_mult,
        "lead_themes": lead_themes,
        "leaders": leaders_brief,
        "spillover_60d": spill,
        "note": note,
    }


def emotion_gate(day: str, *, intraday: bool = False) -> dict[str, Any]:
    """悟空真经第一步：情绪闸门（人气股赚钱效应）。

    结果按 day 缓存（见 `_GATE_CACHE`）：全市场扫描约 4s，多处调用只算一次。
    盘中读数（intraday=True，当日收盘 bar 未进面板时）**不缓存** —— 行情在变。

    用户原话：「明显的亏钱效应，最直接的指标，人气股是不是在大跌，是不是按在跌停」。

    用三个可历史回溯的代理指标（任一明显恶化即判情绪不佳）：
      A. 昨日连板≥2 的高标，今日平均涨幅 ≤ -3%
      B. 昨日高标今日跌停占比 ≥ 40%
      C. 昨日涨停溢价 premium ≤ -1.5%

    为什么要区分「高低切换」与「全面崩盘」：2026-11-16 全市场跌停 68 家
    （旧口径判冰点 L5），但昨日高标 6 只里 4 只涨停、平均 +2.18% —— 那只是
    资金换方向，不是打板亏钱效应。旧口径会让策略在这种日子空仓。
    """
    # 键归一：'20261120' 与 '2026-11-20' 是同一交易日，必须共用缓存
    # （2026-11-23 审计实测：两种写法各扫一遍全市场，每次 ~4s）
    from backend import dates as _dates
    _nd = _dates.norm_day(day)
    key = str(_nd) if _nd else str(day)
    hit = _GATE_CACHE.get(key)
    if hit is not None:
        return copy.deepcopy(hit)
    out: dict[str, Any] = {"available": False, "reason": None, "blocked": False}
    try:
        from backend import feeds, feature_engine
        p = feeds.build_panel()
        if p is None or p.height == 0:
            out["reason"] = "面板不可用"
            return out
        days = sorted({str(d) for d in p["trade_date"].unique().to_list()})
        dd = _day_dash(day)
        as_of = ""
        intraday_used = False
        if dd in days:
            i = days.index(dd)
            if i < 1:
                out["reason"] = "无前一交易日"
                return out
            prev, today = days[i - 1], dd
            hi_ret, prem_vals, hi3_rows = _emotion_close_lists(p, prev, today, include_hi3=True)
        elif intraday:
            got = _emotion_intraday_lists(day, p, include_hi3=True)
            if got is None:
                out["reason"] = f"面板无 {dd}，且实时行情不可用（非当日快照）"
                return out
            hi_ret, prem_vals, hi3_rows, as_of = got
            intraday_used = True
        else:
            out["reason"] = f"面板无 {dd}"
            return out

        if not hi_ret:
            out["reason"] = "无昨日高标样本"
            return out
        n = len(hi_ret)
        avg = sum(hi_ret) / n
        dt_share = sum(1 for r in hi_ret if r <= -9.5) / n
        hb_pulse = _build_high_board_pulse(hi3_rows, day, p)

        # C：昨日涨停溢价 = 昨日所有涨停股，今日平均涨跌幅。
        # 2026-11-23 用户裁定：用真实日线算回来（此前字段恒为 None）。
        # 口径：昨日涨停（含首板与连板）→ 今日收盘相对昨收的平均涨幅。
        premium = round(sum(prem_vals) / len(prem_vals), 2) if prem_vals else None
        out.update({
            "available": True,
            "intraday": intraday_used,
            "as_of": as_of or None,
            "n_high": n,
            "avg_ret": round(avg, 2),
            "limit_down_share": round(dt_share, 3),
            "worst": round(min(hi_ret), 2),
            "premium": premium,
            "n_premium": len(prem_vals),
            "high_board_pulse": hb_pulse,
        })
        reasons = []
        if avg <= -3.0:
            reasons.append(f"昨日高标今日均涨 {avg:+.2f}% ≤ -3%")
        if dt_share >= 0.40:
            reasons.append(f"昨日高标今日跌停占比 {dt_share:.0%} ≥ 40%")
        if premium is not None and premium <= -1.5:
            reasons.append(f"昨日涨停溢价 {premium:+.2f}% ≤ -1.5%")
        out["blocked"] = bool(reasons)
        out["reasons"] = reasons
        out["verdict"] = ("情绪不佳 → 停手/控仓" if reasons else "情绪可做")
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"{type(e).__name__}: {e}"
    # 只缓存成功结果；失败（面板缺失等）不缓存，下次仍可重试。
    # 盘中读数不缓存：行情每 30 秒在变，缓存会把 09:30 的判断留到 10:30。
    if out.get("available") and not out.get("intraday"):
        if len(_GATE_CACHE) >= _GATE_CACHE_MAX:
            _GATE_CACHE.pop(next(iter(_GATE_CACHE)))
        _GATE_CACHE[key] = copy.deepcopy(out)
    return out


# account_circuit_breaker 已解耦至 backend.position_sizing（顶部原名导入）


def market_climax_and_ebb_guard(
    day: str,
    gate: dict[str, Any] | None,
    ice: dict[str, Any] | None,
    *,
    frames: list[dict[str, Any]] | None = None,
    hist_zt_counts: list[int] | None = None,
    rabbit_themes: dict[str, list[str]] | None = None,
    youzi_live_win_rate: float | None = None,
    major_bull_catalyst: bool = False,
    broken_rate: float | None = None,
    vol_ratio_20: float | None = None,
    market_reaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """情绪高潮防摊大饼 + 退潮初期拦截（含跨题材多兔高低切试错降级，全自适应）。

    1. 退潮初期（最毒阶段，区别于跌透后的冰点末期试错）：
       当前并非深冰点（非 L5 末期），但高标突发核按钮（state=='drag'）且昨日涨停溢价转负 → 暂停新开仓；
       例外（高低切）：若当日出现与跌停旧高标**零题材重叠**且有 ≥2 只一字兔共振的新主线，
       降级为 0.50× 小仓试错而非一刀切拦截，防止踏空新周期启动。
    2. 情绪高潮预警（防次日分化全埋）：
       当日涨停家数达到近期 60 日滚动 P90（无历史分布时退化到单帧涨停 ≥85 家）→
       强制收缩单日上限至 2 只、每题材 1 只（只做唯一前排龙二，绝不摊大饼）。
    """
    g = gate or {}
    ic = ice or {}
    hb = (g.get("high_board_pulse") or {}) if g.get("available") else {}
    prem = g.get("premium")
    is_deep_ice = bool(ic.get("available") and ic.get("level") == "L5")

    # 1) 退潮初期识别（区分同题材退潮 vs 跨题材多兔「高低切」）
    ebb_block = bool(
        hb.get("state") == "drag"
        and (prem is not None and float(prem) < 0.0)
        and not is_deep_ice
    )
    high_low_cut_themes: list[str] = []
    ebb_trial_mult = 1.0
    if ebb_block and rabbit_themes:
        drag_themes: set[str] = set()
        for ld in (hb.get("leaders") or []):
            if ld.get("status") == "跌停":
                drag_themes.update(str(t) for t in (ld.get("themes") or []) if t)
        for th, rabs in rabbit_themes.items():
            if len(rabs) >= 2 and th not in drag_themes:
                high_low_cut_themes.append(th)
        if high_low_cut_themes:
            ebb_block = False
            ebb_trial_mult = 0.50

    # 2) 情绪高潮识别（优先用近 60 日滚动涨停家数 P90）
    today_zt_n = 0
    if frames:
        last_rows, _ = _pool_rows(frames[-1])
        today_zt_n = len(last_rows)
    elif ic.get("available") and isinstance((ic.get("metrics") or {}).get("limit_up_n"), (int, float)):
        today_zt_n = int((ic.get("metrics") or {})["limit_up_n"])

    climax_th = 85
    valid_hist = sorted(int(x) for x in (hist_zt_counts or []) if x is not None and int(x) > 0)
    if len(valid_hist) >= 10:
        climax_th = max(55, valid_hist[min(len(valid_hist) - 1, int(0.90 * len(valid_hist)))])

    # 2.5) 两阶段动态市场确认引擎（以史鉴今：2024-09-24 / 2026-11-27 真启动 vs 2024-10-08 73% 炸板兑现陷阱）
    #      Stage 1: 消息标记（major_bull_catalyst）绝不单独松阀；
    #      Stage 2: 看期货(A50/恒生科技) + 国内宽基(含突然拉升) + 全市场炸板率 + 全市场量能比 + 游资实时胜率：
    #        - 高炸板兑现陷阱（broken_rate ≥ 45%，或 ≥40% 且期货/指数回落）→ 禁止松绑并立刻收紧；
    #        - 市场真认账共振 → 连续平滑计算 tailwind_level ∈ [0, 1] 动态释放名额与仓位。
    mr = market_reaction or {}
    mr_verdict = str(mr.get("verdict") or "neutral")
    mr_surge = bool(mr.get("sudden_surge"))
    br_f = float(broken_rate) if broken_rate is not None else None
    vr_f = float(vol_ratio_20) if (vol_ratio_20 is not None and float(vol_ratio_20) > 0) else None

    sell_the_news_trap = bool(
        br_f is not None and (
            br_f >= 0.45
            or (br_f >= 0.40 and mr_verdict == "fade")
        )
    )
    trap_size_mult = 0.70 if sell_the_news_trap else 1.0

    tw_score = 0.0
    if not ebb_block and not sell_the_news_trap:
        if prem is not None and float(prem) > 0.0:
            tw_score += min(0.25, float(prem) / 3.0 * 0.25)
        if youzi_live_win_rate is not None and float(youzi_live_win_rate) > 0.50:
            tw_score += min(0.25, max(0.0, (float(youzi_live_win_rate) - 0.50) / 0.30) * 0.25)
        ld_share = float(g.get("limit_down_share") or 0.0)
        if hb.get("state") == "lead" and ld_share <= 0.03:
            tw_score += 0.15 * (1.0 - ld_share / 0.05)
        if vr_f is not None and vr_f > 1.0:
            tw_score += min(0.15, (vr_f - 1.0) / 0.60 * 0.15)
        if mr_verdict == "confirm":
            tw_score += 0.20 + (0.10 if mr_surge else 0.0)
        elif mr_verdict == "fade":
            tw_score -= 0.25
        # 消息先标记、必须配合市场正向反应（期货/宽基确认或游资胜率≥58%或放量≥1.15x）才计入政策加分
        market_confirmed = bool(
            mr_verdict == "confirm"
            or (youzi_live_win_rate is not None and float(youzi_live_win_rate) >= 0.58)
            or (vr_f is not None and vr_f >= 1.15 and (br_f is None or br_f < 0.38))
        )
        if major_bull_catalyst and market_confirmed:
            tw_score += 0.15
        # 炸板率偏高区间（35%~45%）平滑阻尼，防半山腰兑现
        if br_f is not None and br_f > 0.35:
            tw_score *= max(0.40, 1.0 - (br_f - 0.35) / 0.15 * 0.60)
    else:
        market_confirmed = False

    has_external_confirm = bool(
        not sell_the_news_trap and (
            mr_verdict == "confirm"
            or (youzi_live_win_rate is not None and float(youzi_live_win_rate) >= 0.58)
            or (major_bull_catalyst and market_confirmed)
        )
    )
    tailwind_level = round(max(0.0, min(1.0, tw_score)), 3) if has_external_confirm else 0.0
    tailwind_breakout = bool(tailwind_level >= 0.55)
    # 平滑映射防守松绑系数：0.55~0.70 过渡松绑 0.85，≥0.70（类似 9.24 / 突发政策超级顺风日）直接 1.00 完全释放仓位
    defensive_relax_mult = (
        1.0 if (tailwind_breakout and tailwind_level >= 0.70)
        else (0.85 if tailwind_breakout else None)
    )

    climax_active = bool((today_zt_n > 0 and today_zt_n >= climax_th and not tailwind_breakout) or sell_the_news_trap)
    if tailwind_breakout:
        max_total = min(MAX_DAILY_ORDERS, 3 + int(round((tailwind_level - 0.55) / 0.45 * 2)))
        max_per_theme = MAX_PER_THEME_ORDERS if tailwind_level >= 0.75 else 1
    else:
        max_total = 2 if climax_active else MAX_DAILY_ORDERS
        max_per_theme = 1 if climax_active else MAX_PER_THEME_ORDERS

    reasons: list[str] = []
    if ebb_block:
        reasons.append(f"退潮初期预警（高标核按钮拖累 + 昨日涨停溢价 {prem:+.2f}% < 0）→ 暂停新开仓防接飞刀")
    elif high_low_cut_themes:
        reasons.append(
            f"高低切试错（旧高标跌停但新主线 {'、'.join(high_low_cut_themes)} 多兔共振无重叠）"
            f"→ 降级为 ×{ebb_trial_mult:.2f} 小仓试错，不盲目空仓"
        )
    if sell_the_news_trap:
        reasons.append(
            f"高炸板兑现陷阱警报（全市场炸板率 {br_f:.0%} ≥ 45% 原型 24-10-08）→ "
            f"禁止松绑，收缩至单日≤{max_total}只且仓位收紧 ×{trap_size_mult:.2f}"
        )
    elif tailwind_breakout:
        reasons.append(
            f"动态顺风启动确认（顺风度 {tailwind_level:.2f} · 昨日溢价 {float(prem or 0):+.2f}%"
            + (f" · 游资胜率 {float(youzi_live_win_rate):.0%}" if youzi_live_win_rate is not None else "")
            + (f" · 量能 {vr_f:.2f}x" if vr_f is not None else "")
            + (" · 期货/宽基突拉共振" if mr_surge else (" · 期货/宽基确认" if mr_verdict == "confirm" else ""))
            + f" · 涨停 {today_zt_n} 家）→ 动态放宽至单日≤{max_total}只/每题材≤{max_per_theme}只，防守松绑 ×{defensive_relax_mult:.2f}"
        )
    elif climax_active:
        reasons.append(
            f"情绪高潮防摊大饼（当日涨停 {today_zt_n} 家 ≥ 滚动高潮线 {climax_th} 家）→ "
            f"收缩至单日≤{max_total}只、每题材≤{max_per_theme}只前排"
        )
    return {
        "ebb_block": ebb_block,
        "ebb_trial_mult": ebb_trial_mult,
        "high_low_cut_themes": high_low_cut_themes,
        "sell_the_news_trap": sell_the_news_trap,
        "trap_size_mult": trap_size_mult,
        "tailwind_level": tailwind_level,
        "tailwind_breakout": tailwind_breakout,
        "defensive_relax_mult": defensive_relax_mult,
        "climax_active": climax_active,
        "today_zt_n": today_zt_n,
        "climax_threshold": climax_th,
        "max_total": max_total,
        "max_per_theme": max_per_theme,
        "reasons": reasons,
    }


_PREV_FIRST_BOARD_CACHE: dict[str, set[str]] = {}


def _prev_first_board_codes(day: str) -> set[str]:
    """返回 day 前一交易日的真实首板股票集合（prev_streak == 1，排除 ST，按日缓存）。"""
    d_key = _day_dash(day)
    if d_key in _PREV_FIRST_BOARD_CACHE:
        return set(_PREV_FIRST_BOARD_CACHE[d_key])
    out: set[str] = set()
    try:
        from backend import feeds, ice_gate, news_feed
        p = feeds.build_panel()
        if p is None or p.height == 0:
            return out
        # 严格取 < day 的最近一个交易日作为 T-1
        sub_p = p.filter(pl.col("trade_date").cast(pl.Utf8) < d_key)
        if sub_p.height == 0:
            return out
        prev_df = ice_gate.prev_day_state(sub_p)
        if prev_df.height == 0:
            return out
        name_map, _ = news_feed._stock_name_maps()
        for r in prev_df.filter(pl.col("prev_streak") == 1).iter_rows(named=True):
            c6 = str(r.get("ts_code") or "").zfill(6)
            if not c6 or c6 == "000000":
                continue
            if feature_engine.is_st(name_map.get(c6, "")):
                continue
            out.add(c6)
    except Exception:  # noqa: BLE001
        return set()
    if len(_PREV_FIRST_BOARD_CACHE) >= 8:
        _PREV_FIRST_BOARD_CACHE.clear()
    _PREV_FIRST_BOARD_CACHE[d_key] = set(out)
    return out


def scan_today_first_to_second_sweep(
    day: str,
    frames: list[dict[str, Any]],
    *,
    quotes_map: dict[str, dict[str, Any]] | None = None,
    use_live_l1: bool = True,
) -> list[dict[str, Any]]:
    """昨日首板池 → 今日一进二临封扫板双轨扫描（真实 L1 五档核验，零伪造）。"""
    if not frames:
        return []
    try:
        from backend import buy_signals as _bs, theme_match as _tm
        allzt = collect_all_zt(frames)
        rabbits = {c for c, g in allzt.items() if is_rabbit(g)}
        if not rabbits:
            return []
        prev_1b = _prev_first_board_codes(day)
        if not prev_1b:
            return []
        cand_codes = sorted(rabbits | prev_1b)
        tags = _theme_tags(day, codes=cand_codes)
        if not tags:
            return []
        items = [{"code": c, "name": (allzt.get(c) or {}).get("name") or "",
                  "tags": tags.get(c) or [], "industry": None,
                  "streak": 2 if c in rabbits else 1}
                 for c in cand_codes if tags.get(c)]
        grouped = _tm.group_by_theme(items)
        theme_rabbits: dict[str, list[str]] = {}
        theme_1b: dict[str, list[str]] = {}
        for g in grouped.get("groups") or []:
            if g.get("level") not in ("axis", "tag"):
                continue
            th = str(g.get("theme") or "")
            for c in g.get("codes") or []:
                c6 = str(c).zfill(6)
                if c6 in rabbits:
                    theme_rabbits.setdefault(th, []).append(c6)
                elif c6 in prev_1b and c6 not in allzt:
                    theme_1b.setdefault(th, []).append(c6)
        # 只保留存在锚定兔子的题材下的昨日首板
        active_1b: set[str] = set()
        for th in list(theme_rabbits.keys()):
            active_1b.update(theme_1b.get(th) or [])
        if not active_1b:
            return []

        qmap: dict[str, dict[str, Any]] = dict(quotes_map or {})
        if not qmap:
            qdf = archive.latest_frame(day, "quotes")
            if qdf is not None and qdf.height > 0:
                sub_q = qdf.filter(pl.col("ts_code").cast(pl.Utf8).str.zfill(6).is_in(list(active_1b)))
                for r in sub_q.iter_rows(named=True):
                    c6 = str(r.get("ts_code") or "").zfill(6)
                    qmap[c6] = dict(r)
            if use_live_l1:
                # 仅对已冲高至 ≥+6.5%（或 20cm ≥+13%）的少数标的发一次轻量五档批量查询
                shortlist: list[str] = []
                for c6 in sorted(active_1b):
                    qr = qmap.get(c6)
                    if qr is None:
                        shortlist.append(c6)
                    else:
                        pct_v = qr.get("pct")
                        lim_p = feature_engine.limit_pct(c6) * 100.0
                        if pct_v is not None and float(pct_v) >= 0.65 * lim_p:
                            shortlist.append(c6)
                if shortlist:
                    try:
                        from backend import feeds
                        l1 = feeds.batch_quotes(shortlist[:25], ttl=5.0)
                        for c6, l1_q in (l1 or {}).items():
                            if l1_q:
                                merged_q = dict(qmap.get(c6) or {})
                                merged_q.update(l1_q)
                                qmap[c6] = merged_q
                    except Exception:  # noqa: BLE001
                        pass

        return _bs.scan_first_to_second_sweep(
            theme_rabbits, theme_1b, qmap,
            already_sealed=set(allzt.keys()),
        )
    except Exception:  # noqa: BLE001
        return []


def _update_sweep_orders(state: dict[str, Any], frames: list[dict[str, Any]], day: str) -> None:
    """盘中/盘后持续跟踪 pre_seal_sweep 扫板单后续是否真正封死涨停，打破「+8.5% 必封幻觉」。"""
    if not frames:
        return
    prefix = f"{day}|"
    last_rows, _ = _pool_rows(frames[-1])
    last_zt = {str(r.get("ts_code") or "").zfill(6): r for r in last_rows if r.get("ts_code")}
    maps = _frame_maps(frames)
    ts_pos = {ts: i for i, (ts, _) in enumerate(maps)}
    for oid, o in (state.get("orders") or {}).items():
        if not oid.startswith(prefix) or o.get("exec_mode") != "pre_seal_sweep":
            continue
        code = str(o.get("ts_code") or "").zfill(6)
        i0 = ts_pos.get(str(o.get("entry_frame_ts") or ""))
        start_idx = (i0 + 1) if i0 is not None else 0
        ever_sealed = False
        max_broken = int(o.get("broken_max") or 0) if o.get("broken_max") is not None else 0
        for _ts, m in maps[start_idx:]:
            if code in m:
                ever_sealed = True
                br = m[code]
                if br is not None:
                    max_broken = max(max_broken, int(br))
        in_last = code in last_zt
        if in_last:
            last_br = last_zt[code].get("broken")
            if last_br is not None:
                max_broken = max(max_broken, int(last_br))
            o["broken_max"] = max_broken
            o["sealed_at_close"] = True
            o["sweep_seal_verified"] = True
            o["sweep_failed_seal"] = False
        elif len(maps) > start_idx + 1:
            # 后续帧已推进但末帧不在涨停池 → 冲板失败或炸板未回封，如实标为炸板以便 T+1 走路径 A 优先止损
            o["broken_max"] = max(1, max_broken)
            o["sealed_at_close"] = False
            o["sweep_seal_verified"] = True
            o["sweep_failed_seal"] = True
            o["fill_reason"] = (
                f"临封扫板后{'封板再炸' if ever_sealed else '未封死涨停'}（冲板回落），T+1 按路径 A 优先止损"
            )
        pos = (state.get("positions") or {}).get(oid)
        if isinstance(pos, dict) and pos.get("status") != "closed":
            if o.get("broken_max") is not None:
                pos["broken_max"] = o["broken_max"]
            if "sweep_failed_seal" in o:
                pos["sweep_failed_seal"] = o["sweep_failed_seal"]


def _evaluate_holding_linkage(
    open_pos: list[dict[str, Any]],
    state: dict[str, Any],
    day: str,
    frames: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """为所有在途持仓（含昨日买进、今日待卖出的 T+1 持仓）计算兔子强弱与同门乌龟实时联动信号。"""
    if not open_pos:
        return {}
    orders = state.get("orders") or {}
    need_codes: set[str] = set()
    for p in open_pos:
        ord_ref = orders.get(p.get("id")) or {}
        rcodes = p.get("guitu_rabbit_codes") or ord_ref.get("guitu_rabbit_codes") or []
        for rc in rcodes:
            c6 = str(rc).zfill(6)
            if c6 and c6 != "000000":
                need_codes.add(c6)
        entry_d = str(p.get("entry_date") or ord_ref.get("day") or "")
        th = str(p.get("guitu_theme") or ord_ref.get("guitu_theme") or "")
        if entry_d and th:
            for _oid, _o in orders.items():
                if (str(_o.get("day") or _o.get("entry_date") or "") == entry_d
                        and str(_o.get("guitu_theme") or "") == th
                        and not _o.get("invalid_historical_bug")):
                    pc6 = str(_o.get("ts_code") or "").zfill(6)
                    if pc6 and pc6 != str(p.get("ts_code") or "").zfill(6):
                        need_codes.add(pc6)

    qmap: dict[str, dict[str, Any]] = {}
    if need_codes:
        try:
            qdf = archive.latest_frame(day, "quotes")
            if qdf is not None and qdf.height > 0:
                for r in qdf.filter(pl.col("ts_code").cast(pl.Utf8).str.zfill(6).is_in(list(need_codes))).iter_rows(named=True):
                    c6 = str(r.get("ts_code") or "").zfill(6)
                    qmap[c6] = dict(r)
        except Exception:  # noqa: BLE001
            pass
        try:
            from backend import feeds, market_clock
            if market_clock.is_trading("CN") or not qmap:
                live_q = feeds.batch_quotes(sorted(need_codes), ttl=10.0)
                for c6, qr in (live_q or {}).items():
                    if qr:
                        qmap[c6] = {**qmap.get(c6, {}), **qr}
        except Exception:  # noqa: BLE001
            pass

    zt_last: set[str] = set()
    if frames:
        lrows, _ = _pool_rows(frames[-1])
        zt_last = {str(r.get("ts_code") or "").zfill(6) for r in lrows if r.get("ts_code")}

    out: dict[str, dict[str, Any]] = {}
    for p in open_pos:
        pid = str(p.get("id") or "")
        ord_ref = orders.get(pid) or {}
        rcodes = [
            str(rc).zfill(6)
            for rc in (p.get("guitu_rabbit_codes") or ord_ref.get("guitu_rabbit_codes") or [])
            if str(rc).strip()
        ]
        rabbit_strong: bool | None = None
        rabbit_nuclear = False
        rabbit_weak = False
        if rcodes:
            any_zt = False
            any_nuc = False
            all_weak = True
            has_obs = False
            for rc in rcodes:
                qr = qmap.get(rc) or {}
                pct = qr.get("pct")
                lim_pct = feature_engine.limit_pct(rc) * 100.0
                if rc in zt_last:
                    any_zt = True
                    all_weak = False
                    has_obs = True
                    continue
                if pct is not None:
                    has_obs = True
                    pf = float(pct)
                    if pf >= lim_pct - 0.5:
                        any_zt = True
                        all_weak = False
                    elif pf <= -0.70 * lim_pct:
                        any_nuc = True
                    elif pf > 0.0:
                        all_weak = False
            if any_nuc:
                rabbit_nuclear = True
                rabbit_weak = True
                rabbit_strong = False
            elif any_zt:
                rabbit_strong = True
                rabbit_weak = False
            elif has_obs and all_weak:
                rabbit_weak = True
                rabbit_strong = False

        # 同门乌龟卡位保护（同买入日同题材其他乌龟走强）
        peer_strong = False
        entry_d = str(p.get("entry_date") or ord_ref.get("day") or "")
        th = str(p.get("guitu_theme") or ord_ref.get("guitu_theme") or "")
        my_c6 = str(p.get("ts_code") or "").zfill(6)
        if entry_d and th:
            for _oid, _o in orders.items():
                if (str(_o.get("day") or _o.get("entry_date") or "") == entry_d
                        and str(_o.get("guitu_theme") or "") == th
                        and not _o.get("invalid_historical_bug")):
                    pc6 = str(_o.get("ts_code") or "").zfill(6)
                    if pc6 == my_c6:
                        continue
                    pqr = qmap.get(pc6) or {}
                    ppct = pqr.get("pct")
                    if pc6 in zt_last or (ppct is not None and float(ppct) >= 5.0):
                        peer_strong = True
                        break

        out[pid] = {
            "rabbit_weak": rabbit_weak,
            "rabbit_strong": rabbit_strong,
            "rabbit_nuclear": rabbit_nuclear,
            "peer_turtle_strong": peer_strong,
        }
    return out


def sync(day: str | None = None, *, frames: list[dict[str, Any]] | None = None,
         daily: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """同步当日委托 + 结算所有未平仓持仓。幂等，可盘中反复调用。"""
    day = day or datetime.now().strftime("%Y%m%d")
    with _lock:
        state = _load_state()
        n_voided = sanitize_invalid_positions(state)
        fr = frames if frames is not None else read_frames(day)
        notes: list[str] = []
        if n_voided:
            notes.append(f"账本自净：无损冲销 {n_voided} 笔历史异常脏单持仓并回退资金")
        # 2026-11-16 用户裁定：改为「题材内有一字板 → 买该题材其他二板」。
        # 旧实现 collect_two_board 是「全部二板都买」，方向与策略原意不符。
        panel_gap_blocked = False
        panel_gap_reason = ""
        if frames is None:
            try:
                from backend import feeds
                ph = feeds.panel_health(today=day)
                if ph.get("continuity_broken"):
                    feeds.backfill_missing_live_bars(today=day)
                    ph = feeds.panel_health(today=day)
                if ph.get("continuity_broken"):
                    panel_gap_blocked = True
                    panel_gap_reason = f"日线连续性熔断阻断自动挂单：{ph.get('message')}"
                    notes.append(panel_gap_reason)
            except Exception:  # noqa: BLE001
                pass
        gate = emotion_gate(day, intraday=True)
        # 外盘情绪风偏（日经/韩国KOSPI/美股/A50）+ 国内快讯催化/雷区 + 昨日龙虎榜活人情绪（严格因果 T-1，测试下不外网阻塞）
        overseas_snap: dict[str, Any] = {"available": False, "sentiment": {"size_mult": 1.0}}
        news_snap: dict[str, Any] = {"available": False, "catalyst_by_theme": {}, "stock_alerts": {}}
        lhb_snap: dict[str, Any] = {"available": False, "market_sentiment": {}, "stock_profiles": {}}
        if frames is None:  # 生产实盘 sync 才拉取外盘、快讯与昨日龙虎榜；单元测试传 mock frames 时保持纯离线隔离
            try:
                from backend import overseas as _ovs
                overseas_snap = _ovs.snapshot(day=day)
            except Exception:  # noqa: BLE001
                overseas_snap = {"available": False, "sentiment": {"size_mult": 1.0}}
            try:
                from backend import news_feed as _nf
                news_snap = _nf.snapshot(day=day)
            except Exception:  # noqa: BLE001
                news_snap = {"available": False, "catalyst_by_theme": {}, "stock_alerts": {}}
            try:
                from backend import lhb as _lhb
                lhb_snap = _lhb.causal_snapshot(day, lookback_days=5, allow_fetch=True)
            except Exception:  # noqa: BLE001
                lhb_snap = {"available": False, "market_sentiment": {}, "stock_profiles": {}}

        # 市场级冰点闸门 + 自适应普跌细分（随近 250 日滚动分布 & 实盘因果成交动态演化，不写死阈值）
        try:
            from backend import ice_gate
            ice = ice_gate.assess(
                day, persist=True, intraday=True,
                emotion=gate,
                overseas=overseas_snap if overseas_snap.get("available") else None,
                causal_trades=state.get("trades") or [],
            )
        except Exception as exc:  # noqa: BLE001 - 冰点不可用不阻塞主流程
            ice = {"available": False, "is_ice": False, "size_factor": 1.0,
                   "level": None, "reason": f"{type(exc).__name__}: {exc}"}

        ice_factor = float(ice.get("size_factor") or 1.0)
        dr = (ice.get("decline_regime") or {}) if ice.get("available") else {}
        ovs_sent = (overseas_snap.get("sentiment") or {}) if overseas_snap.get("available") else {}
        ovs_mult = float(ovs_sent.get("size_mult") or 1.0)
        hb_pulse = (gate.get("high_board_pulse") or {}) if gate.get("available") else {}
        hb_mult = float(hb_pulse.get("size_mult") or 1.0)
        formula_parts: list[str] = []
        if dr.get("active") and isinstance(dr.get("adapted_mult"), (int, float)):
            # 普跌细分已融合滚动250日修复率与实盘因果反馈；外盘冲击型不再重复乘 ovs_mult 避免双重折损
            reg_mult = float(dr["adapted_mult"])
            eff_ovs = 1.0 if dr.get("regime") == "external_shock" else ovs_mult
            size_factor = round(max(0.15, min(1.0, reg_mult * eff_ovs * hb_mult)), 3)
            formula_parts.append(f"{dr.get('regime_cn')} ×{reg_mult:.2f}")
            if eff_ovs < 1.0:
                formula_parts.append(f"外盘 ×{eff_ovs:.2f}")
        else:
            reg_mult = None
            eff_ovs = ovs_mult
            size_factor = round(max(0.20, min(1.0, ice_factor * ovs_mult * hb_mult)), 3)
            formula_parts.append(f"冰点基础 ×{ice_factor:.2f}")
            if ovs_mult < 1.0:
                formula_parts.append(f"外盘 ×{ovs_mult:.2f}")
        if hb_mult < 1.0:
            formula_parts.append(f"{hb_pulse.get('state_cn', '高标负反馈')} ×{hb_mult:.2f}")

        # 账户级连亏熔断（严格因果：只统计 exit_date < day）+ 情绪高潮/退潮初期自适应守卫
        cb = account_circuit_breaker(state.get("trades") or [], day)
        override_day = str(state.get("circuit_breaker_override_day") or "").replace("-", "")
        if override_day and override_day == str(day).replace("-", "") and cb.get("mode") != "normal":
            cb = {
                **cb,
                "mode": "override",
                "mode_cn": "今日手动解除熔断",
                "size_mult": 1.0,
                "block_new_orders": False,
                "only_grade_a": False,
                "reason": f"用户手动解除 {day} 当日熔断（原态：{cb.get('mode_cn')}，回撤 {cb.get('drawdown_pct', 0):.1%}），次日自动恢复风控",
            }
        # 提前提取当日锚定兔子按题材分组（用于高低切识别，避免旧高标跌停误杀新主线多兔启动）
        today_rabbit_themes: dict[str, list[str]] = {}
        if frames is None and fr:
            try:
                _allzt_pre = collect_all_zt(fr)
                _rabs_pre = [c for c, g in _allzt_pre.items() if is_rabbit(g)]
                if _rabs_pre:
                    _tags_pre = _theme_tags(day, codes=_rabs_pre)
                    from backend import theme_match as _tm_pre
                    _grp_pre = _tm_pre.group_by_theme([
                        {"code": c, "name": _allzt_pre[c].get("name") or "",
                         "tags": _tags_pre.get(c) or [], "industry": None, "streak": 2}
                        for c in _rabs_pre
                    ])
                    for _gg in (_grp_pre.get("groups") or []):
                        if _gg.get("level") in ("axis", "tag"):
                            today_rabbit_themes[str(_gg.get("theme") or "")] = list(_gg.get("codes") or [])
            except Exception:  # noqa: BLE001
                today_rabbit_themes = {}
        macro_policy_alert = news_snap.get("macro_policy_alert") or {}
        major_bull_catalyst = bool(
            macro_policy_alert.get("active")
            or sum(1 for ni in (news_snap.get("catalyst_by_theme") or {}).values()
                   if float(ni.get("score") or 0.0) >= 2.0) >= 3
        )
        if macro_policy_alert.get("active"):
            try:
                from backend import overseas as _ov_burst
                _ov_burst.trigger_burst_mode(900.0)
            except Exception:  # noqa: BLE001
                pass
            if macro_policy_alert.get("note"):
                notes.append(str(macro_policy_alert["note"]))
        youzi_wr = None
        if lhb_snap.get("available"):
            yp = lhb_snap.get("youzi_premium") or {}
            if isinstance(yp.get("win_rate"), (int, float)):
                youzi_wr = float(yp["win_rate"])
            elif lhb_snap.get("stock_profiles"):
                try:
                    from backend import lhb as _lhb_yp, archive as _arch_yp
                    q_latest = _arch_yp.latest_frame(day, "quotes")
                    if q_latest is not None and q_latest.height > 0:
                        pct_map = {
                            str(r.get("ts_code") or "").zfill(6): float(r["pct"])
                            for r in q_latest.to_dicts()
                            if r.get("ts_code") and r.get("pct") is not None
                        }
                        yp_live = _lhb_yp.compute_youzi_premium(lhb_snap["stock_profiles"], pct_map)
                        if yp_live.get("available") and isinstance(yp_live.get("win_rate"), (int, float)):
                            youzi_wr = float(yp_live["win_rate"])
                            lhb_snap["youzi_premium"] = yp_live
                except Exception:  # noqa: BLE001
                    pass
        # 实时全市场炸板率与放量比（P1-2 修复：融合东财官方 zb 炸板池与涨停池帧差，取较大值防早盘秒炸低估）
        ice_m = (ice.get("metrics") or {}) if ice.get("available") else {}
        rt_broken_rate = None
        if fr:
            try:
                seen_zt: set[str] = set()
                for _fi in fr:
                    _frows, _ = _pool_rows(_fi)
                    for _frw in _frows:
                        _fc = str(_frw.get("ts_code") or "").strip()
                        if _fc:
                            seen_zt.add(_fc.zfill(6))
                last_rows_br, _ = _pool_rows(fr[-1])
                in_last_zt = {str(_frw.get("ts_code") or "").strip().zfill(6)
                              for _frw in last_rows_br if _frw.get("ts_code")}
                frame_diff_br = (max(0, len(seen_zt) - len(in_last_zt)) / len(seen_zt)) if seen_zt else None
                zb_blk = fr[-1].get("zb") or {}
                zb_cnt = len(zb_blk.get("all") or zb_blk.get("top") or []) if isinstance(zb_blk, dict) else 0
                zb_br = (zb_cnt / (len(in_last_zt) + zb_cnt)) if (zb_cnt > 0 and (len(in_last_zt) + zb_cnt) > 0) else None
                cands_br = [v for v in (frame_diff_br, zb_br) if v is not None]
                if cands_br:
                    rt_broken_rate = round(max(cands_br), 4)
            except Exception:  # noqa: BLE001
                rt_broken_rate = None
        if rt_broken_rate is None and isinstance(ice_m.get("broken_rate"), (int, float)):
            rt_broken_rate = float(ice_m["broken_rate"])
        rt_vol_ratio = (
            float(ice_m["vol_ratio_5d"]) if isinstance(ice_m.get("vol_ratio_5d"), (int, float))
            else (float(ice_m["vol_ratio"]) if isinstance(ice_m.get("vol_ratio"), (int, float)) else None)
        )
        market_reaction = (overseas_snap.get("market_reaction") or {}) if overseas_snap.get("available") else {}
        if market_reaction.get("sudden_surge") and market_reaction.get("note"):
            notes.append("⚡ 指数/期货突拉共振：" + str(market_reaction["note"]))

        climax_ebb = market_climax_and_ebb_guard(
            day, gate, ice, frames=fr, rabbit_themes=today_rabbit_themes or None,
            youzi_live_win_rate=youzi_wr,
            major_bull_catalyst=major_bull_catalyst,
            broken_rate=rt_broken_rate,
            vol_ratio_20=rt_vol_ratio,
            market_reaction=market_reaction or None,
        )
        if frames is None and float(climax_ebb.get("trap_size_mult") or 1.0) < 1.0:
            _tm_trap = float(climax_ebb["trap_size_mult"])
            size_factor = round(max(0.15, size_factor * _tm_trap), 3)
            formula_parts.append(f"高炸板兑现收紧 ×{_tm_trap:.2f}")
        if frames is None and float(climax_ebb.get("ebb_trial_mult") or 1.0) < 1.0:
            _tm_ebb = float(climax_ebb["ebb_trial_mult"])
            size_factor = round(max(0.15, size_factor * _tm_ebb), 3)
            formula_parts.append(f"高低切试错 ×{_tm_ebb:.2f}")
        if frames is None and cb.get("size_mult", 1.0) < 1.0:
            if cb.get("block_new_orders"):
                size_factor = 0.0
                formula_parts.append(f"账户风控({cb.get('mode_cn')}) ×0.00")
            else:
                cb_eff_mult = float(cb["size_mult"])
                if climax_ebb.get("tailwind_breakout") and climax_ebb.get("defensive_relax_mult"):
                    cb_eff_mult = max(cb_eff_mult, float(climax_ebb["defensive_relax_mult"]))
                    formula_parts.append(f"账户风控(顺风松绑) ×{cb_eff_mult:.2f}")
                else:
                    formula_parts.append(f"账户风控({cb.get('mode_cn')}) ×{cb_eff_mult:.2f}")
                size_factor = round(max(0.10, size_factor * cb_eff_mult), 3)

        sizing_info: dict[str, Any] = {
            "effective_size_factor": size_factor,
            "ice_factor": ice_factor,
            "regime_mult": reg_mult,
            "overseas_mult": eff_ovs,
            "high_board_mult": hb_mult,
            "circuit_breaker": cb,
            "climax_ebb_guard": climax_ebb,
            "panel_gap_blocked": panel_gap_blocked,
            "lhb_sentiment": (lhb_snap.get("market_sentiment") if lhb_snap.get("available") else None),
            "formula": " × ".join(formula_parts) + f" → 最终生效 ×{size_factor:.2f}",
        }

        skipped_candidates: list[dict[str, Any]] = []
        leader_heat_map: dict[str, dict[str, Any]] = {}
        if panel_gap_blocked:
            groups, collect_notes = {}, [panel_gap_reason]
        elif gate.get("available") and gate.get("blocked"):
            groups, collect_notes = {}, [
                "情绪闸门拦截：" + "；".join(gate.get("reasons") or [])
                + "（按悟空真经第一步，情绪不佳时停手/控仓）"]
        elif frames is None and cb.get("block_new_orders"):
            groups, collect_notes = {}, [cb["reason"]]
        elif frames is None and climax_ebb.get("ebb_block"):
            groups, collect_notes = {}, list(climax_ebb.get("reasons") or [])
        else:
            groups, collect_notes = collect_guitu_targets(fr, day)
            if groups and frames is None:
                # 生产聚焦过滤：按买点质量 + 美股/国内消息催化排序，剔除硬利空/烂板，聚焦前排 Top N
                # 严格扣除当日已存在有效委托，杜绝跨次 sync 累积摊大饼（如 11-24 单日 26 单）
                try:
                    from backend import buy_signals as _bs
                    cat_scores: dict[str, float] = {}
                    for th, ci in (overseas_snap.get("catalyst_by_theme") or {}).items():
                        cat_scores[th] = cat_scores.get(th, 0.0) + float(ci.get("catalyst_score") or 0.0)
                    for th, ni in (news_snap.get("catalyst_by_theme") or {}).items():
                        cat_scores[th] = cat_scores.get(th, 0.0) + max(-2.0, min(2.0, float(ni.get("score") or 0.0)))
                    # 高标同题材引领加分（仅在近 60 日实证传导胜率 >=50% 时才生效，不写死盲目追高）
                    for th in (hb_pulse.get("lead_themes") or []):
                        cat_scores[th] = cat_scores.get(th, 0.0) + 0.5
                    # 昨日龙虎榜游资主攻板块观察池：给命中昨日活人游资主攻板块的次日候选注入催化加分（走动态催化权重）
                    lhb_watch_themes = (lhb_snap.get("attack_themes") or []) if lhb_snap.get("available") else []
                    for _lt in lhb_watch_themes:
                        _lth = str(_lt.get("theme") or "")
                        if _lth:
                            cat_scores[_lth] = cat_scores.get(_lth, 0.0) + 1.0
                    if lhb_watch_themes:
                        collect_notes.append(
                            f"昨日龙虎榜游资主攻观察池（{lhb_snap.get('latest_day')}）："
                            + "、".join(f"{x['theme']}({x['n_stocks']}只/{x['net_yi']:+.2f}亿)" for x in lhb_watch_themes[:4])
                        )

                    # 踏空反事实反哺：近期市场走出漏网主线龙头的题材，因果注入催化分（不破核心选股纪律）
                    try:
                        from backend import evolution as _evo_heat
                        leader_heat_map = _evo_heat.causal_theme_leader_heat(day, state.get("orders") or {})
                        for th, hi in leader_heat_map.items():
                            cat_scores[th] = cat_scores.get(th, 0.0) + float(hi.get("catalyst_bonus") or 0.0)
                    except Exception:  # noqa: BLE001
                        leader_heat_map = {}

                    existing_today = [
                        o for oid, o in (state.get("orders") or {}).items()
                        if oid.startswith(f"{day}|")
                        and o.get("status") != "cancelled"
                        and not o.get("invalid_historical_bug")
                    ]
                    existing_codes = {str(o.get("ts_code") or "").zfill(6) for o in existing_today}
                    cancelled_codes = {
                        str(o.get("ts_code") or "").zfill(6)
                        for oid, o in (state.get("orders") or {}).items()
                        if oid.startswith(f"{day}|") and o.get("status") == "cancelled"
                    }
                    invalid_codes = {
                        str(o.get("ts_code") or "").zfill(6)
                        for oid, o in (state.get("orders") or {}).items()
                        if oid.startswith(f"{day}|") and o.get("invalid_historical_bug")
                    }
                    existing_theme_cnt: dict[str, int] = {}
                    for _eo in existing_today:
                        _eth = str(_eo.get("guitu_theme") or _eo.get("industry") or "")
                        if _eth:
                            existing_theme_cnt[_eth] = existing_theme_cnt.get(_eth, 0) + 1

                    eff_max_total = int(climax_ebb.get("max_total") or MAX_DAILY_ORDERS)
                    eff_max_per_theme = int(climax_ebb.get("max_per_theme") or MAX_PER_THEME_ORDERS)
                    if cb.get("only_grade_a") and not climax_ebb.get("tailwind_breakout"):
                        eff_max_total = min(eff_max_total, 2)
                        eff_max_per_theme = min(eff_max_per_theme, 1)

                    # 跨题材顺风解耦：当高潮/防守把单日锁死在 2 只时，若同时满足
                    # 「高标强势晋级 + 零跌停核按钮 + 昨日溢价 ≥ -1% + 龙虎榜游资活人净买为正 + 命中 ≥ 2 个题材」，
                    # 允许弹性放宽至 3 只，但新增名额严格限制给「未占满的异题材前排」（绝不往同一题材硬加第 3 只）
                    all_themes = {str(g.get("guitu_theme") or g.get("industry") or "")
                                  for g in groups.values() if (g.get("guitu_theme") or g.get("industry"))}
                    lhb_net_yi = float(((lhb_snap.get("market_sentiment") or {}).get("total_net_yi") or 0.0)
                                       if lhb_snap.get("available") else 0.0)
                    cross_theme_relaxed = False
                    if (eff_max_total == 2
                            and len(all_themes) >= 2
                            and hb_pulse.get("state") == "lead"
                            and float(gate.get("limit_down_share") or 0.0) <= 0.0
                            and (gate.get("premium") is not None and float(gate.get("premium")) >= -1.0)
                            and lhb_net_yi > 0.0):
                        eff_max_total = 3
                        cross_theme_relaxed = True
                        collect_notes.append(
                            f"跨题材顺风解耦（高标晋级+零核按钮+游资净买+{lhb_net_yi:.2f}亿+多题材共振）"
                            "→ 单日上限弹性放宽至 3 只（限异题材前排，仍保留防守仓位系数）"
                        )

                    # 给候选挂载近 5 日事前龙虎榜画像（T-1 及之前）供买点评分与进化引擎使用
                    lhb_profs = (lhb_snap.get("stock_profiles") or {}) if lhb_snap.get("available") else {}
                    for _c, _g in groups.items():
                        if _c in lhb_profs:
                            _g["lhb_profile"] = lhb_profs[_c]
                    if lhb_snap.get("available") and (lhb_snap.get("market_sentiment") or {}).get("note"):
                        collect_notes.append(
                            f"昨日龙虎榜活人情绪（{lhb_snap.get('latest_day')}）："
                            + str(lhb_snap["market_sentiment"]["note"])
                        )

                    # 严格因果计算外盘/政策催化动态权重 ∈ [0.0, 1.5]（不再写死重权）
                    cat_weight = 1.0
                    try:
                        from backend import evolution as _evo_cat
                        cat_weight = _evo_cat.causal_catalyst_weight_before(
                            day, state.get("orders") or {}, clean_trades(state.get("trades") or [], state.get("orders"))
                        )
                    except Exception:  # noqa: BLE001
                        cat_weight = 1.0

                    # 已存在委托保留刷新，新增候选严格受剩余名额约束；已撤单/脏单自动释放槽位顺延递补
                    keep_existing = {c: g for c, g in groups.items() if c in existing_codes}
                    new_cands = {
                        c: g for c, g in groups.items()
                        if c not in existing_codes
                        and str(c).zfill(6) not in cancelled_codes
                        and str(c).zfill(6) not in invalid_codes
                    }
                    # 跨题材解耦时，已达单题材上限的题材不抢占新增的跨题材名额
                    if cross_theme_relaxed and existing_theme_cnt:
                        eligible_new = {}
                        for _nc, _ng in new_cands.items():
                            _nth = str(_ng.get("guitu_theme") or _ng.get("industry") or "")
                            if existing_theme_cnt.get(_nth, 0) >= eff_max_per_theme:
                                skipped_candidates.append({
                                    "ts_code": _nc, "name": _ng.get("name") or "",
                                    "theme": _nth,
                                    "reason": f"同题材「{_nth}」已有 {existing_theme_cnt[_nth]} 只有效委托，跨题材名额留给其他主线",
                                })
                            else:
                                eligible_new[_nc] = _ng
                    else:
                        eligible_new = new_cands
                    rem_slots = max(0, eff_max_total - len(existing_today))
                    if rem_slots > 0 and eligible_new:
                        picked_new, _sk_extra = _bs.rank_and_focus_candidates(
                            eligible_new,
                            max_per_theme=eff_max_per_theme,
                            max_total=rem_slots,
                            catalyst_by_theme=cat_scores,
                            catalyst_weight=cat_weight,
                            stock_alerts=news_snap.get("stock_alerts") or {},
                            drop_rejected=True,
                            cancelled_codes=cancelled_codes,
                            invalid_codes=invalid_codes,
                            existing_theme_counts=existing_theme_cnt,
                        )
                        skipped_candidates.extend(_sk_extra)
                        for _pc, _pg in picked_new.items():
                            _th = str(_pg.get("guitu_theme") or _pg.get("industry") or "")
                            if _th in cat_scores:
                                _pg["catalyst_score"] = round(float(cat_scores[_th]), 2)
                        groups = {**keep_existing, **picked_new}
                    else:
                        groups = keep_existing
                        for _c, _g in eligible_new.items():
                            skipped_candidates.append({
                                "ts_code": _c, "name": _g.get("name") or "",
                                "theme": str(_g.get("guitu_theme") or ""),
                                "reason": f"当日有效委托已达上限 {eff_max_total} 只，严控摊大饼",
                            })
                    if climax_ebb.get("reasons"):
                        collect_notes.extend(climax_ebb["reasons"])
                    if cb.get("mode") != "normal":
                        collect_notes.append(cb["reason"])
                    if skipped_candidates:
                        collect_notes.append(
                            f"买点聚焦与雷区过滤：保留前排 {len(groups)} 只，过滤/顺延 {len(skipped_candidates)} 只"
                        )
                except Exception:  # noqa: BLE001
                    pass
            if gate.get("available"):
                collect_notes.append(
                    f"情绪闸门通过：昨日高标 {gate.get('n_high')} 只今日均涨 "
                    f"{gate.get('avg_ret'):+.2f}%、跌停占比 "
                    f"{gate.get('limit_down_share'):.0%}"
                    + (f"、溢价 {gate.get('premium'):+.2f}%"
                       if gate.get("premium") is not None else ""))
        if ice.get("available") and ice.get("is_ice"):
            collect_notes.append(
                f"市场冰点 L={ice.get('level')}（" + "；".join(ice.get("reasons") or [])
                + f"）→ 冰点系数 {ice_factor:.2f}"
                "（用户 2026-11-23 裁定：冰点不绝对空仓）")
        elif ice.get("available"):
            lv = ice.get("level")
            dl = ice.get("dim_levels") or {}
            dim_cn = {"down_share": "下跌占比", "limit_down_n": "跌停家数",
                      "dt_ratio": "跌涨停比", "broken_hi_n": "高标断板"}
            hi_dims = [f"{dim_cn.get(k, k)}={v}" for k, v in dl.items() if v in ("L4", "L5")]
            if lv in ("L4", "L5") and hi_dims:
                collect_notes.append(
                    f"市场非冰点（仅单维偏高：{'/'.join(hi_dims)}，未触发多维冰点）→ 正常仓位（系数 {ice_factor:.2f}）"
                )
            else:
                collect_notes.append(
                    f"市场非冰点 L={lv} → 正常仓位（系数 {ice_factor:.2f}）"
                )
        if dr.get("active"):
            sec_txt = f"（兼 {dr.get('secondary_cn')}）" if dr.get("secondary_cn") else ""
            collect_notes.append(
                f"自适应普跌细分：{dr.get('regime_cn')}{sec_txt} · "
                f"{dr.get('adaptation_note')}（不写死静态阈值，随近 250 日分布与实盘因果更新）"
            )
        if overseas_snap.get("available") and ovs_sent.get("score") is not None:
            collect_notes.append(
                f"外盘风偏（日韩美股/A50）：{ovs_sent.get('risk_label')} "
                f"情绪分 {ovs_sent.get('score'):+.1f}（系数 ×{ovs_mult:.2f} → 综合仓位系数 {size_factor:.2f}）"
            )
        notes.extend(collect_notes)
        if not fr:
            notes.append(f"{day} 无涨停池留档帧；本日不产生任何委托单（不补数、不猜测）。")

        # 1) 委托单：新单 or 更新已有单的开板状态（单向迁移）
        # 2026-11-25：滚动真实净值 + 冰点降仓 + 严格因果信息素反馈系数 + 总仓 80% 上限。
        held = sum(float(p.get("notional") or 0.0)
                   for p in state["positions"].values()
                   if p.get("status") != "closed" and not is_invalid_record(p))
        clean_t_list = clean_trades(state.get("trades") or [], state.get("orders"))
        realized_so_far = sum(
            float(t.get("net_return")) * (float(t.get("notional") or 0.0) or 100_000.0)
            for t in clean_t_list
            if isinstance(t.get("net_return"), (int, float))
        ) + sum(
            float(p.get("partial_realized_pnl") or 0.0)
            for p in state["positions"].values()
            if p.get("status") != "closed" and not is_invalid_record(p) and p.get("partial_closed")
        )
        current_capital = max(0.0, CAPITAL_YUAN + realized_so_far) if (clean_t_list or realized_so_far != 0.0) else CAPITAL_YUAN
        evo_mults: dict[str, float] = {}
        evo_meta: dict[str, dict[str, Any]] = {}
        evo_high_turn_delta: float | None = None
        evo_rotten_delta: float | None = None
        # 痛点 I 修复：在 sync 中只读回填未成交/撤单遗憾收益（严格因果），打通侦察蚁与撤单反事实闭环
        enriched_orders_for_evo: Any = state.get("orders") or {}
        if frames is None:
            try:
                from backend import evolution as _evo_enr
                enriched_orders_for_evo = _evo_enr.enrich_unfilled_regret(state.get("orders") or {})
            except Exception:  # noqa: BLE001
                enriched_orders_for_evo = state.get("orders") or {}

        # 痛点 J 修复（冷启动防污染护栏）：统计新口径（≥2026-11-25 龟兔聚焦+自适应卖点）的独立结算交易日与笔数；
        # 样本未达标（<10笔 或 <5个独立日）前，进化引擎仅做影子观察记录，实盘 plan_orders 锁死 ×1.00 不注入旧口径噪音
        _EVO_CLEAN_START = "20261125"
        _invalid_oids = {
            oid0 for oid0, o0 in (state.get("orders") or {}).items()
            if o0.get("invalid_historical_bug")
        }
        _clean_trades = [
            t for t in (state.get("trades") or [])
            if t.get("id") not in _invalid_oids
            and not t.get("invalid_historical_bug")
            and str(t.get("entry_date") or "").replace("-", "") >= _EVO_CLEAN_START
            and str(t.get("exit_date") or "").replace("-", "") < str(day).replace("-", "")
        ]
        _clean_days = {str(t.get("exit_date") or "").replace("-", "") for t in _clean_trades if t.get("exit_date")}
        evo_live_ready = bool(len(_clean_trades) >= 10 and len(_clean_days) >= 5)

        if groups:
            try:
                from backend import evolution as _evo
                fac_evo = _evo.causal_factor_scores_before(
                    day, enriched_orders_for_evo, state.get("trades") or [],
                    groups, all_days=state.get("days") or None,
                )
                evo_high_turn_delta = fac_evo.get("high_turn_delta") if evo_live_ready else None
                evo_rotten_delta = fac_evo.get("rotten_delta") if evo_live_ready else None
                fac_by_code = fac_evo.get("by_code") or {}
                for _c, _g in groups.items():
                    _c6 = str(_c).zfill(6)
                    _pk = _evo.make_path_key(_g)
                    _th = str(_g.get("guitu_theme") or _g.get("industry") or "")
                    _hb = float((leader_heat_map.get(_th) or {}).get("heat_bonus") or 0.0)
                    _finfo = fac_by_code.get(_c6)
                    if _finfo or _hb > 0:
                        base_m = float(_finfo["size_mult"]) if (_finfo and evo_live_ready) else 1.0
                        comb_m = round(max(0.30, min(1.25, base_m + _hb)), 3)
                        # P1-5 修复：冷启动未达标时，evo_mults 严格留空（×1.00），确保日志与实盘仓位 100% 一致
                        if evo_live_ready:
                            evo_mults[_c] = comb_m
                        evo_meta[_c] = {
                            "evo_engine": "12_factor_bayesian" if evo_live_ready else "12_factor_shadow_coldstart",
                            "evo_path": _pk,
                            "evo_weighted_delta": _finfo["weighted_delta"] if _finfo else 0.0,
                            "evo_n_factors": _finfo["n_matched"] if _finfo else 0,
                            "evo_factors": _finfo["factors"] if _finfo else {},
                            "evo_size_mult": comb_m if evo_live_ready else 1.0,
                            "evo_shadow_mult": comb_m,
                            "leader_heat_bonus": round(_hb, 3),
                        }
                if not evo_live_ready:
                    notes.append(
                        f"进化冷启动保护（新口径已结算 {len(_clean_trades)} 笔 / {len(_clean_days)} 个独立日"
                        " < 10笔/5日）→ 12维信息素仅影子观察，实盘仓位锁死 ×1.00 不受旧口径污染"
                    )
            except Exception:  # noqa: BLE001
                evo_mults = {}
        # 识别真主线爆发顺风候选（情绪健康无核按钮 + 多兔共振≥2 + A级前排，避免顺风日把主线前排砍成残仓）
        mainline_boost_codes: set[str] = set()
        if (groups and not ice.get("is_ice")
                and hb_pulse.get("state") != "drag"
                and (gate.get("premium") is None or float(gate.get("premium")) >= -1.0)):
            for _c, _g in groups.items():
                rabs = _g.get("guitu_rabbits") or []
                bq_pre = _g.get("buy_quality_pre") or {}
                if isinstance(rabs, list) and len(rabs) >= 2 and bq_pre.get("grade") == "A":
                    mainline_boost_codes.add(str(_c).zfill(6))

        budget = plan_orders(list(groups.values()), size_factor=size_factor,
                             capital=current_capital, held_notional=held,
                             evo_mults=evo_mults or None,
                             mainline_boost_codes=mainline_boost_codes or None)
        size_by_code = {b["code"]: b for b in budget}
        if groups:
            total_plan = sum(b["size_yuan"] for b in budget)
            notes.append(
                f"仓位分配：滚动可用本金 {current_capital:,.0f} 元、已持仓 {held:,.0f} 元、"
                f"本次计划 {total_plan:,.0f} 元（冰点系数 {size_factor:.2f}，"
                f"信息素因果命中 {len(evo_mults)} 只，总仓上限 {MAX_GROSS_PCT:.0%}）")
        has_prior_today_orders = any(
            oid0.startswith(f"{day}|") for oid0 in (state.get("orders") or {})
        )
        for code, g in groups.items():
            oid = f"{day}|{code}|B1"
            cur = state["orders"].get(oid)
            b = size_by_code.get(code) or {}
            # 实盘防时序穿越：若当日已有前期委托，盘中因空位/新封而新加入的挂单起点锁定为当前最新帧，绝不倒签 09:25；
            # 且若已进入 14:57 收盘集合竞价或盘后，严禁再补生成无意义盘后委托
            g_order = g
            if frames is None and cur is None and has_prior_today_orders and fr:
                last_ts = str(fr[-1].get("ts") or "")
                hhmm_last = last_ts.split("T", 1)[1][:5] if "T" in last_ts else ""
                if hhmm_last >= "14:57":
                    skipped_candidates.append({
                        "ts_code": code,
                        "name": g.get("name") or "",
                        "theme": str(g.get("guitu_theme") or ""),
                        "reason": f"已进入收盘/盘后时段({hhmm_last})，禁止新增尾盘/盘后委托",
                    })
                    continue
                if last_ts and g.get("frame_ts") != last_ts:
                    g_order = dict(g)
                    g_order["frame_ts"] = last_ts
                    g_order["first"] = g.get("last") or g.get("first")
            new = build_order(day, code, g_order, size_factor=size_factor,
                              size_yuan=b.get("size_yuan"))
            if b:
                new["budget_reason"] = b.get("budget_reason")
            if g.get("catalyst_score") is not None:
                new["catalyst_score"] = g.get("catalyst_score")
            if dr.get("active"):
                new["decline_regime"] = dr.get("regime")
            if code in evo_meta:
                new.update(evo_meta[code])
            if cur is None:
                state["orders"][oid] = new
                cur = new
            else:
                # 只更新证据字段；filled 一旦成立不回退（Q037：开板过即已成交）
                cur["n_frames"] = new["n_frames"]
                cur["broken_max"] = new["broken_max"]
                # 换手/行业也要随末帧更新，否则委托单显示首帧值、龟兔判定用末帧值，
                # 同一行出现两个不同的换手率（13:36 实测 2.45% vs 2.82%）。
                cur["turnover"] = new["turnover"]
                cur["industry"] = new["industry"]
                # 题材标签是分组依据（2026-11-24 新增）；但兔子身份已在决策时刻 t 因果固化，
                # 严禁因盘后兔子炸板把已建仓/已挂单的兔子锚定抹掉变成「无兔孤儿单」。
                if not cur.get("guitu_theme"):
                    cur["guitu_theme"] = new.get("guitu_theme")
                if not cur.get("guitu_rabbits"):
                    cur["guitu_rabbits"] = new.get("guitu_rabbits")
                if not cur.get("guitu_rabbit_codes"):
                    cur["guitu_rabbit_codes"] = new.get("guitu_rabbit_codes")
                if not cur.get("rabbit_snapshot_at_entry"):
                    cur["rabbit_snapshot_at_entry"] = new.get("rabbit_snapshot_at_entry")
                # 封板质量观察字段（2026-11-25 新增）：旧单缺字段时补上，
                # 只做展示，不参与选股/成交判定。
                for _k in ("turn_at_seal", "first_seal_hhmmss", "seal_quality",
                           "seal_quality_note", "seal_quality_basis"):
                    if _k in new:
                        cur[_k] = new[_k]
                cur["queue"] = new["queue"] if cur["status"] != "filled" else cur["queue"]
                # 挂单起点只在首次生成时固化，后续帧不得覆盖（否则又是未来信息）
                cur["broken_at_entry"] = cur.get("broken_at_entry",
                                                 new.get("broken_at_entry"))
                # 角色按当日全帧累计重算；一字兔子盘中开板转乌龟时留痕（Q037 反而可成交）
                old_role = (cur.get("guitu") or {}).get("role")
                cur["guitu"] = new["guitu"]
                if old_role and old_role != new["guitu"]["role"]:
                    cur["role_changed"] = (
                        f"{guitu_roles.ROLE_CN.get(old_role, old_role)} → "
                        f"{new['guitu']['role_cn']}（当日全帧重算）")
                # 成交判定改由 advance_fills 逐帧推进，这里不再用 new["status"]
        # 1.5) 昨日首板池 → 今日一进二临封扫板双轨买点（影子留档 + 五档薄卖一可执行扫板）
        sweep_store = state.setdefault("sweep_shadow_orders", {})
        if (frames is None and fr and not panel_gap_blocked
                and not (gate.get("available") and gate.get("blocked"))
                and not cb.get("block_new_orders")
                and not climax_ebb.get("ebb_block")):
            try:
                sweep_hits = scan_today_first_to_second_sweep(day, fr)
                eff_max_tot_sw = int(climax_ebb.get("max_total") or MAX_DAILY_ORDERS)
                eff_max_th_sw = int(climax_ebb.get("max_per_theme") or MAX_PER_THEME_ORDERS)
                for sh in sweep_hits:
                    sc = str(sh.get("ts_code") or "").zfill(6)
                    sid = f"{day}|{sc}|SWEEP"
                    if sid not in sweep_store:
                        sweep_store[sid] = {
                            "id": sid, "day": day, "strategy": STRATEGY,
                            "ts_code": sc, "name": sh.get("name") or "",
                            "guitu_theme": sh.get("theme") or "",
                            "guitu_rabbit_codes": sh.get("rabbits") or [],
                            "exec_mode": "sweep_shadow",
                            "entry_date": day,
                            "entry_price": sh.get("limit_price") or sh.get("price"),
                            "trigger_price": sh.get("price"),
                            "trigger_pct": sh.get("pct"),
                            "ask1_amt_wan": sh.get("ask1_amt_wan"),
                            "executable": bool(sh.get("executable")),
                            "triggered_at": datetime.now().isoformat(timespec="seconds"),
                            "note": sh.get("note") or "",
                        }
                    # 若五档薄卖一吃单加速达标且当日剩余名额充足 → 按真实买单滑点价建仓并后续帧跟踪封板
                    oid_sw = f"{day}|{sc}|B1"
                    if sh.get("executable") and oid_sw not in state["orders"]:
                        active_today_sw = [
                            o for k0, o in state["orders"].items()
                            if k0.startswith(f"{day}|")
                            and o.get("status") != "cancelled"
                            and not o.get("invalid_historical_bug")
                        ]
                        th_sw = str(sh.get("theme") or "")
                        th_cnt_sw = sum(
                            1 for o in active_today_sw
                            if str(o.get("guitu_theme") or "") == th_sw
                        )
                        if len(active_today_sw) < eff_max_tot_sw and th_cnt_sw < eff_max_th_sw:
                            lim_px_sw = float(sh.get("limit_price") or sh.get("price") or 0.0)
                            ask1_sw = sh.get("ask1")
                            if ask1_sw is not None and float(ask1_sw) > 0 and lim_px_sw > 0:
                                sw_px = round(min(lim_px_sw, float(ask1_sw) + 0.01), 2)
                            else:
                                sw_px = round(float(sh.get("price") or lim_px_sw or 0.0), 2)
                            sw_budget = POSITION_SIZE_YUAN * max(0.20, min(1.0, size_factor))
                            sw_qty, sw_notional = position_size(sw_px, size_yuan=sw_budget)
                            if sw_px > 0 and sw_qty > 0:
                                now_iso = datetime.now().isoformat(timespec="seconds")
                                state["orders"][oid_sw] = {
                                    "id": oid_sw, "day": day, "strategy": STRATEGY,
                                    "ts_code": sc, "name": sh.get("name") or "",
                                    "industry": "", "turnover": sh.get("turnover"),
                                    "guitu_theme": th_sw,
                                    "guitu_rabbits": sh.get("rabbits") or [],
                                    "guitu_rabbit_codes": sh.get("rabbits") or [],
                                    "entry_date": day, "entry_price": sw_px,
                                    "qty": sw_qty, "notional": sw_notional,
                                    "size_factor": size_factor,
                                    "size_budget_yuan": round(sw_budget, 2),
                                    "entry_frame_ts": (fr[-1].get("ts") if fr else now_iso),
                                    "entry_rule": "昨日首板池一进二临封扫板（同题材一字兔锚定+薄卖一滑点扫板）",
                                    "exit_rule": EXIT_RULE,
                                    "exec_mode": "pre_seal_sweep",
                                    "status": "filled",
                                    "fill_frame_ts": now_iso,
                                    "fill_price": sw_px,
                                    "fill_confidence": "sweep_live",
                                    "fill_reason": sh.get("note") or f"一进二临封滑点@{sw_px:.2f}扫板建仓（持续跟踪封板）",
                                    "broken_at_entry": None, "broken_max": None,
                                    "sweep_seal_verified": False, "sealed_at_close": None,
                                    "turn_at_seal": sh.get("turnover"),
                                    "guitu": {"role": "turtle", "role_cn": "乌龟（临封扫板）", "evidence": {}},
                                    "n_frames": 1,
                                    "caveats": ["临封薄卖一滑点扫板委托，按卖一加1分实际滑点建仓并跟踪后续帧是否封死涨停。"],
                                }
                                notes.append(f"一进二临封扫板触发：{sc} {sh.get('name') or ''} @ {sw_px:.2f}（{sh.get('note')}）")
            except Exception:  # noqa: BLE001
                pass

        # 1.6) B2 纯换手挑战者毕业解锁 0.3× 独立卫星仓（严格因果：仅当某子路径通过毕业门禁时才挂单）
        b2_sat_status: dict[str, Any] = {"active": False, "graduated_paths": [], "n_today": 0}
        if (frames is None and fr and not panel_gap_blocked
                and not (gate.get("available") and gate.get("blocked"))
                and not cb.get("block_new_orders")
                and not climax_ebb.get("ebb_block")):
            try:
                from backend import evolution as _evo_b2, buy_signals as _bs_b2
                b2_eval = _evo_b2.evaluate_b2_turnover_challenger(enriched_orders_for_evo, as_of_day=day)
                grad_paths = set(b2_eval.get("graduated_paths") or [])
                b2_sat_status["graduated_paths"] = sorted(grad_paths)
                if grad_paths:
                    active_today_all = [
                        o0 for oid0, o0 in state["orders"].items()
                        if oid0.startswith(f"{day}|")
                        and o0.get("status") != "cancelled"
                        and not is_invalid_record(o0)
                    ]
                    b2_cap_total = min(int(eff_max_total), int(MAX_DAILY_ORDERS))
                    b2_rem_slots = max(0, min(2, b2_cap_total - len(active_today_all)))
                    if climax_ebb.get("climax_active"):
                        b2_sat_status["blocked_reason"] = "情绪高潮期禁止开启 B2 卫星仓"
                    elif cb.get("mode") != "normal":
                        b2_sat_status["blocked_reason"] = f"账户处于防守模式({cb.get('mode')})，禁止开启 B2 卫星仓"
                    elif b2_rem_slots <= 0:
                        b2_sat_status["blocked_reason"] = f"当日总单数已达上限({b2_cap_total})，B2 卫星仓不再加单"
                    else:
                        b2_sat_status["active"] = True
                        two_b2, _ = collect_two_board(fr)
                        allzt_b2 = collect_all_zt(fr)
                        b2_tags = _theme_tags(day, codes=list(allzt_b2.keys()))
                        from backend import theme_match as _tm_b2
                        b2_axes_map: dict[str, list[str]] = {}
                        for _bc, _bt in b2_tags.items():
                            b2_axes_map[_bc] = [_tm_b2._index().key_name.get(k, k) for k in _tm_b2.map_to_axes(_bt)]
                        ow2_codes = {c for c, g in allzt_b2.items() if is_rabbit(g)}
                        ow1_codes = {c for c, g in allzt_b2.items() if int(g.get("streak") or 0) == 1 and is_oneword(g)}
                        hi3_codes = {c for c, g in allzt_b2.items() if int(g.get("streak") or 0) >= 3}
                        ice_x_b2 = bool((ice_m.get("down_share") or 0.0) >= 0.75)
                        q_b2 = archive.latest_frame(day, "quotes")
                        q_b2_map: dict[str, dict[str, Any]] = {}
                        if q_b2 is not None and q_b2.height > 0:
                            for _qr in q_b2.iter_rows(named=True):
                                q_b2_map[str(_qr.get("ts_code") or "").zfill(6)] = dict(_qr)
                        sat_added = 0
                        for _bc, _bg in two_b2.items():
                            if sat_added >= b2_rem_slots:
                                break
                            if _bc in groups or is_rabbit_at_open(_bg) or is_rabbit(_bg):
                                continue
                            oid_b2 = f"{day}|{_bc}|B2_SAT"
                            if oid_b2 in state["orders"] or f"{day}|{_bc}|B1" in state["orders"]:
                                continue
                            my_ax = b2_axes_map.get(_bc) or []
                            my_set = set(my_ax)
                            if any(rc != _bc and my_set & set(b2_axes_map.get(rc) or []) for rc in ow2_codes):
                                continue
                            rel_set: set[str] = set()
                            for _th in my_ax:
                                rel_set.update(_tm_b2.related_themes(_th))
                            if not my_ax:
                                alt_anc = "无题材·市场自选"
                            elif any(rc != _bc and rel_set & set(b2_axes_map.get(rc) or []) for rc in ow2_codes):
                                alt_anc = "关联产业链有兔"
                            elif any(rc != _bc and my_set & set(b2_axes_map.get(rc) or []) for rc in hi3_codes):
                                alt_anc = "高标同题材带动"
                            elif any(rc != _bc and my_set & set(b2_axes_map.get(rc) or []) for rc in ow1_codes):
                                alt_anc = "一字首板带动"
                            else:
                                alt_anc = "独立换手龙头"
                            tv_f = _bg.get("turnover")
                            try:
                                tv_num = float(tv_f) if tv_f is not None else None
                            except (TypeError, ValueError):
                                tv_num = None
                            tb = "低换手(≤10%)" if (tv_num is not None and tv_num <= 10.0) else (
                                "中换手(10-18%)" if (tv_num is not None and tv_num <= 18.0) else "高换手(>18%)"
                            )
                            qr_b2 = q_b2_map.get(_bc) or {}
                            pc_b2, op_b2 = qr_b2.get("prev_close"), qr_b2.get("open")
                            og_b2 = ((float(op_b2) - float(pc_b2)) / float(pc_b2) * 100.0) if (pc_b2 and op_b2 and float(pc_b2) > 0) else None
                            gb = "弱转强高开" if (og_b2 is not None and og_b2 >= 3.0) else "平低开"
                            b2_pk = f"{alt_anc} | {tb} | {gb}" + (" | 冰点穿越" if ice_x_b2 else "")
                            if b2_pk not in grad_paths:
                                continue
                            bq_b2 = _bs_b2.evaluate_turtle_quality(_bg, rabbit_count=0)
                            if bq_b2.get("reject"):
                                continue
                            cur_held = sum(
                                float(p.get("notional") or 0.0) for p in state["positions"].values()
                                if p.get("status") != "closed" and not is_invalid_record(p)
                            )
                            if cur_held + POSITION_SIZE_YUAN * 0.30 > current_capital * MAX_GROSS_PCT:
                                break
                            sat_budget = round(POSITION_SIZE_YUAN * 0.30 * max(0.20, min(1.0, size_factor)), 2)
                            _bg_sat = dict(_bg)
                            _bg_sat["guitu_theme"] = my_ax[0] if my_ax else "纯换手·B2卫星"
                            _bg_sat["guitu_rabbits"] = []
                            _bg_sat["guitu_rabbit_codes"] = []
                            _bg_sat["b2_anchor_type"] = alt_anc
                            sat_ord = build_order(day, _bc, _bg_sat, size_factor=size_factor, size_yuan=sat_budget)
                            sat_ord["id"] = oid_b2
                            sat_ord["strategy"] = "B2_SAT"
                            sat_ord["exec_mode"] = "b2_satellite"
                            sat_ord["b2_path"] = b2_pk
                            sat_ord["b2_anchor_type"] = alt_anc
                            state["orders"][oid_b2] = sat_ord
                            sat_added += 1
                        b2_sat_status["n_today"] = sum(
                            1 for oid0, o0 in state["orders"].items()
                            if oid0.startswith(f"{day}|") and o0.get("strategy") == "B2_SAT"
                        )
                        if sat_added:
                            notes.append(f"B2 毕业卫星仓解锁（0.3×）：新增 {sat_added} 只纯换手二板排板单")
            except Exception:  # noqa: BLE001
                pass

        # 2026-11-16：成交判定改为逐帧推进（消除未来函数），同步接入动态撤单反事实反馈与蚂蚁因子
        eff_ice_level = ice.get("level") if ice.get("is_ice") else None
        cancel_fb: dict[str, float] | None = None
        if frames is None:
            try:
                from backend import evolution as _evo_cfb
                cancel_fb = (_evo_cfb.evaluate_cancel_counterfactual(
                    enriched_orders_for_evo, as_of_day=day
                ).get("threshold_adj_by_kind") or None)
            except Exception:  # noqa: BLE001
                cancel_fb = None
        n_filled_now = advance_fills(
            state, fr, day,
            cancel_feedback=cancel_fb,
            market_vol_ratio=rt_vol_ratio,
            ice_level=eff_ice_level,
            evo_high_turn_delta=evo_high_turn_delta,
            evo_rotten_delta=evo_rotten_delta,
        )
        _update_sweep_orders(state, fr, day)
        if frames is None:
            try:
                from backend import market_clock as _mc_cw
                if _mc_cw.is_trading("CN"):
                    poll_queued_cancel_watch(
                        state, day,
                        market_vol_ratio=rt_vol_ratio,
                        ice_level=eff_ice_level,
                        cancel_feedback=cancel_fb,
                        evo_high_turn_delta=evo_high_turn_delta,
                        evo_rotten_delta=evo_rotten_delta,
                    )
            except Exception:  # noqa: BLE001
                pass
        if n_filled_now:
            notes.append(f"逐帧推进成交 {n_filled_now} 笔"
                         "（只用挂单帧之后的开板信息，无未来函数）")

        # 2b) 成交 → 建仓：必须在 advance_fills **之后**，
        #     否则首遍 sync 时状态还是 queued，永远建不了仓。
        for oid, o in state["orders"].items():
            if not oid.startswith(f"{day}|"):
                continue
            if o.get("invalid_historical_bug"):
                continue
            if o.get("status") != "filled" or oid in state["positions"]:
                continue
            # 建仓即固化 qty + notional + 题材/兔子/开板/成交分类，供 T+1 卖点全链路联动；
            entry_px = o.get("entry_price")
            qty = o.get("qty")
            notional = o.get("notional")
            if (qty is None or notional is None) and entry_px is not None:
                _q, _n = position_size(entry_px)
                qty = qty if qty is not None else _q
                notional = notional if notional is not None else _n
            try:
                from backend import fill_verify as _fv_cat
                f_cat = _fv_cat.classify_fill_category(o).get("category")
            except Exception:  # noqa: BLE001
                f_cat = None
            state["positions"][oid] = {
                "id": oid, "strategy": o.get("strategy") or STRATEGY, "ts_code": o.get("ts_code"),
                "name": o.get("name"), "industry": o.get("industry"),
                "guitu": o.get("guitu"),
                "guitu_theme": o.get("guitu_theme"),
                "guitu_rabbits": o.get("guitu_rabbits"),
                "guitu_rabbit_codes": o.get("guitu_rabbit_codes"),
                "rabbit_snapshot_at_entry": o.get("rabbit_snapshot_at_entry"),
                "broken_max": o.get("broken_max"),
                "fill_category": f_cat,
                "exec_mode": o.get("exec_mode"),
                "sweep_failed_seal": o.get("sweep_failed_seal"),
                "entry_date": day, "entry_price": entry_px,
                "qty": qty,
                "notional": notional,
                "notional_basis": o.get("notional_basis")
                or "建仓价×股数（单笔上限 10 万元口径）",
                "entry_frame_ts": o.get("entry_frame_ts"),
                "fill_frame_ts": o.get("fill_frame_ts"),
                "fill_confidence": o.get("fill_confidence"),
                "decline_regime": o.get("decline_regime"),
                "status": "holding",
                "state_note": "按逐帧推进判定成交（开板/炸板时点），等待日线落盘判定断板",
            }


        if day not in state["days"]:
            state["days"].append(day)
            state["days"].sort()

        # 3) 结算未平仓持仓（完整透传 T-1 兔子今日强弱 / 核按钮 / 同门乌龟联动信号）
        open_pos = [p for p in state["positions"].values()
                    if p.get("status") != "closed" and p.get("status") != "no_fill"
                    and not is_invalid_record(p)]
        dl = daily if daily is not None else load_daily(
            sorted({str(p["ts_code"]) for p in open_pos}))
        trade_ids = {t.get("id") for t in state["trades"]}
        rabbit_states_today = _summarize_rabbit_states(state, day, fr) if fr else {}
        linkage_map = _evaluate_holding_linkage(open_pos, state, day, fr) if frames is None else {}
        for p in open_pos:
            # 兼容旧账本：持仓若缺龟兔角色/题材/兔子/开板数，从同 id 委托单回填（不重新推断）
            ord_ref = state["orders"].get(p["id"]) or {}
            for _fk in ("guitu", "guitu_theme", "guitu_rabbits", "guitu_rabbit_codes",
                        "rabbit_snapshot_at_entry", "broken_max", "exec_mode", "sweep_failed_seal"):
                if p.get(_fk) is None and ord_ref.get(_fk) is not None:
                    p[_fk] = ord_ref.get(_fk)
            rows = dl.get(str(p["ts_code"])) or []
            p_theme = str(p.get("guitu_theme") or ord_ref.get("guitu_theme") or "")
            link = linkage_map.get(str(p.get("id") or "")) or {}
            p_rabbit_weak = bool(
                link.get("rabbit_weak")
                or (p_theme and (rabbit_states_today.get(p_theme) or {}).get("state") == "failed")
            )
            # 仅在真实多维冰点（is_ice=True）时才向卖点传 ice_level，避免单维假 L5 误收紧卖点 25%
            updated = settle(
                p, rows,
                ice_level=eff_ice_level,
                rabbit_weak=p_rabbit_weak,
                rabbit_strong=link.get("rabbit_strong"),
                rabbit_nuclear=bool(link.get("rabbit_nuclear")),
                peer_turtle_strong=bool(link.get("peer_turtle_strong")),
            )
            state["positions"][p["id"]] = updated
            if updated["status"] == "closed" and p["id"] not in trade_ids:
                state["trades"].append(updated)
                trade_ids.add(p["id"])

        # 3.5) 持久化当日因限额顺延的优质候选，并在次日自动扫描二进三弱转强 / 分歧回封接力机会
        wl_store = state.setdefault("skipped_watchlist", {})
        if frames is None and skipped_candidates:
            good_skipped = [
                {
                    "ts_code": str(s.get("ts_code") or "").zfill(6),
                    "name": s.get("name") or "",
                    "theme": s.get("theme") or "",
                    "reason": s.get("reason") or "",
                }
                for s in skipped_candidates
                if "硬过滤回避" not in str(s.get("reason") or "")
            ]
            if good_skipped:
                wl_store[day] = good_skipped[:15]
                for old_k in sorted(wl_store.keys())[:-5]:
                    wl_store.pop(old_k, None)

        t1_relay_radar: dict[str, Any] = {
            "source_day": None,
            "watchlist": [],
            "weak_to_strong_auction": [],
            "reseal_confirm": [],
        }
        prev_wl_days = sorted(k for k in wl_store.keys() if k < day)
        if prev_wl_days and frames is None:
            src_day = prev_wl_days[-1]
            prev_list = wl_store.get(src_day) or []
            t1_relay_radar["source_day"] = src_day
            t1_relay_radar["watchlist"] = prev_list
            try:
                from backend import buy_signals as _bs_t1, archive as _arch_t1, fill_verify as _fv_t1
                latest_q = _arch_t1.latest_frame(day, "quotes")
                q_map: dict[str, dict[str, Any]] = {}
                if latest_q is not None and latest_q.height > 0:
                    for _qr in latest_q.to_dicts():
                        q_map[str(_qr.get("ts_code") or "").zfill(6)] = _qr
                # P0-2 修复：使用 _pool_rows(fr[-1]) 读取真实涨停池（非不存在的 "records" 键），并读取 zb 炸板池
                last_pool_rows, _ = _pool_rows(fr[-1]) if fr else ([], "")
                pool_map = {str(r.get("ts_code") or "").zfill(6): r for r in last_pool_rows if r.get("ts_code")}
                zb_block = (fr[-1].get("zb") or {}) if fr else {}
                zb_rows = zb_block.get("all") or zb_block.get("top") or []
                zb_map = {str(r.get("ts_code") or "").zfill(6): r for r in zb_rows if isinstance(r, dict) and r.get("ts_code")}
                # 读取当日竞价末帧成交额，与昨日成交额比对计算真实 auction_vol_ratio（严防无量干瘪高开诱多）
                auc_latest = _arch_t1.latest_frame(day, "auction")
                auc_amt_map: dict[str, float] = {}
                if auc_latest is not None and auc_latest.height > 0:
                    for _ar in auc_latest.to_dicts():
                        _ac = str(_ar.get("ts_code") or "").zfill(6)
                        if _ac and _ar.get("amount") is not None:
                            try:
                                auc_amt_map[_ac] = float(_ar["amount"])
                            except (TypeError, ValueError):
                                pass
                scan_in: list[dict[str, Any]] = []
                for item in prev_list:
                    c6 = str(item.get("ts_code") or "").zfill(6)
                    qr = q_map.get(c6) or {}
                    pr = pool_map.get(c6) or {}
                    zbr = zb_map.get(c6) or {}
                    pc = qr.get("prev_close")
                    op = qr.get("open")
                    og = ((float(op) - float(pc)) / float(pc) * 100.0) if (pc and op and float(pc) > 0) else None
                    seal_yi = float(pr.get("seal_fund") or 0.0) / 1e8 if pr.get("seal_fund") else 0.0
                    prev_day_amt = _fv_t1.daily_amount(src_day, c6)
                    auc_amt = auc_amt_map.get(c6)
                    auc_vol_ratio = (
                        round(auc_amt / float(prev_day_amt), 4)
                        if (auc_amt is not None and prev_day_amt and float(prev_day_amt) > 0)
                        else None
                    )
                    br_cnt = int(pr.get("broken") or zbr.get("broken") or (1 if zbr else 0))
                    scan_in.append({
                        "ts_code": c6,
                        "name": item.get("name") or pr.get("name") or zbr.get("name") or "",
                        "prev_sealed": True,
                        "open_gap": og,
                        "auction_vol_ratio": auc_vol_ratio,
                        "sealed_now": bool(pr),
                        "broken": br_cnt,
                        "seal_fund_yi": seal_yi,
                    })
                scanned = _bs_t1.scan_reseal_and_weak_to_strong(scan_in)
                t1_relay_radar["weak_to_strong_auction"] = scanned.get("weak_to_strong_auction") or []
                t1_relay_radar["reseal_confirm"] = scanned.get("reseal_confirm") or []
            except Exception:  # noqa: BLE001
                pass

        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        # 2026-11-17（Fix A 配套）：历史账本补 qty/notional 字段（只补字段，
        # 不重算已结算的 r_net），保证 /api/live-sim 一开始就有成本口径字段。
        n_backfill = _backfill_size_fields(state)
        if n_backfill:
            notes.append(f"历史账本回填 qty/notional {n_backfill} 条（仅补字段，不改已结算 r_net）")
        out = _payload(state, day, fr, notes, ice=ice, gate=gate,
                       overseas=overseas_snap, skipped_candidates=skipped_candidates,
                       sizing=sizing_info, t1_relay_radar=t1_relay_radar,
                       b2_satellite_status=b2_sat_status)
        _atomic_write(STATE_FILE, state)
        _atomic_write(SNAPSHOT, out)
        return out


def _ice_payload_from(ice: dict[str, Any] | None, day: str) -> dict[str, Any]:
    """冰点快照：优先复用 sync 已算好的结果，缺失才现算（省一次全市场扫描）。"""
    if ice is not None:
        r = ice
    else:
        try:
            from backend import ice_gate
            r = ice_gate.assess(day, intraday=True)
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "available": bool(r.get("available")),
        "intraday": bool(r.get("intraday")),
        "as_of": r.get("as_of"),
        "level": r.get("level"),
        "is_ice": bool(r.get("is_ice")),
        "ice_type": r.get("ice_type"),
        "size_factor": r.get("size_factor"),
        "decline_regime": r.get("decline_regime"),
        "verdict": r.get("verdict"),
        "reasons": r.get("reasons") or [],
        "metrics": r.get("metrics"),
        "dim_levels": r.get("dim_levels"),
        "sample": r.get("sample"),
        "caveat": r.get("caveat"),
        "reason": r.get("reason"),
    }


def _emotion_payload_from(gate: dict[str, Any] | None, day: str) -> dict[str, Any]:
    """情绪快照：优先复用 sync 已算好的结果，缺失现算（emotion_gate 约 4s/次）。"""
    if gate is not None:
        g = gate
    else:
        try:
            g = emotion_gate(day, intraday=True)
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "available": bool(g.get("available")),
        "intraday": bool(g.get("intraday")),
        "as_of": g.get("as_of"),
        "blocked": bool(g.get("blocked")),
        "verdict": g.get("verdict"),
        "n_high": g.get("n_high"),
        "avg_ret": g.get("avg_ret"),
        "limit_down_share": g.get("limit_down_share"),
        "premium": g.get("premium"),
        "n_premium": g.get("n_premium"),
        "high_board_pulse": g.get("high_board_pulse"),
        "reasons": g.get("reasons") or [],
        "reason": g.get("reason"),
    }


def _latest_price_map(day: str,
                      codes: set[str] | None = None) -> tuple[dict[str, float], bool]:
    """一次读取当日 quotes 最新帧并按目标代码裁剪，供委托估值与账户市值复用。"""
    px_map: dict[str, float] = {}
    try:
        df = archive.latest_frame(str(day), "quotes")
        if df is None or df.height == 0:
            return {}, True
        cols = [c for c in ("ts_code", "price") if c in df.columns]
        if len(cols) < 2:
            return {}, True
        sub = df.select(cols)
        if codes is not None:
            if not codes:
                return {}, False
            sub = sub.filter(pl.col("ts_code").cast(pl.Utf8).str.zfill(6).is_in(list(codes)))
        for r in sub.iter_rows(named=True):
            c = str(r.get("ts_code") or "").zfill(6)
            p = r.get("price")
            if c and p and float(p) > 0:
                px_map[c] = float(p)
        return px_map, False
    except Exception:  # noqa: BLE001
        return {}, True


def _orders_with_live_value(orders: list[dict[str, Any]], day: str,
                            *, px_map: dict[str, float] | None = None
                            ) -> list[dict[str, Any]]:
    """给今日委托补「持有价值」：实时价 × 数量 + 浮盈（元）。"""
    if px_map is None:
        need = {str(o.get("ts_code") or "").zfill(6) for o in orders if o.get("status") == "filled"}
        px_map, _ = _latest_price_map(day, codes=need)

    out = []
    for o in orders:
        row = dict(o)
        # 2026-11-25：封板质量观察字段——旧单（改造前建仓）没有这两个
        # 字段时，用已有的 turnover / first_seal 现算一份补上（只读展示）。
        if "seal_quality" not in row:
            try:
                row.update(_seal_quality_fields(
                    {"turnover": row.get("turnover"),
                     "first_seal": row.get("first_seal_hhmmss")
                                   or row.get("first_seal")}, row))
            except Exception:  # noqa: BLE001
                pass
        code = str(o.get("ts_code") or "").zfill(6)
        qty = int(o.get("qty") or 0)
        entry = float(o.get("entry_price") or 0.0)
        filled = o.get("status") == "filled"
        px = px_map.get(code) if filled else None
        if px is None or px <= 0:
            px = entry
        value = px * qty
        pnl = (px - entry) * qty if filled else 0.0
        row["live_price"] = round(px, 3)
        row["hold_value"] = round(value, 2)
        row["hold_pnl"] = round(pnl, 2)
        row["hold_pnl_pct"] = round((px / entry - 1.0) * 100.0, 2) if entry else None
        row["value_basis"] = ("实时价" if (filled and px_map.get(code)) else
                              "委托价（未成交）" if not filled else "成本价")
        out.append(row)
    return out


def _account_snapshot(state: dict[str, Any], positions: list[dict[str, Any]],
                      day: str, *,
                      px_map: dict[str, float] | None = None,
                      px_stale: bool | None = None) -> dict[str, Any]:
    """交易软件式账户面板（总资产 = 可用 + 实时持仓市值，盈亏自然滚入）。"""
    holding = [p for p in positions if p.get("status") != "closed" and not is_invalid_record(p)]
    if px_map is None:
        need = {str(p.get("ts_code") or "").zfill(6) for p in holding if p.get("ts_code")}
        px_map, px_stale = _latest_price_map(day, codes=need)
    else:
        px_stale = bool(px_stale) if px_stale is not None else False
    cost_amount = 0.0
    mv_amount = 0.0          # 持仓市值（实时价）
    float_pnl = 0.0
    today_float_pnl = 0.0
    n_px_cost = 0            # 退回成本价的持仓数
    for p in holding:
        qty = p.get("qty")
        cost = float(p.get("entry_price") or 0.0)
        c = str(p.get("ts_code") or "").zfill(6)
        px = px_map.get(c)
        if px is None or px <= 0:
            px = cost
            n_px_cost += 1
        n = int(qty) if qty else 0
        if n <= 0 and cost > 0:
            # 历史持仓缺 qty：按 10 万预算反推股数
            n = max(1, int(100_000.0 / (cost * LOT_SIZE)))
        cost_amount += cost * n
        mv_amount += px * n
        fl = (px - cost) * n
        float_pnl += fl
        if str(p.get("entry_date")) == str(day):
            today_float_pnl += fl

    # 已实现盈亏（全部已结算 trades + 在途持仓已落袋的半仓分批止盈，1 笔委托仅 1 条顶层成交不双倍计数）
    realized = 0.0
    today_realized = 0.0
    n_closed = 0
    day_clean = str(day).replace("-", "")
    for p in holding:
        if p.get("partial_closed") and isinstance(p.get("partial_realized_pnl"), (int, float)):
            ppnl = float(p["partial_realized_pnl"])
            realized += ppnl
            if str(p.get("partial_exit_date") or "").replace("-", "") == day_clean:
                today_realized += ppnl
    for t in clean_trades(state.get("trades") or [], state.get("orders")):
        nr = t.get("net_return")
        if isinstance(nr, (int, float)):
            amt = float(t.get("notional") or 0.0) or 100_000.0
            pnl = float(nr) * amt
            realized += pnl
            n_closed += 1
            exit_d = str(t.get("exit_date") or "").replace("-", "")
            if exit_d == day_clean:
                today_realized += pnl

    capital = CAPITAL_YUAN
    available = capital - cost_amount + realized     # 卖出回笼含盈亏
    total_amount = available + mv_amount              # 总资产 = 可用 + 持仓市值

    return {
        "capital_yuan": round(capital, 2),
        "total_amount": round(total_amount, 2),
        "available": round(max(0.0, available), 2),
        "position_amount": round(mv_amount, 2),
        "position_cost": round(cost_amount, 2),
        "position_pct": (round(mv_amount / total_amount * 100.0, 1)
                         if total_amount > 0 else None),
        "n_holding": len(holding),
        "float_pnl": round(float_pnl, 2),
        "today_pnl": round(today_realized + today_float_pnl, 2),
        "today_realized": round(today_realized, 2),
        "today_float": round(today_float_pnl, 2),
        "total_pnl": round(realized + float_pnl, 2),
        "total_return": (round((realized + float_pnl) / capital, 6)
                         if capital > 0 else None),
        "realized_pnl": round(realized, 2),
        "total_pnl_n": n_closed,
        "px_source": ("quotes 最新帧" if not px_stale else "无留档"),
        "n_px_cost_fallback": n_px_cost,
        "note": ("总金额 = 可用 + 持仓市值（盈亏滚入）；持仓按盘中实时价估值"
                 f"（quotes 留档，{n_px_cost} 只无实时价退回成本价）；"
                 "当日盈亏 = 当日平仓实现盈亏 + 今日买入持仓的浮动盈亏。"),
    }


def _summarize_fill_categories(state: dict[str, Any]) -> dict[str, Any]:
    """四分类成交独立记账统计（队首成交 / 开板后回封 / 开板后没回封 / 扫板影子）。"""
    from backend import fill_verify as _fv
    orders = state.get("orders") or {}
    trades = clean_trades(state.get("trades") or [], orders)
    buckets: dict[str, list[float]] = {
        "queue_head": [], "reopen_reseal": [], "reopen_broken": [], "sweep_shadow": [],
    }
    counts_all: dict[str, int] = {k: 0 for k in buckets}
    for o in orders.values():
        if o.get("invalid_historical_bug"):
            continue
        cat = _fv.classify_fill_category(o)["category"]
        if cat in counts_all:
            counts_all[cat] += 1
    for _sw in (state.get("sweep_shadow_orders") or {}).values():
        counts_all["sweep_shadow"] += 1
    for t in trades:
        o = orders.get(t.get("id")) or orders.get(t.get("parent_id")) or {}
        cat = _fv.classify_fill_category(o)["category"]
        ret = (t.get("r_net") if t.get("r_net") is not None
               else (t.get("net_return") if t.get("net_return") is not None
                     else (t.get("gross_return") if t.get("gross_return") is not None
                           else t.get("r_gross"))))
        if cat in buckets and ret is not None:
            buckets[cat].append(float(ret))
    out: dict[str, Any] = {}
    for k in ("queue_head", "reopen_reseal", "reopen_broken", "sweep_shadow"):
        vs = buckets[k]
        out[k] = {
            "label": _fv.FILL_CATEGORY_CN[k],
            "n_orders": counts_all[k],
            "n_closed": len(vs),
            "win_rate": round(sum(1 for x in vs if x > 0) / len(vs), 4) if vs else None,
            "mean_ret": round(sum(vs) / len(vs), 4) if vs else None,
        }
    return out


def _summarize_rabbit_states(
    state: dict[str, Any],
    day: str,
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    """按题材汇总当日锚定兔子的四态强度（anchored / decaying / diverging / failed）。"""
    if not frames:
        return {}
    from backend import rabbit_state as _rs
    theme_rabbits: dict[str, set[str]] = {}
    for oid, o in (state.get("orders") or {}).items():
        if not oid.startswith(f"{day}|") or o.get("invalid_historical_bug"):
            continue
        th = str(o.get("guitu_theme") or o.get("industry") or "")
        rcodes = o.get("guitu_rabbit_codes") or []
        if th and isinstance(rcodes, list) and rcodes:
            theme_rabbits.setdefault(th, set()).update(str(x).zfill(6) for x in rcodes if str(x).strip())
    out: dict[str, Any] = {}
    for th, rset in sorted(theme_rabbits.items()):
        try:
            out[th] = _rs.evaluate_rabbit_state(sorted(rset), frames)
        except Exception:  # noqa: BLE001
            pass
    return out


def _payload(state: dict[str, Any], day: str, frames: list[dict[str, Any]],
             notes: list[str], *,
             ice: dict[str, Any] | None = None,
             gate: dict[str, Any] | None = None,
             overseas: dict[str, Any] | None = None,
             skipped_candidates: list[dict[str, Any]] | None = None,
             sizing: dict[str, Any] | None = None,
             t1_relay_radar: dict[str, Any] | None = None,
             b2_satellite_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """组装账本快照。"""
    orders = [o for o in state["orders"].values() if not o.get("invalid_historical_bug")]
    today = [o for o in orders if o.get("day") == day]
    no_orders_reason: str | None = None
    if not today:
        no_orders_reason = (
            f"{day} 无委托单（今天不开仓）。可能原因：策略按规则未开仓"
            "（题材内无一字板+换手二板组合 / 情绪闸门拦截 / 当日非交易日 / 快照未落盘）。"
            "上方列表为空即表示当日没有可执行的委托，不回退展示历史交易日的单。")
    positions = [p for p in state["positions"].values() if not is_invalid_record(p)]
    trades = clean_trades(state["trades"], state.get("orders"))
    closed = [t for t in trades if isinstance(t.get("gross_return"), (int, float))]

    def _agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
        vals = [float(r["gross_return"]) for r in rows
                if isinstance(r.get("gross_return"), (int, float))]
        if not vals:
            return {"n": 0, "win_rate": None, "gross_mean": None, "gross_median": None}
        a = np.asarray(vals, dtype=float)
        return {"n": int(a.size), "win_rate": round(float((a > 0).mean()), 4),
                "gross_mean": round(float(a.mean()), 6),
                "gross_median": round(float(np.median(a)), 6)}

    # 单一口径（万0.84免五）：新记录用 net_return；历史记录用 r_net 兜底。
    by_cost = {COST_LABEL: _agg([{**t, "gross_return": (
        t.get("net_return") if t.get("net_return") is not None else t.get("r_net"))}
        for t in closed])}
    # 2026-11-16：读取时按需补「排板核验」（只读展示，不改委托状态）。
    try:
        from backend import fill_verify as _fv
        for _o in today:
            stale = False
            qs_old = _o.get("queue_sim")
            if qs_old is None:
                stale = True
            else:
                # 2026-11-24 盘中修复：账本里固化可能是"当日成交额还没进面板"
                # 时刻的缺数据结果（detail 以「封单/成交额/换手缺一」开头）。
                # 盘中 quotes 每 5 分钟落一帧，数据后来有了 → 必须重算，
                # 否则消耗比整天都是空的。
                if str((qs_old or {}).get("detail") or "").startswith("封单/成交额/换手缺一"):
                    stale = True
            if stale:
                _o["queue_sim"] = _fv.simulate_fill(
                    day, str(_o.get("ts_code") or ""),
                    str(_o.get("entry_frame_ts") or ""), frames,
                    entry_price=_o.get("entry_price"))
            _sim = _o.get("queue_sim") or {}
            if not _o.get("fill_confidence"):
                _o["fill_confidence"] = (
                    _sim.get("confidence")
                    if _o.get("status") == _sim.get("status")
                    else "unverified")
    except Exception:  # noqa: BLE001
        pass

    # 一次读取所需个股的最新 quotes 快照，供今日委托估值与账户市值共用（避免两次扫 5580 行）
    need_codes = (
        {str(o.get("ts_code") or "").zfill(6) for o in today if o.get("status") == "filled"}
        | {str(p.get("ts_code") or "").zfill(6) for p in positions if p.get("status") != "closed"}
    )
    need_codes.discard("000000")
    px_map, px_stale = _latest_price_map(day, codes=need_codes)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "day": day,
        # 2026-11-20 用户裁定：不再回退到「最近有委托的交易日」。
        # `no_orders=true` + `no_orders_reason` 让前端直接显示"今天不开仓"，
        # 列表为空就是当日无委托，不拿历史日的单充数。
        "no_orders": bool(no_orders_reason),
        "no_orders_reason": no_orders_reason,
        # 兼容字段：始终为 None（保留键位，避免旧前端 undefined）
        "fallback_day_note": None,
        "strategy": STRATEGY,
        "entry_rule": ENTRY_RULE,
        "exit_rule": EXIT_RULE,
        "cost_label": COST_LABEL,
        "cost_rate": round(costs.DEFAULT.round_trip_rate(), 6),
        "cost_status": "用户 2026-11-23 裁定：万0.84免五，单一口径；历史记录不重算",
        "ice": _ice_payload_from(ice, day),
        "emotion": _emotion_payload_from(gate, day),
        "overseas": (overseas if (overseas and overseas.get("available")) else {"available": False}),
        "sizing": sizing or {"effective_size_factor": float((ice or {}).get("size_factor") or 1.0)},
        "skipped_candidates": skipped_candidates or [],
        "t1_relay_radar": t1_relay_radar or {"source_day": None, "watchlist": [], "weak_to_strong_auction": [], "reseal_confirm": []},
        "b2_satellite_status": b2_satellite_status or {"active": False, "graduated_paths": [], "n_today": 0},
        "by_fill_category": _summarize_fill_categories(state),
        "sweep_shadow_orders": [
            v for v in (state.get("sweep_shadow_orders") or {}).values()
            if str(v.get("day") or "") == str(day)
        ],
        "rabbit_states": _summarize_rabbit_states(state, day, frames),
        "core_definition": guitu_roles.CORE_DEFINITION,
        "guitu_note": guitu_roles.PROXY_NOTE,
        "by_role": guitu_roles.summarize_roles(orders, trades),
        "pairs": guitu_roles.pair_groups(orders),
        "today_pairs": guitu_roles.pair_groups(today),
        "n_pairs_today": guitu_roles.pair_groups(today)["n_pairs"],
        "capital_yuan": CAPITAL_YUAN,
        "position_size_yuan": POSITION_SIZE_YUAN,
        "frames": len(frames),
        "today_orders": _orders_with_live_value(today, day, px_map=px_map),
        "n_today": len(today),
        "n_filled_today": sum(1 for o in today if o.get("status") == "filled"),
        "n_unfilled_today": sum(1 for o in today if o.get("status") == "queued_unfilled"),
        "n_unknown_today": sum(1 for o in today if o.get("status") == "unknown"),
        "positions": positions,
        "n_holding": sum(1 for p in positions if p.get("status") != "closed"),
        "account": _account_snapshot(state, positions, day, px_map=px_map, px_stale=px_stale),
        "trades": trades,
        "summary": {
            "n_orders_all": len(orders),
            "n_filled_all": sum(1 for o in orders if o.get("status") == "filled"),
            "n_trades_closed": len(closed),
            "gross": _agg(closed),
            "by_cost": by_cost,
            "by_strategy": {
                STRATEGY: _agg([t for t in closed if (t.get("strategy") or STRATEGY) == STRATEGY]),
                "B2_SAT": _agg([t for t in closed if t.get("strategy") == "B2_SAT"]),
            },
        },
        "days": state["days"],
        "notes": notes,
        "caveats": list(CAVEATS),
        "money_guarantee": queue_match.EXTERNAL_RESEARCH["money_guarantee"],
    }


def _attach_fill_audit(d: dict[str, Any]) -> None:
    """给快照里的今日委托补「排板核验」（只读展示，不写回快照）。

    2026-11-16：快照是修复前落盘的，没有 queue_sim；页面需要展示
    队列消耗比与成交额来源，这里现算一份。异常一律吞掉 —— 展示层
    不能因为核验失败而拖垮整个 payload。
    """
    try:
        day = str(d.get("day") or "")
        orders = d.get("today_orders") or []
        if not day or not orders:
            return
        frames = read_frames(day)
        if not frames:
            return
        from backend import fill_verify as _fv
        for o in orders:
            if o.get("queue_sim") is None:
                o["queue_sim"] = _fv.simulate_fill(
                    day, str(o.get("ts_code") or ""),
                    str(o.get("entry_frame_ts") or ""), frames,
                    entry_price=o.get("entry_price"))
            sim = o.get("queue_sim") or {}
            if not o.get("fill_confidence"):
                o["fill_confidence"] = (
                    sim.get("confidence")
                    if o.get("status") == sim.get("status")
                    else "unverified")
    except Exception:  # noqa: BLE001
        pass


def payload() -> dict[str, Any]:
    """给 API 用：优先返回已落盘快照，无则同步一次。不触发外部请求。"""
    if SNAPSHOT.is_file():
        try:
            d = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
            d["source"] = "snapshot"
            _attach_fill_audit(d)
            return d
        except (json.JSONDecodeError, OSError):
            pass
    d = sync()
    d["source"] = "synced"
    return d


if __name__ == "__main__":
    r = sync()
    print(json.dumps({
        "day": r["day"], "frames": r["frames"],
        "n_today": r["n_today"], "n_filled_today": r["n_filled_today"],
        "n_unfilled_today": r["n_unfilled_today"], "n_unknown_today": r["n_unknown_today"],
        "n_holding": r["n_holding"], "summary": r["summary"]["gross"],
        "notes": r["notes"],
    }, ensure_ascii=False, indent=2))
