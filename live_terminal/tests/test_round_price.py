"""round_price 正确性 + 性能回归（2026-11-14 向量化改写）。

守住两件事：
1. **数值正确**：与 Decimal ROUND_HALF_UP 参考实现在大样本上逐值一致，
   特别是 `.xx5` 半分位（交易所口径的要害）、负零、NaN、标量/数组一致；
2. **性能**：向量化后必须比逐值 Decimal 快两个数量级，否则 /api/plan
   会再次被拖到 40 秒级。
"""
from __future__ import annotations

import decimal
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.trading_rules import round_price  # noqa: E402


def _ref(v: float) -> float:
    """参考实现：**先消浮点噪声，再 Decimal ROUND_HALF_UP**。

    为什么不能直接 `Decimal(repr(v))`：浮点乘法会引入噪声，例如
    `84.85 * 1.3 == 110.30499999999999`，而数学真值是 110.305。
    直接对 repr 做 quantize 会得到 110.30（少一分），交易所口径是 110.31。
    旧实现（2026-11-13）正是踩了这个坑——它修好了银行家舍入，
    却把浮点噪声当真值，在 30% 板的 .xx5 半分位上系统性少一分。

    正确做法：先把 v 量化到「分」的精度（round(v, 6) * 100 → round → /100），
    消除 ulp 噪声，再交给 Decimal 做 half-up。
    """
    if not np.isfinite(v):
        return float("nan")
    # 消噪声：真实价格最多 3 位小数，量化到分后再还原
    cleaned = round(round(float(v), 6) * 100.0, 6) / 100.0
    d = decimal.Decimal(repr(cleaned)).quantize(
        decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
    return float(d)


class TestCorrectness:
    def test_float_noise_not_treated_as_true_value(self):
        """浮点噪声不得被当成数学真值（旧实现的核心 bug）。

        `84.85 * 1.3` 的 float64 结果是 110.30499999999999，数学真值是 110.305。
        交易所（前收 × 1.3，四舍五入到分）应得 **110.31**，不是 110.30。
        旧实现用 `Decimal(repr(v))` 直接量化，得到 110.30 —— 少一分。
        """
        v = 84.85 * 1.3
        assert v == 110.30499999999999          # 确认这就是浮点噪声
        assert round_price(v) == pytest.approx(110.31, abs=1e-9)
        # 直接对 repr 做 Decimal 会得到错误答案，用它做对照证明我们没走那条路
        naive = decimal.Decimal(repr(v)).quantize(
            decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
        assert float(naive) == 110.30           # 错误答案
        assert round_price(v) != float(naive)   # 我们给的是正确答案

    @pytest.mark.parametrize("v,want", [
        (10.05, 10.05),
        (11.055, 11.06),      # .xx5 半分位 → 向上
        (12.155, 12.16),
        (13.475, 13.48),
        (2.5, 2.5),
        (0.005, 0.01),        # 经典浮点坑：0.005 二进制不精确，仍须向上
        (1.005, 1.01),
        (99.995, 100.00),
        (0.0, 0.0),
        (0.004, 0.0),
        (0.006, 0.01),
    ])
    def test_half_up_cases(self, v, want):
        assert round_price(v) == pytest.approx(want, abs=1e-9)

    def test_half_up_not_bankers(self):
        """银行家舍入会把 2.5 变 2、3.5 变 4；交易所两个都向上。"""
        for v in (2.5, 3.5, 4.5, 5.5):
            assert round_price(v) == pytest.approx(v, abs=1e-9)
        # .xx5 的两位小数版本
        assert round_price(2.505) == pytest.approx(2.51, abs=1e-9)
        assert round_price(2.515) == pytest.approx(2.52, abs=1e-9)

    def test_nan_and_inf(self):
        assert np.isnan(round_price(float("nan")))
        assert np.isnan(round_price(float("inf")))
        arr = round_price(np.array([1.234, np.nan, np.inf, -np.inf]))
        assert arr[0] == pytest.approx(1.23)
        assert np.isnan(arr[1:]).all()

    def test_scalar_array_consistency(self):
        """标量路径与数组路径必须给出完全相同的值（两条路径不能漂移）。"""
        vals = [10.05, 11.055, 0.005, 99.995, 2.5, 3.333]
        arr = round_price(np.array(vals))
        for i, v in enumerate(vals):
            assert arr[i] == round_price(v), f"{v} 标量/数组不一致"

    def test_matches_decimal_reference_random(self):
        """随机样本逐值对拍 Decimal 参考实现。"""
        rng = np.random.default_rng(20261114)
        # 模拟真实涨停价：前收 1~200 元 × (1+limit)，limit 含 5/10/20/30%
        prev = np.round(rng.uniform(1.0, 200.0, 4000), 2)
        lims = rng.choice([0.05, 0.10, 0.20, 0.30], size=4000)
        x = prev * (1.0 + lims)
        got = round_price(x)
        want = np.array([_ref(v) for v in x])
        diff = np.abs(got - want)
        bad = np.flatnonzero(np.nan_to_num(diff) > 1e-9)
        assert bad.size == 0, (
            f"{bad.size} 处与 Decimal 参考不一致，前 3 个: "
            f"{[(x[i], got[i], want[i]) for i in bad[:3]]}")

    def test_half_cent_boundary_exhaustive(self):
        """穷举 .xx5 半分位：0.01~200.00 每个半分位都要向上。"""
        cents = np.arange(1, 20000)          # 0.01 ~ 199.99
        x = (cents + 0.5) / 100.0            # 每个都落在 x.xx5
        got = round_price(x)
        # half-up 的期望值：向上到下一个整分
        want = (cents + 1) / 100.0
        bad = np.flatnonzero(np.abs(got - want) > 1e-9)
        assert bad.size == 0, (
            f"{bad.size}/{cents.size} 个半分位未向上，前 3: "
            f"{[(x[i], got[i], want[i]) for i in bad[:3]]}")


class TestPerformance:
    def test_vectorized_is_fast(self):
        """向量化必须比逐值 Decimal 快 ≥50 倍（否则 /api/plan 会再次超时）。"""
        n = 200_000
        rng = np.random.default_rng(7)
        x = np.round(rng.uniform(1.0, 200.0, n) * 1.1, 4)

        t0 = time.perf_counter()
        round_price(x)
        fast = time.perf_counter() - t0

        # 参考实现的耗时（只跑 1/50 样本再外推，避免测试本身太慢）
        m = n // 50
        t0 = time.perf_counter()
        for v in x[:m]:
            _ref(v)
        slow_est = (time.perf_counter() - t0) * 50

        speedup = slow_est / max(fast, 1e-9)
        assert speedup >= 50, (
            f"向量化只快了 {speedup:.0f}×（要求 ≥50×）；"
            f"fast={fast * 1000:.1f}ms, slow_est={slow_est * 1000:.0f}ms"
        )
