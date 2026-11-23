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

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from backend import costs, feature_engine, guitu_roles, queue_match

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LIVE_DIR = DATA / "live"
PANEL = DATA / "panel_raw.parquet"
PAPER = DATA / "paper"
STATE_FILE = PAPER / "sim_state.json"
SNAPSHOT = PAPER / "live_sim.json"

_lock = threading.Lock()

STRATEGY = "B1 悟空真经·二板排板"
ENTRY_RULE = "二板封板瞬间挂涨停价限价委托排队（不默认成交）"
EXIT_RULE = "首个未涨停日（断板）当日收盘卖出；断板后不持有"

# 成本单一口径（用户 2026-11-23 裁定）：万 0.84 免五，见 costs.DEFAULT。
# 实际成本率由 costs.DEFAULT.round_trip_rate() 计算，不在此硬编码百分比。
COST_LABEL = "万0.84免五（佣金0.0084%/边·无最低5元·印花0.05%·过户0.001%·滑点15bp/边）"

#: 单笔模拟买入金额上限（元）。对齐决策层「单笔 ≤10% / 单笔上限 10 万」口径。
#: 用于把「价格」换算成「数量 × 金额」，账本因此有真实成交额字段。
POSITION_SIZE_YUAN = 100_000.0
#: A 股最小交易单位（1 手 = 100 股），数量必须是它的整数倍。
LOT_SIZE = 100


