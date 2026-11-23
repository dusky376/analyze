"""重构前的参考实现，逐字节复制自 2026-11-12 基线代码。

来源
------------------------------------------------
- `limit_pct` / `is_st`：`backend/papertrade.py:55,63`（另外 4 份实现经核对逐字节一致）
- `zt_flags`：`backend/guitu_backtest.py:63`（`leader_ladder.py:118` 等价，仅循环写法不同）
- `streak_loop`：`backend/papertrade.py:121`
- `next_non_zt_all`：对应 `backend/guitu_backtest.py:104` 的逐日找断板循环

**仅供 `test_feature_parity.py` 证明新旧实现逐日一致，生产代码禁止 import 本文件。**
这里保留 Python 循环是刻意的：它就是被替换掉的慢实现，是等价性的标尺。
"""
from __future__ import annotations

import numpy as np


def limit_pct(code: str) -> float:
    if code.startswith(("43", "83", "87", "92")):
        return 0.30
    if code.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


def is_st(name: str) -> bool:
    n = (name or "").upper().replace(" ", "")
    return "ST" in n or "退" in n


def zt_flags(code: str, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """标尺舍入口径已与现行 `feature_engine` 对齐（2026-11-13）。

    历史：本函数原用 `np.round`（银行家舍入）。feature_engine 改用交易所
    ROUND_HALF_UP（trading_rules.round_price，坑 24：45 行假涨停）后，
    标尺若不更新，parity 测试会在 `.xx5` 半分位行（如 920826 前收 11.85、
    30% 板价 15.405 → 银行家 15.40 / 交易所 15.41）报「不一致」——
    那不是回归，是标尺落后于口径修正。标尺同步改为交易所口径并留痕。
    """
    lim = limit_pct(code)
    prev = np.roll(c, 1)
    prev[0] = np.nan
    ztp = np.where(prev > 0, _exchange_round(prev * (1 + lim)), np.nan)
    zt = np.array([bool(np.isfinite(ztp[t]) and c[t] >= ztp[t] - 0.005)
                   for t in range(len(c))])
    streak = np.zeros(len(c), dtype=np.int32)
    for t in range(len(c)):
        streak[t] = (streak[t - 1] + 1 if t > 0 else 1) if zt[t] else 0
    return zt, streak


def _exchange_round(x):
    """与 trading_rules.round_price 同口径（逐标量 Decimal HALF_UP）。"""
    from decimal import ROUND_HALF_UP, Decimal

    def one(v):
        if not np.isfinite(v):
            return np.nan
        return float(Decimal(str(v)).quantize(Decimal("0.01"),
                                              rounding=ROUND_HALF_UP))
    x = np.asarray(x, dtype=float)
    out = np.full(x.shape, np.nan)
    with np.nditer(x, flags=["c_index"]) as it:
        for _ in it:
            out[it.index] = one(x[it.index])
    return out


def streak_loop(zt: np.ndarray) -> np.ndarray:
    s = np.zeros(len(zt), dtype=np.int32)
    for t in range(len(zt)):
        s[t] = (s[t - 1] + 1 if t > 0 else 1) if zt[t] else 0
    return s


def next_non_zt_all(zt: np.ndarray) -> np.ndarray:
    """每个 t 之后第一个未涨停日的下标；没有则 -1（对应旧 for x in range(t+1, n)）。"""
    n = len(zt)
    out = np.full(n, -1, dtype=np.int64)
    for t in range(n):
        for x in range(t + 1, n):
            if not zt[x]:
                out[t] = x
                break
    return out


def rolling_min(a: np.ndarray, win: int) -> np.ndarray:
    """重构前 `papertrade._rolling_min` 的逐日循环。"""
    out = np.full(len(a), np.nan)
    for t in range(win, len(a)):
        out[t] = np.min(a[t - win:t])
    return out


def rolling_max(a: np.ndarray, win: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    for t in range(win, len(a)):
        out[t] = np.max(a[t - win:t])
    return out


def vma20_loop(v: np.ndarray) -> np.ndarray:
    n = len(v)
    out = np.full(n, np.nan)
    for t in range(20, n):
        out[t] = v[t - 20:t].mean()
    return out


def dt_flags(code: str, c: np.ndarray) -> np.ndarray:
    """重构前 `papertrade.replay` 内联的 is_dt（跌停判定）。舍入口径同 zt_flags。"""
    lim = limit_pct(code)
    prev = np.roll(c, 1)
    prev[0] = np.nan
    ztp = np.where(prev > 0, _exchange_round(prev * (1 - lim)), np.nan)
    return np.array([
        bool(np.isfinite(ztp[t]) and np.isfinite(prev[t])
             and c[t] <= ztp[t] + 0.005)
        for t in range(len(c))
    ])
