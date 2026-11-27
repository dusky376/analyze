"""涨停「排板」撮合 + 外部模拟盘核实登记。

用户 2026-11-12 裁定②
------------------------------------------------
> 买点＝二板当日涨停瞬间排版（排板）。

即：在**二板封板的那一瞬间**以涨停价挂单排队，不是「收盘价默认成交」。
封板期间能否排到，取决于封单量、是否开板、队列位置。

规则依据（2026-11-12 实地核实，均为公开页面，非猜测）
------------------------------------------------
1. 东方财富帮助中心 Q037《沪深模拟组合怎么用?》
   「涨停板的股票，以涨停价委托的单子，如果涨停打开，卖一价有委托单，即可成交；
     涨停板不打开，不成交。」
2. 东方财富《模拟炒股百科》更细的队列口径
   「涨停板的股票，以涨停价委托的单子，记录当时买一量，若其后时间的当前价低于涨停价
     或者阶段成交量差大于买一量，成交，成交价为委托价。」
3. 雪球《产品说明 · 组合》
   「A 股涨停的股票无法买入但可以卖出……组合系统会对调仓进行拆单处理，
     可以成交的仓位先行成交，未成交的部分参与下一次系统调仓。」

结论：外部模拟盘**确实带排板规则**，用户判断正确。本模块按上述公开规则复现，
不依赖任何第三方代码，零账号、零费用、可审计、可复现。

历史回放的硬限制（不隐藏、不伪造）
------------------------------------------------
仓库只有日线 OHLC，没有 Level-2 委托队列 / 分时逐笔。因此：
- **一字板**（O=H=L=C，全天封死）→ 确定买不进，跳过；
- 其余日子：`low < 涨停价` 只能说明当日曾出现低于涨停价的成交，
  无法判断它发生在「挂单之前」还是「挂单之后炸板」→ **排板是否成交不可判定**；
- 所以历史表格给的是「假设全部排到」的**乐观上界**，以及「全部未排到」的下界，
  真实成交率不可知；不得把上界当策略结论。
- 真实排板判定只在**实时留档**上做（有 `seal_fund` 封单额 / `broken` 开板次数）。

外部模拟盘（`chinapapertrading`）核实结论
------------------------------------------------
- 仓库真实：`silentsky2billten/chinapapertrading`（GitHub repo id 1141586655，
  创建 2026-01-25，最后推送 2026-01-30，2 stars / 0 forks / 1 open issue）。
- 实现：Selenium + Chrome CDP 端口 9223，靠 DOM 点击操作
  雪球 `xueqiu.com/performance` 与东财 `group.eastmoney.com/room/index.html?p=0`
  的**模拟组合**，需用户提前手动登录。
- 零费用：东财模拟组合初始虚拟资金 100 万元（Q037），雪球组合同为虚拟组合，
  均不接真实券商资金、无付费 API、无付费功能；GitHub 仓库公开免费。
- 但 **license = null**（无开源许可证，法律上保留所有权利），不可复制其代码；
  且 0 fork、约 9 个月未更新、纯 DOM 逆向易随页面改版失效、依赖账号登录。
- 因此本系统：**默认不启用外部模拟盘**，仅登记核实结论与只读连通性探测；
  **永不向任何外部账户发送交易指令**，确保绝对不会花钱。
  排板逻辑由本模块按公开规则本地复现。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# 数据与代码分离：留档根统一由 backend.paths 解析。
from backend import archive, paths  # noqa: E402

LIVE_DIR = paths.live_dir()

EXTERNAL_RESEARCH: dict[str, Any] = {
    "verified_at": "2026-11-12",
    "repo": "silentsky2billten/chinapapertrading",
    "repo_url": "https://github.com/silentsky2billten/chinapapertrading",
    "repo_id": 1141586655,
    "created_at": "2026-01-25", "last_push": "2026-01-30",
    "stars": 2, "forks": 0, "open_issues": 1,
    "license": None,
    "license_note": "仓库无 LICENSE 文件（GitHub API 返回 license=null），代码不可直接复制，仅作事实参考。",
    "platforms": [
        {"name": "雪球模拟组合", "url": "https://xueqiu.com/performance",
         "limit_rule": "涨停股无法买入但可卖出；组合会对调仓拆单，未成交部分参与下一次调仓。"},
        {"name": "东方财富模拟组合", "url": "https://group.eastmoney.com/room/index.html?p=0",
         "limit_rule": "涨停价委托单，涨停打开且卖一有单才成交；不开板不成交（Q037）。"},
    ],
    "free_evidence": [
        "东财帮助中心 Q037：模拟组合初始虚拟资金 100 万元，注册东财通行证即可，无真实资金。",
        "雪球组合为站内虚拟组合，不接券商实盘资金。",
        "GitHub 公开仓库，克隆/阅读免费，无付费 API。",
    ],
    "queue_rule_sources": [
        "东方财富帮助中心 Q037《沪深模拟组合怎么用?》涨跌停股票特殊规则",
        "东方财富《模拟炒股百科》涨停委托「记录当时买一量」队列口径",
        "雪球《产品说明 · 组合》调仓与交易规则说明",
    ],
    "tech": "Selenium + Chrome --remote-debugging-port=9223，DOM 元素点击，需手动登录",
    "risks": [
        "无开源许可证，不能复制其源码；本系统自行按公开规则实现。",
        "2 stars / 0 forks / 2026-01-30 后未推送，维护状态弱。",
        "纯 DOM 逆向，雪球或东财页面改版即失效。",
        "需要用户注册并登录账号，属于额外人工动作。",
    ],
    "decision": "不接为交易主引擎；本地按公开规则复现排板撮合，外部仅登记核实结论 + 可选只读探测。",
    "money_guarantee": "本系统不会向雪球/东财/任何券商账户发送下单、撤单或资金指令；无账号、无支付、无真实资金路径。",
}


# ---------------------------------------------------------------------------
# 历史日线：排板可成交性分类
# ---------------------------------------------------------------------------

def classify_history(open_px: float, high_px: float, low_px: float,
                     close_px: float, limit_price: float) -> dict[str, Any]:
    """按涨停排板口径给二板日分类。只做日线能支撑的判断，不猜队列。

    返回 status:
      - one_word   一字板，全天封死，确定买不进
      - sealed     非一字但全天价 ≥ 涨停价（不可能，保留兜底）
      - uncertain  曾出现低于涨停价的价格，但不知发生在挂单前后 → 不可判定
    """
    eps = max(0.005, limit_price * 0.0005)
    one_word = (abs(open_px - high_px) < 1e-9 and abs(open_px - low_px) < 1e-9
                and abs(open_px - close_px) < 1e-9)
    if one_word:
        return {"status": "one_word", "fillable": False,
                "reason": "一字涨停，全天无卖单释放，涨停价委托确定排不到"}
    if low_px >= limit_price - eps:
        return {"status": "sealed", "fillable": False,
                "reason": "全日成交价均在涨停价，封单未释放，队列未成交"}
    return {
        "status": "uncertain", "fillable": None,
        "reason": ("当日曾出现低于涨停价的成交，可能来自封板前或炸板后；"
                   "日线无委托队列/分时，无法判断挂单后是否开板 → 排板成交不可判定"),
        "has_below_limit_price": True,
    }


# ---------------------------------------------------------------------------
# 实时留档：真实排板判定（有封单额与开板次数）
# ---------------------------------------------------------------------------

def classify_live(row: dict[str, Any]) -> dict[str, Any]:
    """对实时涨停池的一条记录生成「涨停瞬间排板」委托单状态。

    依据东财 Q037：涨停价委托，开板且卖一有单才成交；不开板不成交。
    实时字段：`price`(现价=涨停价) / `seal_fund`(封单额, 元) / `broken`(开板次数)。
    """
    px = float(row.get("price") or 0)
    seal = row.get("seal_fund")
    broken = row.get("broken")
    order = {
        "order_type": "涨停价限价委托（排板）",
        "order_price": round(px, 2) if px > 0 else None,
        "order_moment": "二板封板瞬间（以留档帧时刻近似，逐笔封板时刻不可得）",
        "queue_position": "封单队列尾部",
        "seal_fund": seal,
        "seal_fund_wan": round(float(seal) / 10000, 1) if isinstance(seal, (int, float)) else None,
        "broken_times": broken,
        "fill_price": round(px, 2) if px > 0 else None,
    }
    if broken is None:
        order.update({
            "status": "不可判定", "filled": None,
            "reason": "留档缺开板次数字段，无法按 Q037 判定是否成交",
        })
    elif int(broken) > 0:
        order.update({
            "status": "开板过 · 按规则可成交", "filled": True,
            "reason": (f"当日开板 {int(broken)} 次；按东财 Q037，涨停价委托单在涨停打开、"
                       "卖一有委托单时即可成交，成交价 = 委托价（涨停价）"),
            # 阶段 1：filled=True 是 Q037 条件下的**乐观上界**，不是成交概率 1。
            # 队列位置、封单消耗、是否回封都不可得；下游结算/学习必须保守处理。
            "fill_evidence": "UPPER_BOUND",
            "fill_probability": None,
            "fill_note": ("开板只说明具备成交条件；队列在封单尾部，实盘未必排到。"
                          "此结论不得当真实成交率送入结算或参数学习"),
        })
    else:
        # 2026-11-20 修正（用户质疑「板了之后也会有交易」）：
        # 旧文案「不开板就不成交」是东财模拟组合 Q037 的简化口径，不是真实交易真相。
        # 真实涨停板上一直有成交。能否成交取决于「封单是否被吃穿」。
        # 这里只给粗判；精确判定见 fill_verify.simulate_fill 的封单消耗模拟
        # （真视通 11-18：封单 1.58→1.41 亿仅消耗 6%，没吃穿 → 确实排不到）。
        order.update({
            "status": "未开板 · 封单未被吃穿（粗判）", "filled": False,
            "reason": ("当日未开板；涨停价上仍有真实成交，但需看封单是否被吃穿。"
                       "本粗判仅按 Q037 简化口径给 filled=False，"
                       "精确判定请用 fill_verify.simulate_fill 的封单消耗模拟；"
                       "无逐笔队列，任何判定都是近似"),
        })
    return order


def live_queue_book(day: str | None = None) -> dict[str, Any]:
    """读取今日实时涨停池，为所有二板生成**展示用**排板观察队列（只读）。

    注意：这是给 /api/guitu 展示「今日所有二板的排板状态」的观察表，
    **不是** B1 的实际选股/建仓结果。真正开仓口径见 `live_sim.collect_guitu_targets`：
    情绪闸门 + 题材内「一字兔 → 买其他二板」，无组合则不开仓。
    """
    day = day or datetime.now().strftime("%Y%m%d")
    # 2026-11-25 瘦身：走 archive 抽象层（明文 JSONL / 盘后 gzip 都支持）。
    # 传入模块级 LIVE_DIR（测试会 monkeypatch 它），保持隔离语义。
    try:
        lines = archive.read_pool_lines(day, root=LIVE_DIR)
        if not lines:
            return {"available": False,
                    "reason": f"无 {archive.pools_plain_path(day, LIVE_DIR)}（或压缩归档）"}
        frame = json.loads(lines[-1])
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    rows = ((frame.get("zt") or {}).get("top") or [])
    orders = []
    for r in rows:
        try:
            if int(r.get("streak") or 0) != 2:
                continue
        except (TypeError, ValueError):
            continue
        name = str(r.get("name") or "")
        if "ST" in name.upper() or "退" in name:
            continue
        q = classify_live(r)
        orders.append({
            "strategy": "龟兔·二板涨停瞬间排板 → 断板当日收盘卖",
            "name": name, "ts_code": r.get("ts_code"),
            "industry": r.get("industry"), "turnover": r.get("turnover"),
            "streak": 2, "price": r.get("price"),
            "queue": q,
            "entry_rule": "二板封板瞬间挂涨停价排队（不默认成交）",
            "exit_rule": "首个未涨停日（断板）当日收盘卖出",
            "snapshot_at": frame.get("ts"),
        })
    n_fill = sum(1 for o in orders if o["queue"].get("filled") is True)
    n_wait = sum(1 for o in orders if o["queue"].get("filled") is False)
    return {
        "available": True, "day": day, "snapshot_at": frame.get("ts"),
        "orders": orders, "n": len(orders), "n_filled": n_fill, "n_waiting": n_wait,
        "n_unknown": len(orders) - n_fill - n_wait,
        "rule": "东财 Q037：涨停价委托，开板且卖一有单才成交；不开板不成交，成交价=委托价",
        "note": "展示用观察表：列出今日全部二板的 Q037 排板状态，**不是 B1 实际选股结果**"
                "（实际开仓见 /api/live-sim 与 live_sim.collect_guitu_targets）。"
                "此判定基于实时留档的开板次数与封单额，不向任何外部账户发送交易指令。",
    }


if __name__ == "__main__":
    print(json.dumps({"research": {k: v for k, v in EXTERNAL_RESEARCH.items()
                                   if k not in ("platforms", "free_evidence", "risks",
                                                "queue_rule_sources")}},
                     ensure_ascii=False, indent=2))
    print("live book:", json.dumps({k: v for k, v in live_queue_book().items()
                                    if k != "orders"}, ensure_ascii=False, indent=2))
