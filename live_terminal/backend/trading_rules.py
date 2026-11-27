"""A 股交易规则事实层：涨跌停限制（悟空真经 B1 单策略）。

本模块是**唯一事实源**，所有判定必须调用这里，不得在别处重写。
所有规则均标注官方依据，未找到权威原文的一律标注「未取证」，不猜。

================================================================
涨跌停幅度（价格限制）
================================================================

| 板块 | 普通 | ST / *ST | 依据 |
|---|---|---|---|
| 主板（沪 60xxxx / 深 000、001、002、003） | 10% | **5%** | 沪深交易规则 |
| 创业板（300、301） | 20% | 20%（注册制后 ST 不降幅） | 深交所创业板规则 |
| 科创板（688、689） | 20% | 20%（同上） | 上交所科创板规则 |
| 北交所（43、83、87、92） | 30% | 30% | 北交所规则 |

⚠️ ST 降幅为 5% 只适用于**主板**。创业板/科创板注册制后 ST 股仍为 20%。
⚠️ 新股上市首日、退市整理期另有规则，本模块未覆盖（标注 LIMITATION）。
⚠️ 历史 ST 状态不可还原（无历史简称数据），本模块**只接受调用方传入的 ST 标记**，
   不得用「当前是否 ST」去判历史（那是未来函数，见 ST_LOOKAHEAD_BIAS_AUDIT.md）。

2026-11-23 精简：原「严重异常波动（监管指标，不是交易信号）」一族
（SEVERE_ABNORMAL / SEVERE_ABNORMAL_APPLICABLE / DeviationResult /
cumulative_deviation / check_severe_abnormal）生产与测试均零调用，已删除。
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# 涨跌停幅度
# ---------------------------------------------------------------------------

BJ_PREFIXES = ("43", "83", "87", "92")
GEM_PREFIXES = ("300", "301", "688", "689")

#: 涨跌停幅度（普通 / ST）
LIMIT_NORMAL = {
    "MAIN": 0.10,
    "GEM": 0.20,
    "BJ": 0.30,
}
LIMIT_ST = {
    "MAIN": 0.05,   # 主板 ST/*ST 降为 5%
    "GEM": 0.20,    # 注册制后创业板/科创板 ST 不降幅
    "BJ": 0.30,
}


def board_of(code: str) -> str:
    """判断板块：MAIN / GEM / BJ。"""
    if code.startswith(BJ_PREFIXES):
        return "BJ"
    if code.startswith(GEM_PREFIXES):
        return "GEM"
    return "MAIN"


def limit_pct(code: str, *, is_st_flag: bool = False) -> float:
    """涨跌停幅度。

    Args:
        code: 6 位股票代码
        is_st_flag: 该交易日是否为 ST/*ST。**必须由调用方按当日状态传入**，
                    不得用当前 ST 状态去判历史（未来函数）。

    Returns:
        涨跌幅比例，如 0.10 / 0.05 / 0.20 / 0.30
    """
    board = board_of(code)
    return LIMIT_ST[board] if is_st_flag else LIMIT_NORMAL[board]


def limit_pct_series(code: str, is_st_flags: np.ndarray | None = None) -> np.ndarray:
    """逐日涨跌停幅度数组。

    is_st_flags 为 None 时，全部按非 ST 处理（并应在调用方标注「ST 未建模」）。
    """
    board = board_of(code)
    normal = LIMIT_NORMAL[board]
    st_val = LIMIT_ST[board]
    if is_st_flags is None:
        return np.full(1, normal)  # 标量场景由调用方广播
    flags = np.asarray(is_st_flags, dtype=bool)
    return np.where(flags, st_val, normal)


def round_price(x: np.ndarray | float) -> np.ndarray | float:
    """按交易所规则把价格四舍五入到「分」，**不是银行家舍入**。

    2026-11-13 修复（口径）：`np.round` 用 IEEE 754 round-half-to-even，
    `2.5 -> 2`；A 股交易所是四舍五入（ROUND_HALF_UP），`2.5 -> 3`。
    实测 3,708,840 行里 253,236 行落在 `.xx5` 半分位，其中 45 行会被
    银行家舍入误判成涨停（假涨停，污染连板高度）。

    2026-11-14 修复（性能，审计未发现的 P0）：
    初版实现逐值 `Decimal(repr(v)).quantize(...)` Python 循环。
    `zt_price` 在 372 万行面板上被调 27,815 次 → 累计 **1860 万次 Decimal
    调用、37.4 秒**，把 `/api/plan` 拖到 42.9s。现改为**纯 numpy 向量化**：
      1. 先量化到 6 位小数（真实价格最多 3 位：2 位小数 × 涨跌幅），
         消除 float64 的 ulp 噪声；
      2. 再量化到「分」（×100）后做 half-up：`floor(s + 0.5 + eps)`。
    正确性由 `tests/test_round_price.py` 逐分支守住（含 .xx5 半分位、
    负零、NaN、标量与数组一致性），并与 Decimal 参考实现在随机样本上逐值比对。
    """
    EPS = 1e-9

    if np.isscalar(x):
        v = float(x)
        if not np.isfinite(v):
            return float("nan")
        # 标量路径保持与向量路径完全一致的算法，避免两条路径给出不同答案
        s = round(v, 6) * 100.0
        s = round(s, 6)                     # 消除 ×100 引入的 ulp 噪声
        return float(np.floor(s + 0.5 + EPS) / 100.0)

    arr = np.asarray(x, dtype=float)
    out = np.full(arr.shape, np.nan)
    finite = np.isfinite(arr)
    if not finite.any():
        return out
    v = arr[finite]
    # 1) 量化到 6 位小数：真实价格最多 3 位，这一步只吃掉浮点噪声
    q = np.round(v, 6)
    # 2) 转「分」再量化一次，让 1540.4999999999998 变回精确的 1540.5
    s = np.round(q * 100.0, 6)
    # 3) half-up：+0.5 后取 floor（加 EPS 抵消 floor 边界的浮点误差）
    out[finite] = np.floor(s + 0.5 + EPS) / 100.0
    return out


__all__ = [
    "board_of",
    "limit_pct",
    "limit_pct_series",
    "LIMIT_NORMAL",
    "LIMIT_ST",
]
