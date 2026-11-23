# -*- coding: utf-8 -*-
"""成本模型逐腿核算回归（2026-11-17 Q4 修复）。

背景
====
`round_trip_rate` 从 2026-11-13 起是「两腿都按买入成交额」的近似：
真实扣费是 `fee(买入额) + fee(卖出额)`，卖出腿多一笔印花税，且盈亏会改变
卖出额。12.34 元买 100 股、次日 13.50 卖：
  旧口径 = 14.3437 元；逐腿精确 = 14.5768 元（差 0.233 元 = 费用 1.6%）。
本文件锁住：
1. 旧 `round_trip_rate` 的数值**逐位不变**（历史报告可复现）；
2. 新增逐腿接口 `leg_fee` / `round_trip_fee` / `round_trip_rate(sell_notional=)`
   / `round_trip_rate_for_return` 与手工逐腿一致；
3. 平价（买卖同额）时两者**完全相等**（证明旧口径只在盈亏时才有偏差）；
4. 最低 5 元佣金、印花税只在卖出腿等边界（锁在 LEGACY_DEFAULT）。

当前 B1 口径（用户 2026-11-23 裁定）为万0.84免五 = N(costs.DEFAULT)，
见文件末尾 TestNewDefaultCostWan084 的专项断言。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import costs  # noqa: E402

#: 历史口径（万2.5+最低5元）——下面这些"逐位不变"的断言锁的是它。
#: 当前 B1 结算口径是万0.84免五（costs.DEFAULT），另有专门测试。
M = costs.LEGACY_DEFAULT
N = costs.DEFAULT


# ---------------------------------------------------------------------------
# 1) 旧口径向后兼容：数值必须逐位不变
# ---------------------------------------------------------------------------

def test_legacy_round_trip_rate_unchanged() -> None:
    """历史口径的数值锁定（这些常量被所有旧报告引用，不得变）。"""
    assert M.round_trip_rate(None) == pytest.approx(0.00402, abs=1e-12)
    assert M.round_trip_rate(0) == pytest.approx(0.00402, abs=1e-12)
    assert M.round_trip_rate(1234) == pytest.approx(0.0116237, abs=1e-7)
    assert M.round_trip_rate(5000) == pytest.approx(0.00552, abs=1e-12)
    # ≥20,000 元时最低佣金不生效（5 元 = 费率×2 万）
    assert M.round_trip_rate(20000) == pytest.approx(0.00402, abs=1e-12)
    assert M.round_trip_rate(50000) == pytest.approx(0.00402, abs=1e-12)
    # 显式传 None 与不传等价
    assert M.round_trip_rate(1234, sell_notional=None) == \
        pytest.approx(M.round_trip_rate(1234), abs=1e-15)


def test_legacy_zero_percentile_branch_unchanged() -> None:
    """notional<1 的旧分支仍返回纯费率+滑点。"""
    assert M.round_trip_rate(0.5) == pytest.approx(0.00402, abs=1e-12)


# ---------------------------------------------------------------------------
# 2) 逐腿精确值 = 手工逐腿
# ---------------------------------------------------------------------------

def test_leg_fee_matches_hand_calculation() -> None:
    # 买入 1,234：佣金 5（触最低）+ 过户 0.01234 + 滑点 1.851
    assert M.leg_fee(1234, sell=False) == pytest.approx(6.86334, abs=1e-9)
    # 卖出 1,350：佣金 5 + 印花税 0.675 + 过户 0.0135 + 滑点 2.025
    assert M.leg_fee(1350, sell=True) == pytest.approx(7.7135, abs=1e-9)


def test_round_trip_fee_1234_1350() -> None:
    """Q4 算例：12.34×100 买、13.50×100 卖 → 真实扣费 14.5768 元。"""
    f = M.round_trip_fee(1234.0, 1350.0)
    assert f["total_fee"] == pytest.approx(14.57684, abs=1e-9)
    assert f["buy"]["commission"] == pytest.approx(5.0)
    assert f["sell"]["commission"] == pytest.approx(5.0)
    assert f["buy"]["stamp"] == 0.0
    assert f["sell"]["stamp"] == pytest.approx(0.675)
    assert f["rate_on_buy"] == pytest.approx(14.57684 / 1234.0, abs=1e-12)
    assert f["rate_on_turnover"] == pytest.approx(
        14.57684 / (1234.0 + 1350.0), abs=1e-12)


def test_old_formula_understates_winning_trade_by_1_6pct() -> None:
    """旧口径对盈利单少算卖出侧费用（Q4 复现的 1.6%）。"""
    old_fee = M.round_trip_rate(1234.0) * 1234.0
    exact_fee = M.round_trip_fee(1234.0, 1350.0)["total_fee"]
    assert old_fee == pytest.approx(14.3437, abs=1e-4)
    assert exact_fee > old_fee
    diff = exact_fee - old_fee
    assert diff == pytest.approx(0.2332, abs=1e-4)
    assert diff / old_fee == pytest.approx(0.01626, abs=1e-4)


def test_old_formula_overstates_losing_trade() -> None:
    """亏损单方向相反：旧口径多算（卖出额更小）。"""
    old_rate = M.round_trip_rate(5000.0)
    exact_rate = M.round_trip_rate(5000.0, sell_notional=4500.0)
    assert exact_rate < old_rate
    assert exact_rate == pytest.approx(26.595 / 5000.0, abs=1e-12)


# ---------------------------------------------------------------------------
# 3) 平价（盈亏=0）时新旧口径完全一致（证明偏差只来自卖出额不同）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("notional", [1234.0, 5000.0, 20000.0, 100000.0])
def test_flat_trade_is_identical(notional: float) -> None:
    exact = M.round_trip_fee(notional, notional)
    assert exact["total_fee"] == pytest.approx(
        M.round_trip_rate(notional) * notional, abs=1e-9)
    assert M.round_trip_rate(notional, sell_notional=notional) == \
        pytest.approx(M.round_trip_rate(notional), abs=1e-15)


def test_sell_notional_defaults_to_buy_notional() -> None:
    f = M.round_trip_fee(5000.0)
    assert f["sell_notional"] == 5000.0
    assert f["total_fee"] == pytest.approx(27.6, abs=1e-9)


# ---------------------------------------------------------------------------
# 4) 收益率接口：round_trip_rate_for_return 与逐腿一致
# ---------------------------------------------------------------------------

def test_rate_for_return_matches_legs() -> None:
    buy = 1234.0
    gross = (1350.0 - 1234.0) / 1234.0
    r = M.round_trip_rate_for_return(buy, gross)
    assert r == pytest.approx(M.round_trip_fee(1234.0, 1350.0)["rate_on_buy"],
                              abs=1e-12)
    # 亏损侧同样精确
    r_loss = M.round_trip_rate_for_return(5000.0, -0.10)
    assert r_loss == pytest.approx(26.595 / 5000.0, abs=1e-12)


def test_rate_for_return_invalid_input_degrades_to_legacy() -> None:
    assert M.round_trip_rate_for_return(0, 0.1) == \
        pytest.approx(M.round_trip_rate(None), abs=1e-15)
    assert M.round_trip_rate_for_return(None, 0.1) == \
        pytest.approx(M.round_trip_rate(None), abs=1e-15)


# ---------------------------------------------------------------------------
# 5) 边界：最低佣金、印花税只在卖出腿、非法输入
# ---------------------------------------------------------------------------

def test_min_commission_binds_on_both_legs_small_amount() -> None:
    d = M.leg_fee_detail(100.0, sell=False)
    assert d["commission"] == pytest.approx(5.0)          # 触最低
    assert d["stamp"] == 0.0
    assert d["total"] == pytest.approx(5.0 + 0.001 + 0.15, abs=1e-12)


def test_stamp_duty_only_on_sell_leg() -> None:
    assert M.leg_fee_detail(100000.0, sell=False)["stamp"] == 0.0
    assert M.leg_fee_detail(100000.0, sell=True)["stamp"] == pytest.approx(50.0)


def test_invalid_notional_returns_zero_leg_fee() -> None:
    assert M.leg_fee(0) == 0.0
    assert M.leg_fee(None) == 0.0            # type: ignore[arg-type]
    assert M.leg_fee("abc") == 0.0           # type: ignore[arg-type]
    assert M.round_trip_fee(0, 0)["total_fee"] == 0.0


def test_custom_cost_model_uses_own_rates() -> None:
    m = costs.CostModel(commission_rate=0.0001, min_commission=0.0,
                        stamp_duty_sell=0.001, transfer_fee=0.0,
                        slippage_bps=0.0)
    f = m.round_trip_fee(10000.0, 11000.0)
    assert f["buy"]["commission"] == pytest.approx(1.0)
    assert f["sell"]["stamp"] == pytest.approx(11.0)
    assert f["total_fee"] == pytest.approx(1.0 + 1.1 + 11.0)


# ---------------------------------------------------------------------------
# 6) describe() 兼容性 + 新增精确字段
# ---------------------------------------------------------------------------

def test_describe_keeps_old_keys_and_adds_exact() -> None:
    d = M.describe(20000.0)
    for k in ("commission_rate", "min_commission", "stamp_duty_sell",
              "transfer_fee", "slippage_bps_per_side", "simple_round_trip",
              "round_trip_rate", "notional", "note"):
        assert k in d
    assert d["round_trip_rate"] == pytest.approx(M.round_trip_rate(20000.0))
    assert d["exact_flat_rate"] == pytest.approx(M.round_trip_rate(20000.0))
    assert d["exact_flat_fee"] == pytest.approx(80.4)
    assert "向后兼容" in d["round_trip_rate_basis"]


# ---------------------------------------------------------------------------
# 7) 当前 B1 口径：万 0.84 免五（用户 2026-11-23 裁定，单一口径）
# ---------------------------------------------------------------------------

class TestNewDefaultCostWan084:
    """万0.84免五：佣金 0.000084/边、无最低佣金。"""

    EXPECTED_RATE = 0.000084 * 2 + 0.0005 + 0.00001 * 2 + 0.0015 * 2  # 0.003688

    def test_rate_is_flat_regardless_of_notional(self) -> None:
        for n in (None, 0, 1, 100, 1234, 5000, 20000, 100000):
            assert N.round_trip_rate(n) == pytest.approx(self.EXPECTED_RATE, abs=1e-12)

    def test_no_minimum_commission(self) -> None:
        assert N.min_commission == 0.0
        assert N.leg_fee_detail(100.0, sell=False)["commission"] == pytest.approx(0.0084)
        assert N.leg_fee_detail(1_000_000.0, sell=False)["commission"] == pytest.approx(84.0)

    def test_stamp_only_on_sell(self) -> None:
        assert N.leg_fee_detail(100000.0, sell=False)["stamp"] == 0.0
        assert N.leg_fee_detail(100000.0, sell=True)["stamp"] == pytest.approx(50.0)

    def test_legacy_is_still_old_rate(self) -> None:
        assert M.commission_rate == 0.00025 and M.min_commission == 5.0
        assert M.round_trip_rate(None) == pytest.approx(0.00402, abs=1e-12)
