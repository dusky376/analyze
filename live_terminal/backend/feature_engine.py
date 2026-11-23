"""公共底层判据（特征工厂）：涨停价 / 涨停 / 连板 / 断板 / 跌停 / 涨跌幅限制 / ST。

为什么存在
------------------------------------------------
重构前 `limit_pct` 在 5 个文件、`is_st` 在 4 个文件、涨停连板判定在 3 个文件
各写一份（`papertrade` 还内联了一份 is_zt/is_dt）。经逐字节核对，这些副本
**当前完全一致**，但分散意味着「ST 泄露」这类修复要改 9 处，且只要漏改一处，
同一只票在同一天就可能在「龟兔回放」和「高标存亡」里判出不同连板数。
本模块是这些判据的唯一事实源，其它模块只转调，不再各自实现。

等价性（硬要求）
------------------------------------------------
`zt_flags` 的输出与重构前的 Python 循环实现**逐日逐票完全一致**，由
`tests/test_feature_parity.py` 用真实面板抽样 + 构造序列（含临界分位、
一字板、连续涨停、创业板/北交所 20%/30% 限制）验证。
任何判据变更都必须先过该测试，否则视为改口径而非重构。

向量化算法
------------------------------------------------
1. 连板：把「遇 False 归零、遇 True +1」转成
   `cumsum(zt) - 每个 True 段之前最后一个 False 处的 cumsum(zt)`，
   用 `np.maximum.accumulate` 求段起点，O(n) 且无 Python 循环。
2. 断板：对所有 False 位置做一次 `np.searchsorted`，得到每个 t 之后
   第一个未涨停日的下标，替代原先 O(n²) 的 `for x in range(t+1, n)`。

边界
------------------------------------------------
- 涨停价用「前收 ×(1+lim) 四舍五入到分」+ 0.005 容差，与旧实现一致。
- 这里不含任何策略阈值、成本口径或参数；那些仍由各自模块持有，本轮未改。
- 空输入时旧实现 `np.array([])` 是 float64，新实现是 bool；真实面板无空票，
  该 dtype 差异不影响任何落盘结果（见 parity 测试说明）。
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from . import trading_rules

# 北交所 30% / 创业板·科创板 20% / 主板 10%。与旧 5 份实现逐字节一致。
BJ_PREFIXES = ("43", "83", "87", "92")
GEM_PREFIXES = ("300", "301", "688")
ZT_TOL = 0.005


def limit_pct(code: str, *, is_st_flag: bool = False) -> float:
    """涨跌停幅度。唯一实现在 `trading_rules.limit_pct`。

    ⚠️ 2026-11-12 修正：主板 ST/*ST 为 **5%**（此前一律 10%，会把 ST 股涨 5%
    误判为涨停，或漏判真正的 5% 涨停）。创业板/科创板注册制后 ST 不降幅。
    依据见 `trading_rules.py` 模块文档。
    """
    return trading_rules.limit_pct(code, is_st_flag=is_st_flag)


def is_st(name: str) -> bool:
    n = (name or "").upper().replace(" ", "")
    return "ST" in n or "退" in n


def prev_close(c: np.ndarray) -> np.ndarray:
    """前收，首日设为 nan（与旧 `np.roll` + `prev[0]=nan` 一致）。"""
    a = np.asarray(c, dtype=float)
    if a.size == 0:
        return a.copy()
    prev = np.roll(a, 1)
    prev[0] = np.nan
    return prev


def prev_day_valid(dates: "list[str] | np.ndarray",
                   trading_days: "list[str]") -> np.ndarray:
    """标出「该行的上一行确实是上一个交易日」的位置。

    为什么需要
    ----------
    `prev_close()` 只是数组 ``shift(1)``，它假设数组里相邻两行就是相邻交易日。
    但面板存在股票级缺口（停牌、上市初期、adj 缺行）：某只票 11-10 之后直接
    出现 11-13 时，``shift(1)`` 会把 11-10 的收盘当成 11-13 的「前收」，
    于是把两日累计涨幅误判成单日涨停。

    实例（2026-11-16 体检）：``601091`` 在 ``panel_adj`` 缺 11-11/11-12 两行，
    11-13 被判成涨停，导致当日涨停家数 39 而真值 38。

    返回
    ----
    bool 数组，长度与 dates 相同；``False`` 表示该行的上一行不是上一交易日
    （首行恒为 False），调用方应把该日的前收视为不可用。
    """
    ds = [str(d)[:10] for d in dates]
    if not ds:
        return np.zeros(0, dtype=bool)
    cal = {str(d)[:10]: i for i, d in enumerate(trading_days)}
    out = np.zeros(len(ds), dtype=bool)
    for i in range(1, len(ds)):
        pi = cal.get(ds[i - 1])
        ci = cal.get(ds[i])
        out[i] = pi is not None and ci is not None and ci - pi == 1
    return out


def zt_price(code: str, c: np.ndarray,
             is_st_flags: np.ndarray | None = None,
             prev_valid: np.ndarray | None = None) -> np.ndarray:
    """每日涨停价；前收非正/缺失时为 nan。

    is_st_flags: 逐日 ST 标记（None = 全按非 ST）。主板 ST 为 5%。
    prev_valid:  可选，见 :func:`prev_day_valid`。为 False 的位置前收不可用，
                 涨停价置 nan（从而 zt/dt 都判 False、连板计数自然断裂）。

    ⚠️ 2026-11-13：舍入改用交易所口径（ROUND_HALF_UP），不再是 `np.round`。
    实测 3,708,840 行里有 253,236 行落在 `.xx5` 半分位，银行家舍入下
    其中 45 行被误判涨停。详见 `trading_rules.round_price`。
    """
    lim = trading_rules.limit_pct_series(code, is_st_flags)
    prev = prev_close(c)
    if prev_valid is not None:
        pv = np.asarray(prev_valid, dtype=bool)
        if pv.size == prev.size:
            prev = np.where(pv, prev, np.nan)
    return np.where(prev > 0, trading_rules.round_price(prev * (1 + lim)), np.nan)


def dt_price(code: str, c: np.ndarray,
             is_st_flags: np.ndarray | None = None) -> np.ndarray:
    """每日跌停价；前收非正/缺失时为 nan。舍入口径同 `zt_price`。"""
    lim = trading_rules.limit_pct_series(code, is_st_flags)
    prev = prev_close(c)
    return np.where(prev > 0, trading_rules.round_price(prev * (1 - lim)), np.nan)


def streak_from_zt(zt: np.ndarray) -> np.ndarray:
    """连续涨停天数（遇未涨停归零），与旧逐日循环逐元素等价。"""
    z = np.asarray(zt, dtype=bool)
    n = z.size
    if n == 0:
        return np.zeros(0, dtype=np.int32)
    cs = np.cumsum(z.astype(np.int64))
    # False 处的累计值，True 处填 0；向前取最大值即「本段起点前的累计」
    base = np.maximum.accumulate(np.where(z, 0, cs))
    return np.where(z, cs - base, 0).astype(np.int32)


def next_non_zt(zt: np.ndarray) -> np.ndarray:
    """out[t] = t 之后第一个未涨停日的下标；不存在为 -1。

    对应旧 `for x in range(t + 1, n): if not zt[x]: ...`，但用一次
    searchsorted 完成，避免 371 万行回放里的 O(n²) 内层循环。
    """
    z = np.asarray(zt, dtype=bool)
    n = z.size
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    false_at = np.flatnonzero(~z)
    if false_at.size == 0:
        return np.full(n, -1, dtype=np.int64)
    pos = np.searchsorted(false_at, np.arange(n), side="right")
    out = np.full(n, -1, dtype=np.int64)
    mask = pos < false_at.size
    out[mask] = false_at[pos[mask]]
    return out


def zt_flags(code: str, c: np.ndarray,
             is_st_flags: np.ndarray | None = None,
             prev_valid: np.ndarray | None = None
             ) -> tuple[np.ndarray, np.ndarray]:
    """(是否涨停, 连板高度)。

    is_st_flags: 逐日 ST 标记（None = 全按非 ST 处理，与旧行为一致）。
                 主板 ST 涨跌停为 5%，不传则按 10% 计算，会误判。
    prev_valid:  可选，见 :func:`prev_day_valid`。为 False 的位置前收不可用
                 → 该日不算涨停、连板计数在此断裂（不跨越停牌/缺行累计）。
    """
    a = np.asarray(c, dtype=float)
    ztp = zt_price(code, a, is_st_flags, prev_valid)
    zt = np.asarray(np.isfinite(ztp) & (a >= ztp - ZT_TOL), dtype=bool)
    return zt, streak_from_zt(zt)


def dt_flags(code: str, c: np.ndarray,
             is_st_flags: np.ndarray | None = None,
             prev_valid: np.ndarray | None = None) -> np.ndarray:
    """是否跌停。与 `papertrade.replay` 内联的 is_dt 逐日完全一致。

    prev_valid: 可选，见 :func:`prev_day_valid`。
    """
    a = np.asarray(c, dtype=float)
    ztp = zt_price(code, a, is_st_flags, prev_valid)
    dtp = dt_price(code, a, is_st_flags)
    prev = prev_close(a)
    if prev_valid is not None:
        pv = np.asarray(prev_valid, dtype=bool)
        if pv.size == prev.size:
            prev = np.where(pv, prev, np.nan)
    return np.asarray(
        np.isfinite(ztp) & np.isfinite(prev) & (a <= dtp + ZT_TOL), dtype=bool
    )


def rolling_prior(a: np.ndarray, win: int, how: str) -> np.ndarray:
    """`out[t] = how(a[t-win:t])`，**不含 t**，前 win 个为 nan。

    与 `papertrade._rolling_min/_rolling_max` 及 `vma20[t]=v[t-20:t].mean()`
    的旧逐日循环语义完全一致。用 sliding_window_view 一次算完，
    避免 5,580 只票 × 690 天 × 4 个指标的 Python 循环。

    注意：浮点求和顺序若与旧实现不同，可能让 `v/vma20 >= 1.5` 这类
    阈值在边界翻转 —— 是否等价必须由 parity 测试在 bit 级别证明，
    证明不了就不许接入回放（见 tests/test_feature_parity.py）。
    """
    x = np.asarray(a, dtype=float)
    n = x.size
    out = np.full(n, np.nan)
    if n <= win:
        return out
    w = sliding_window_view(x, win)          # w[i] = x[i : i+win]
    seg = w[: n - win]                       # w[t-win] = x[t-win : t]
    fn = {"min": np.min, "max": np.max, "mean": np.mean}[how]
    out[win:] = fn(seg, axis=1)
    return out
