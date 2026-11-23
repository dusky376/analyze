"""A 股交易成本的**唯一事实源**。

为什么需要这个模块
==================
2026-11-13 审计发现全项目存在 **4 套互不相同的成本口径**：

| 历史口径 | 数值 |
|---|---|
| 早期 sweep | 双边 0.15% |
| 早期回放 | 双边 0.20% |
| 书面口径 | 0.26%/笔 |
| papertrade 口径 | 0.36%/笔 |

> 精简后只保留 `CostModel` 逐腿计算 + 展示层三口径并列（裁定⑥），
> 下表仅为历史考古，不是当前可用配置。

后果：**任意两份报告的「净收益」不在同一坐标系**，无法横向比较；
且 `guitu_sim.json` 的中位净只有 −0.19%，成本口径从 0.20% 换到 0.36%
就足以让结论从「微亏」变成「明显亏损」——**结论对成本假设的敏感度
高于策略本身的边际**。

本模块把这些收敛为**一个** `CostModel`，以真实费率逐腿计算，而不是一个拍脑袋的百分比。

当前口径（用户 2026-11-23 裁定，单一）
====================================
**万 0.84 免五**：佣金 0.000084/边、无最低 5 元；印花税卖出 0.05%、
过户费双向 0.001%、滑点双向 15bp。因免五，成本率与成交额无关，
`DEFAULT.round_trip_rate()` 恒为 0.003688（0.3688%/笔往返）。
历史「万 2.5 + 最低 5 元」口径保存在 `LEGACY_DEFAULT`，仅供旧报告逐位复现。

设计原则
========
* **单一口径**：`DEFAULT` 就是 B1 唯一成本口径，不再并列多套；
* **可配置但报告内一致**：`CostModel` 可传参，`LEGACY_DEFAULT` 仅考古；
* **打板策略的滑点必须显式**：默认值保守，且报告里必须打印出来；
* **逐腿精确 ≥ 单边近似**（2026-11-17 Q4 修复）：真实扣费是
  `fee(买入额) + fee(卖出额)`，卖出腿多一笔印花税、且盈亏会改变卖出额。
  `leg_fee()/round_trip_fee()` 给出精确值；`round_trip_rate()` 旧口径
  **保持不变**（数值逐位可复现），需要精确时传 `sell_notional=` 或
  `round_trip_rate_for_return()`。12.34→13.50 的 100 股算例旧口径少算
  0.233 元（费用的 1.6%，买入额的 1.89bp）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """可配置的成本模型。所有费率为**小数**（0.000084 = 万 0.84）。

    默认 `DEFAULT` 取用户裁定的万 0.84 免五口径，滑点按「打板/短线」偏保守。
    """

    commission_rate: float = 0.00025      # 佣金费率（双向）
    min_commission: float = 5.0           # 最低佣金（元/笔）
    stamp_duty_sell: float = 0.0005       # 印花税（卖出）
    transfer_fee: float = 0.00001         # 过户费（双向）
    slippage_bps: float = 15.0            # 单边滑点（基点）

    # ---- 兼容旧口径：某些报告只认一个百分比 ----
    @property
    def simple_round_trip(self) -> float:
        """不含最低佣金/滑点的「纯费率」双边合计，用于和旧口径对话。"""
        return (self.commission_rate * 2 + self.stamp_duty_sell
                + self.transfer_fee * 2)

    def round_trip_rate(self, notional: float | None = None, *,
                        sell_notional: float | None = None) -> float:
        """一次完整买卖的成本率（占**买入成交额**比例）。

        Args:
            notional: 单笔成交额（元）。传 None 表示「不模拟最低佣金」，
                      只返回费率部分（适合事件研究）。
            sell_notional: **可选**。卖出成交额。传入时走逐腿精确口径
                      （买入腿按 `notional`、卖出腿按 `sell_notional`）；
                      不传时保持 2026-11-13 起的历史口径（两腿都按买入额估），
                      **数值逐位不变**，保证所有历史报告可复现。

        注意：默认口径免五，成本率与 `notional` 无关；只有 `LEGACY_DEFAULT`
        （含最低 5 元）才随小额放大。

        2026-11-17（Q4 修复）：旧口径把「卖出腿」也按买入额计费，盈利单会
        少算卖出侧多出来的印花税/过户费/滑点（12.34→13.50 的 100 股算例
        少算 0.233 元 = 费用的 1.6%）。逐腿精确值调用
        `round_trip_fee(buy, sell)` 或本函数传 `sell_notional=`，旧调用不受影响。
        """
        base = self.simple_round_trip
        slip = self.slippage_bps / 10000.0 * 2      # 买 + 卖
        if notional is None or notional <= 0:
            return base + slip
        if notional < 1:
            return base + slip
        if sell_notional is not None:
            # 逐腿精确：费用总额 / 买入额
            return self.round_trip_fee(notional, sell_notional)["rate_on_buy"]
        # 最低佣金对单边的放大：max(5, notional*rate) / notional
        commission = max(self.min_commission, notional * self.commission_rate)
        comm_rate = commission / notional
        return (comm_rate * 2 + self.stamp_duty_sell
                + self.transfer_fee * 2 + slip)

    # ---- 逐腿精确核算（2026-11-17 新增，Q4 修复） -------------------------
    def leg_fee_detail(self, notional: float, *, sell: bool = False) -> dict[str, float]:
        """**单腿**费用明细（元）。

        买入腿：佣金（含最低 5 元）+ 过户费 + 滑点；
        卖出腿：再加印花税 0.05%。绝不拿买入额冒充卖出额。
        """
        try:
            n = float(notional)
        except (TypeError, ValueError):
            n = 0.0
        if not (n > 0):
            return {"notional": 0.0, "commission": 0.0, "stamp": 0.0,
                    "transfer": 0.0, "slippage": 0.0, "total": 0.0}
        commission = max(self.min_commission, n * self.commission_rate)
        stamp = n * self.stamp_duty_sell if sell else 0.0
        transfer = n * self.transfer_fee
        slip = n * self.slippage_bps / 10000.0
        return {"notional": n, "commission": commission, "stamp": stamp,
                "transfer": transfer, "slippage": slip,
                "total": commission + stamp + transfer + slip}

    def leg_fee(self, notional: float, *, sell: bool = False) -> float:
        """单腿费用合计（元）。买卖两腿分别调用即得精确往返费用。"""
        return self.leg_fee_detail(notional, sell=sell)["total"]

    def round_trip_fee(self, buy_notional: float,
                       sell_notional: float | None = None) -> dict[str, object]:
        """**逐腿**往返费用（元）：买入腿按买入额、卖出腿按卖出额。

        Args:
            buy_notional: 买入成交额（元）
            sell_notional: 卖出成交额（元）；None 时按平价（= 买入额）估算。

        Returns:
            dict：buy/sell 两腿明细、total_fee、rate_on_buy（总费用/买入额）、
            rate_on_turnover（总费用/(买+卖)）。
        """
        b = float(buy_notional) if buy_notional is not None else 0.0
        s = (float(sell_notional) if sell_notional is not None else b)
        bd = self.leg_fee_detail(b, sell=False)
        sd = self.leg_fee_detail(s, sell=True)
        total = bd["total"] + sd["total"]
        turnover = b + s
        return {
            "buy_notional": b, "sell_notional": s,
            "buy": bd, "sell": sd,
            "total_fee": total,
            "rate_on_buy": (total / b) if b > 0 else float("nan"),
            "rate_on_turnover": (total / turnover) if turnover > 0 else float("nan"),
        }

    def round_trip_rate_for_return(self, notional: float,
                                   gross_return: float) -> float:
        """已知「买入额 + 毛收益率」时的精确往返成本率（占买入额）。

        卖出额 = 买入额 × (1 + gross_return)；成本随盈利放大、随亏损缩小，
        与账户真实扣费一致（旧口径对盈亏两侧都按买入额，方向性偏差）。
        """
        try:
            b = float(notional)
            r = float(gross_return)
        except (TypeError, ValueError):
            return self.round_trip_rate(None)
        if not (b > 0):
            return self.round_trip_rate(None)
        return self.round_trip_rate(b, sell_notional=b * (1.0 + r))


    def describe(self, notional: float | None = None) -> dict[str, object]:
        """给报告用的可读说明（不要只写一个数字，要写清构成了什么）。"""
        try:
            n = float(notional) if notional is not None else 0.0
        except (TypeError, ValueError):
            n = 0.0
        exact = (self.round_trip_fee(n, n) if n > 0 else None)
        return {
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "stamp_duty_sell": self.stamp_duty_sell,
            "transfer_fee": self.transfer_fee,
            "slippage_bps_per_side": self.slippage_bps,
            "simple_round_trip": round(self.simple_round_trip, 6),
            "round_trip_rate": round(self.round_trip_rate(notional), 6),
            "round_trip_rate_basis": "历史口径：两腿都按买入成交额估算（向后兼容）",
            "exact_flat_fee": (round(exact["total_fee"], 4) if exact else None),
            "exact_flat_rate": (round(exact["rate_on_buy"], 6) if exact else None),
            "exact_rate_formula": ("逐腿：fee=leg_fee(买入额)+leg_fee(卖出额)，"
                                   "rate=fee/买入额；见 round_trip_fee()"),
            "notional": notional,
            "note": ("最低佣金对小资金影响显著：notional 越小，实际成本率越高；"
                     "slippage 是打板策略的主要隐性成本，已按单边计。"
                     "旧 round_trip_rate 在两腿金额不同（有盈亏）时与真实扣费"
                     "有 ≤2% 偏差（费用口径），精确值用 round_trip_fee() / "
                     "round_trip_rate(sell_notional=...)。"),
        }



# ---------------------------------------------------------------------------
# 默认成本模型（用户 2026-11-23 裁定：万 0.84 免五，单一口径）
#
#   佣金 万 0.84 = 0.000084/边（双向），**取消最低 5 元**（免五）
#   印花税 卖出 0.05%、过户费 双向 0.001%、滑点 双向 15bp
#   纯费率往返 = 0.000084*2 + 0.0005 + 0.00001*2 + 0.0015*2 = 0.003688
#   因免五，`round_trip_rate()` 不再依赖成交额，任何 notional 都是 0.003688。
#
# `LEGACY_DEFAULT` 是旧口径（万 2.5 + 最低 5 元），只为历史报告的逐位复现保留，
# **不得**用于 B1 新结算。
# ---------------------------------------------------------------------------
DEFAULT = CostModel(commission_rate=0.000084, min_commission=0.0)

#: 旧口径（万 2.5 + 最低 5 元），仅历史复现，不用于新结算。
LEGACY_DEFAULT = CostModel(commission_rate=0.00025, min_commission=5.0)

PRIMARY_ROUND_TRIP: float = 0.0036  # 历史遗留，非 B1 结算口径

LEGACY_CLAIMS = {
    "early_sweep_0.15pct": 0.0015,
    "early_backtest_0.20pct": 0.0020,
    "written_0.26pct": 0.0026,
    "legacy_0.36pct": 0.0036,
}


def primary_cost() -> float:
    """历史报告换算用（0.36%）；**B1 结算不得调用此函数**，请用 `DEFAULT`。"""
    return PRIMARY_ROUND_TRIP