def position_size(price: float | None,
                  *, size_yuan: float = POSITION_SIZE_YUAN,
                  lot: int = LOT_SIZE) -> tuple[int | None, float | None]:
    """价格 → (股数, 成交额)。

    规则：`qty = floor(size_yuan / (price * lot)) * lot`，至少 1 手；
    `notional = price * qty`（取到分）。

    为什么需要它：账本要有真实「数量 × 成交额」才能逐腿核算滑点与印花税。
    万0.84免五口径下成本率与成交额无关，但成交额仍是成本明细的必要字段。
    """
    try:
        p = float(price) if price is not None else None
    except (TypeError, ValueError):
        p = None
    if p is None or not (p > 0) or not (size_yuan > 0) or lot <= 0:
        return None, None
    lots = max(1, int(size_yuan // (p * lot)))
    qty = lots * lot
    return qty, round(p * qty, 2)


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
    "「断板当日收盘卖出」以**真实日线收盘价**记账；实盘无法精确在收盘前成交，属收盘价近似，不是可复现的成交回报。",
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


def build_order(day: str, code: str, g: dict[str, Any], *,
                size_factor: float = 1.0) -> dict[str, Any]:
    """生成**挂单**（不判成交）。

    2026-11-16 修复未来函数：旧实现用 `g["broken_max"]`（全天最大开板次数）
    判定能否成交 —— 那是收盘后才知道的数据，等于挂单时就未卜先知。
    现在这里只产生「挂单」，成交由 :func:`advance_fills` 逐帧推进决定，
    每次只用当前帧及之前的信息。

    Args:
        size_factor: 仓位系数（冰点小仓试错，见 `ice_gate.size_factor`）。
                     只缩放单笔买入金额，不改变选股、成交判定与买卖点。
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
    # 2026-11-23：冰点时按 size_factor 缩放单笔金额（小仓试错，不改买卖点）。
    factor = max(0.0, min(1.0, float(size_factor or 1.0)))
    size_yuan = POSITION_SIZE_YUAN * factor
    qty, notional = position_size(first.get("price"), size_yuan=size_yuan)
    return {
        "id": f"{day}|{code}|B1",
        "day": day, "strategy": STRATEGY,
        "ts_code": code, "name": g["name"],
        "industry": g["industry"], "turnover": g["turnover"],
        "entry_date": day, "entry_price": first.get("price"),
        # 数量与成交额：让 settle 的成本口径能计入最低 5 元佣金
        "qty": qty, "notional": notional,
        "size_factor": factor,
        "notional_basis": (f"单笔上限 {size_yuan:,.0f} 元（冰点系数 {factor:.2f}）："
                           f"qty=floor({size_yuan:,.0f}/(价×100))×100"),
        "entry_frame_ts": g["frame_ts"],
        "entry_rule": ENTRY_RULE, "exit_rule": EXIT_RULE,
        "queue": q,
        "intraday": intraday_check,
        "status": status,
        "guitu": guitu_roles.classify_role(role_row),
        "n_frames": g["n_frames"],
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
                  day: str) -> int:
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
        if o.get("status") == "filled":
            continue                                  # 已成交不回退
        b0 = o.get("broken_at_entry")
        if b0 is None:
            if o.get("status") not in ("filled",):
                o["status"] = "unknown"
            continue
        i0 = ts_pos.get(str(o.get("entry_frame_ts") or ""))
        if i0 is None:
            o["status"] = "unknown"
            o["fill_reason"] = "挂单帧不在留档帧序列中，无法推进"
            continue
        code = o.get("ts_code")

        # 2026-11-16：优先用**完整排板模拟**（逐轮开板累积消耗队列）。
        # 拿不到封单/成交额/换手时，退回二元判定（开板即成交）并标 unverified。
        try:
            from backend import fill_verify as _fv
            sim = _fv.simulate_fill(day, str(code),
                                    str(o.get("entry_frame_ts") or ""),
                                    frames,
                                    entry_price=o.get("entry_price"))
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
            if sim.get("status") == "queued_unfilled":
                o.update(status="queued_unfilled",
                         fill_reason=sim.get("detail"),
                         fill_confidence="none")
                continue
        except Exception:  # noqa: BLE001
            pass

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



def settle(pos: dict[str, Any], daily: list[dict[str, Any]]) -> dict[str, Any]:
    """按真实日线更新一笔持仓（纯函数，返回新 dict，不改入参）。

    2026-11-17 变更（lead 验收 P1）
    ------------------------------
    * 成本：从持仓/委托回填 `notional`（优先持仓字段，其次 `entry_price*qty`
      兜底）；当前口径万0.84免五，成本率与成交额无关，notional 仅作明细留档。
      历史已结算记录保留当时口径，不重算。
    * 卖点：退出日若收盘跌停封死 → `find_exit` 顺延到下一个可成交交易日；
      本函数把 `exit_price_basis` / `exit_blocked` / `exit_blocked_days` 写进
      持仓与 trades，日线未覆盖到可成交日时保持 holding（pending），不伪造价格。
    """
    from backend import dates

    p = dict(pos)
    code = str(p.get("ts_code") or "") or None
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
    return p



# ---------------------------------------------------------------------------
# I/O：留档帧 / 日线 / 账本
# ---------------------------------------------------------------------------

def read_frames(day: str) -> list[dict[str, Any]]:
    f = LIVE_DIR / day / "pools" / "pools.jsonl"
    if not f.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


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


def _theme_tags(day: str) -> dict[str, list[str]]:
    """当日涨停股的题材标签（来自 theme_pool，不是申万行业）。

    2026-11-18（用户实测踩坑）：只返回原始概念标签粒度太碎——
    同一个板块情绪下，一字板锚点（盛剑科技标签=光刻胶/半导体附属装备）
    和换手二板（真视通标签=华为昇腾/液冷算力）被拆进不同标签组，
    「题材内有一字板 → 买其他二板」永远凑不齐，全天不开仓。
    修法：用 theme_pool 已有的人工题材轴（156 标签 → 9 板块）把标签
    归并到板块级再返回。一个股的轴 = 它所有标签命中的板块 key 的并集。
    """
    try:
        import polars as _pl
        from backend import theme_pool
        df = theme_pool.load(_day_dash(day))
        if df.height == 0:
            return {}
        # 标签 → 板块 key 映射（人工轴，随 theme_pool 版本更新）
        axis = theme_pool.load_axis() or {}
        amap: dict[str, dict] = axis.get("axes") or {}
        tag_to_key: dict[str, str] = {}
        for tag, info in amap.items():
            k = (info or {}).get("key") if isinstance(info, dict) else None
            if k:
                tag_to_key[str(tag)] = str(k)
        key_name: dict[str, str] = {}
        for tag, info in amap.items():
            if isinstance(info, dict) and info.get("key"):
                key_name[str(info["key"])] = str(info.get("name") or info["key"])

        out: dict[str, list[str]] = {}
        for r in df.filter(_pl.col("pool") == "zt").iter_rows(named=True):
            c = str(r.get("code") or "").zfill(6)
            if not c or c == "000000":
                continue
            raw = list(r.get("tags") or [])
            # 板块级归并：命中轴的取板块名；没命中轴的保留原始标签兜底
            merged: list[str] = []
            seen: set[str] = set()
            for t in raw:
                k = tag_to_key.get(str(t))
                if k:
                    nm = key_name.get(k, k)
                    if nm not in seen:
                        seen.add(nm)
                        merged.append(nm)
                else:
                    if str(t) not in seen:
                        seen.add(str(t))
                        merged.append(str(t))
            out[c] = merged if merged else raw
        return out
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
    tags = _theme_tags(day)
    if not tags:
        notes.append(
            "当日无题材池标签 → 无法按题材分组，按规则不产生委托"
            "（不退回「全部二板」的旧口径）。")
        return {}, notes

    # 题材 -> 该题材下的涨停股
    from collections import defaultdict
    by_theme: dict[str, list[str]] = defaultdict(list)
    for code in allzt:
        for t in tags.get(code) or []:
            by_theme[t].append(code)

    two_map, _ = collect_two_board(frames)   # 二板素材（含全帧 broken_max 等）

    picked: dict[str, dict[str, Any]] = {}
    hit_themes: list[str] = []
    for theme, codes in sorted(by_theme.items()):
        # 用户确认：兔子必须是「一字板 + 二板」，一字首板不算
        ones = [c for c in codes if is_rabbit(allzt[c])]
        if not ones:
            continue
        # 该题材内的其他二板（排除一字板本身；且只做二板）
        others = [c for c in codes
                  if c not in ones and int(allzt[c].get("streak") or 0) == 2]
        # 尾盘已不在涨停池的（早盘封一下就炸的）不买 —— 那不是"在做"
        others = [c for c in others if allzt[c].get("in_last_frame")]
        if not others:
            continue
        hit_themes.append(theme)
        for c in others:
            g = dict(two_map.get(c) or {})
            # two_map 可能没有（该票只在某些帧出现 streak==2）→ 用 allzt 兜底
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
            g["guitu_rabbits"] = [allzt[c2]["name"] for c2 in ones]
            picked[c] = g

    if hit_themes:
        notes.append(
            f"按「题材内有一字板 → 买该题材其他二板」命中 {len(hit_themes)} 个题材："
            + "、".join(hit_themes)
            + f"；共 {len(picked)} 只候选。")
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


def emotion_gate(day: str) -> dict[str, Any]:
    """悟空真经第一步：情绪闸门（人气股赚钱效应）。

    用户原话：「明显的亏钱效应，最直接的指标，人气股是不是在大跌，是不是按在跌停」。

    用三个可历史回溯的代理指标（任一明显恶化即判情绪不佳）：
      A. 昨日连板≥2 的高标，今日平均涨幅 ≤ -3%
      B. 昨日高标今日跌停占比 ≥ 40%
      C. 昨日涨停溢价 premium ≤ -1.5%

    为什么要区分「高低切换」与「全面崩盘」：2026-11-16 全市场跌停 68 家
    （旧口径判冰点 L5），但昨日高标 6 只里 4 只涨停、平均 +2.18% —— 那只是
    资金换方向，不是打板亏钱效应。旧口径会让策略在这种日子空仓。
    """
    out: dict[str, Any] = {"available": False, "reason": None, "blocked": False}
    try:
        from backend import feeds, feature_engine
        p = feeds.build_panel()
        if p is None or p.height == 0:
            out["reason"] = "面板不可用"
            return out
        days = sorted({str(d) for d in p["trade_date"].unique().to_list()})
        dd = _day_dash(day)
        if dd not in days:
            out["reason"] = f"面板无 {dd}"
            return out
        i = days.index(dd)
        if i < 1:
            out["reason"] = "无前一交易日"
            return out
        prev, today = days[i - 1], dd

        hi_ret: list[float] = []
        for (code,), g in p.group_by("ts_code", maintain_order=True):
            ds = [str(x) for x in g["trade_date"].to_list()]
            if prev not in ds or today not in ds:
                continue
            jp, jt = ds.index(prev), ds.index(today)
            if jt < 1:
                continue
            c = g["close"].to_numpy().astype(float)
            _, st = feature_engine.zt_flags(code, c)
            if int(st[jp]) < 2:
                continue
            r = float(c[jt] / c[jt - 1] - 1.0) * 100.0
            if r == r:
                hi_ret.append(r)

        if not hi_ret:
            out["reason"] = "无昨日高标样本"
            return out
        n = len(hi_ret)
        avg = sum(hi_ret) / n
        dt_share = sum(1 for r in hi_ret if r <= -9.5) / n

        # C：昨日涨停溢价 = 昨日所有涨停股，今日平均涨跌幅。
        # 2026-11-23 用户裁定：用真实日线算回来（此前字段恒为 None）。
        # 口径：昨日涨停（含首板与连板）→ 今日收盘相对昨收的平均涨幅。
        prem_vals: list[float] = []
        for (code,), g in p.group_by("ts_code", maintain_order=True):
            ds = [str(x) for x in g["trade_date"].to_list()]
            if prev not in ds or today not in ds:
                continue
            jp, jt = ds.index(prev), ds.index(today)
            if jt < 1:
                continue
            c = g["close"].to_numpy().astype(float)
            _, st = feature_engine.zt_flags(code, c)
            if int(st[jp]) < 1:                  # 昨日未涨停 → 不进溢价样本
                continue
            r = float(c[jt] / c[jt - 1] - 1.0) * 100.0
            if r == r:
                prem_vals.append(r)
        premium = round(sum(prem_vals) / len(prem_vals), 2) if prem_vals else None
        out.update({
            "available": True,
            "n_high": n,
            "avg_ret": round(avg, 2),
            "limit_down_share": round(dt_share, 3),
            "worst": round(min(hi_ret), 2),
            "premium": premium,
            "n_premium": len(prem_vals),
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
    return out


def sync(day: str | None = None, *, frames: list[dict[str, Any]] | None = None,
         daily: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """同步当日委托 + 结算所有未平仓持仓。幂等，可盘中反复调用。"""
    day = day or datetime.now().strftime("%Y%m%d")
    with _lock:
        state = _load_state()
        fr = frames if frames is not None else read_frames(day)
        notes: list[str] = []
        # 2026-11-16 用户裁定：改为「题材内有一字板 → 买该题材其他二板」。
        # 旧实现 collect_two_board 是「全部二板都买」，方向与策略原意不符。
        gate = emotion_gate(day)
        # 市场级冰点闸门（用户 2026-11-23 裁定）：冰点不空仓，按系数缩减仓位。
        # persist=True：命中即新增样本入库（用户裁定 B，与时俱进更新冰点库）。
        try:
            from backend import ice_gate
            ice = ice_gate.assess(day, persist=True)
        except Exception as exc:  # noqa: BLE001 - 冰点不可用不阻塞主流程
            ice = {"available": False, "is_ice": False, "size_factor": 1.0,
                   "level": None, "reason": f"{type(exc).__name__}: {exc}"}
        size_factor = float(ice.get("size_factor") or 1.0)
        if gate.get("available") and gate.get("blocked"):
            groups, collect_notes = {}, [
                "情绪闸门拦截：" + "；".join(gate.get("reasons") or [])
                + "（按悟空真经第一步，情绪不佳时停手/控仓）"]
        else:
            groups, collect_notes = collect_guitu_targets(fr, day)
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
                + f"）→ 小仓试错，仓位系数 {size_factor:.2f}"
                "（用户 2026-11-23 裁定：冰点不绝对空仓）")
        elif ice.get("available"):
            collect_notes.append(
                f"市场非冰点 L={ice.get('level')} → 正常仓位（系数 {size_factor:.2f}）")
        notes.extend(collect_notes)
        if not fr:
            notes.append(f"{day} 无涨停池留档帧；本日不产生任何委托单（不补数、不猜测）。")

        # 1) 委托单：新单 or 更新已有单的开板状态（单向迁移）
        for code, g in groups.items():
            oid = f"{day}|{code}|B1"
            cur = state["orders"].get(oid)
            new = build_order(day, code, g, size_factor=size_factor)
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
        # 2026-11-16：成交判定改为逐帧推进（消除未来函数）
        # 2026-11-17（Fix C）：此前这里被**连写两遍**（合并冲突痕迹），
        # 第二次调用恒返回 0，纯冗余 → 只保留一次。
        n_filled_now = advance_fills(state, fr, day)
        if n_filled_now:
            notes.append(f"逐帧推进成交 {n_filled_now} 笔"
                         "（只用挂单帧之后的开板信息，无未来函数）")

        # 2b) 成交 → 建仓：必须在 advance_fills **之后**，
        #     否则首遍 sync 时状态还是 queued，永远建不了仓。
        for oid, o in state["orders"].items():
            if not oid.startswith(f"{day}|"):
                continue
            if o.get("status") != "filled" or oid in state["positions"]:
                continue
            # 建仓即固化 qty + notional，作为成本明细留档；
            # 缺价格时按 None 如实留空（settle 再兜底/标注）。
            entry_px = o.get("entry_price")
            qty = o.get("qty")
            notional = o.get("notional")
            if (qty is None or notional is None) and entry_px is not None:
                _q, _n = position_size(entry_px)
                qty = qty if qty is not None else _q
                notional = notional if notional is not None else _n
            state["positions"][oid] = {
                "id": oid, "strategy": STRATEGY, "ts_code": o.get("ts_code"),
                "name": o.get("name"), "industry": o.get("industry"),
                "guitu": o.get("guitu"),
                "entry_date": day, "entry_price": entry_px,
                "qty": qty,
                "notional": notional,
                "notional_basis": o.get("notional_basis")
                or "建仓价×股数（单笔上限 10 万元口径）",
                "entry_frame_ts": o.get("entry_frame_ts"),
                "fill_frame_ts": o.get("fill_frame_ts"),
                "fill_confidence": o.get("fill_confidence"),
                "status": "holding",
                "state_note": "按逐帧推进判定成交（开板/炸板时点），等待日线落盘判定断板",
            }


        if day not in state["days"]:
            state["days"].append(day)
            state["days"].sort()

        # 3) 结算未平仓持仓
        open_pos = [p for p in state["positions"].values()
                    if p.get("status") != "closed" and p.get("status") != "no_fill"]
        dl = daily if daily is not None else load_daily(
            sorted({str(p["ts_code"]) for p in open_pos}))
        trade_ids = {t.get("id") for t in state["trades"]}
        for p in open_pos:
            # 兼容旧账本：持仓若缺龟兔角色，从同 id 委托单回填（不重新推断）
            if not p.get("guitu"):
                p["guitu"] = (state["orders"].get(p["id"]) or {}).get("guitu")
            rows = dl.get(str(p["ts_code"])) or []
            updated = settle(p, rows)
            state["positions"][p["id"]] = updated
            if updated["status"] == "closed" and p["id"] not in trade_ids:
                state["trades"].append(updated)
                trade_ids.add(p["id"])

        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        # 2026-11-17（Fix A 配套）：历史账本补 qty/notional 字段（只补字段，
        # 不重算已结算的 r_net），保证 /api/live-sim 一开始就有成本口径字段。
        n_backfill = _backfill_size_fields(state)
        if n_backfill:
            notes.append(f"历史账本回填 qty/notional {n_backfill} 条（仅补字段，不改已结算 r_net）")
        out = _payload(state, day, fr, notes)
        _atomic_write(STATE_FILE, state)
        _atomic_write(SNAPSHOT, out)
        return out


def _ice_payload(day: str) -> dict[str, Any]:
    """市场级冰点快照（只读展示；不改买卖点）。失败降级为空，不阻塞 payload。"""
    try:
        from backend import ice_gate
        r = ice_gate.assess(day)
        return {
            "available": bool(r.get("available")),
            "level": r.get("level"),
            "is_ice": bool(r.get("is_ice")),
            "ice_type": r.get("ice_type"),
            "size_factor": r.get("size_factor"),
            "verdict": r.get("verdict"),
            "reasons": r.get("reasons") or [],
            "metrics": r.get("metrics"),
            "dim_levels": r.get("dim_levels"),
            "sample": r.get("sample"),
            "caveat": r.get("caveat"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def _emotion_payload(day: str) -> dict[str, Any]:
    """个股级情绪闸门快照（含条件 C 溢价）。失败降级为空。"""
    try:
        g = emotion_gate(day)
        return {
            "available": bool(g.get("available")),
            "blocked": bool(g.get("blocked")),
            "verdict": g.get("verdict"),
            "n_high": g.get("n_high"),
            "avg_ret": g.get("avg_ret"),
            "limit_down_share": g.get("limit_down_share"),
            "premium": g.get("premium"),
            "n_premium": g.get("n_premium"),
            "reasons": g.get("reasons") or [],
            "reason": g.get("reason"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def _payload(state: dict[str, Any], day: str, frames: list[dict[str, Any]],
             notes: list[str]) -> dict[str, Any]:
    orders = list(state["orders"].values())
    today = [o for o in orders if o.get("day") == day]
    # 2026-11-20 用户裁定：**无委托就显示不开仓，不要展示上个有委托日的单**。
    #
    # 历史：
    #   2026-11-16 过了零点 day 变新日期、无留档帧 → 回退到「最近有委托的交易日」，否则页面空。
    #   2026-11-18 用户实测踩坑：静默回退让页面显示前天的票，误以为实时二板 →
    #             改成带 fallback_day_note 的显式回退。
    #   2026-11-20 用户裁定：**显式回退也不够——不该展示历史日的单**。
    #             页面显示 11-18 的委托，用户看到的是"不是今天的票"，容易误读；
    #             正确做法是如实告诉用户「今天没有委托（原因）」，列表就是空的。
    #
    # 现在的行为：day 保持当天，today 为空就是空。
    # `no_orders_reason` 说明为什么没开仓；`no_orders=true` 让前端显示"不开仓"态。
    no_orders_reason: str | None = None
    if not today:
        no_orders_reason = (
            f"{day} 无委托单（今天不开仓）。可能原因：策略按规则未开仓"
            "（题材内无一字板+换手二板组合 / 情绪闸门拦截 / 当日非交易日 / 快照未落盘）。"
            "上方列表为空即表示当日没有可执行的委托，不回退展示历史交易日的单。")
    positions = list(state["positions"].values())
    trades = state["trades"]
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
            if _o.get("queue_sim") is None:
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
        "ice": _ice_payload(day),
        "emotion": _emotion_payload(day),
        "core_definition": guitu_roles.CORE_DEFINITION,
        "guitu_note": guitu_roles.PROXY_NOTE,
        "by_role": guitu_roles.summarize_roles(orders, trades),
        "pairs": guitu_roles.pair_groups(orders),
        "n_pairs_today": guitu_roles.pair_groups(today)["n_pairs"],
        "frames": len(frames),
        "today_orders": today,
        "n_today": len(today),
        "n_filled_today": sum(1 for o in today if o.get("status") == "filled"),
        "n_unfilled_today": sum(1 for o in today if o.get("status") == "queued_unfilled"),
        "n_unknown_today": sum(1 for o in today if o.get("status") == "unknown"),
        "positions": positions,
        "n_holding": sum(1 for p in positions if p.get("status") != "closed"),
        "trades": trades,
        "summary": {
            "n_orders_all": len(orders),
            "n_filled_all": sum(1 for o in orders if o.get("status") == "filled"),
            "n_trades_closed": len(closed),
            "gross": _agg(closed),
            "by_cost": by_cost,
            "by_strategy": {STRATEGY: _agg(closed)},
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
