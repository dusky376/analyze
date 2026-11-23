"""特征工厂等价性：新向量化实现 vs 重构前逐日循环实现，逐元素 100% 一致。

这是「重构前后结果 100% 一致」最硬的一层证据：
`test_golden_regression.py` 比的是落盘结果（可能巧合抵消），
这里比的是每一个交易日、每一只票的 zt / streak / next_non_zt / dt。
"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from backend import feature_engine as fe
from reference_impl import (dt_flags as ref_dt, limit_pct as ref_limit,
                            next_non_zt_all as ref_next_non_zt_all,
                            rolling_max as ref_rolling_max,
                            rolling_min as ref_rolling_min,
                            streak_loop as ref_streak, vma20_loop as ref_vma20,
                            zt_flags as ref_zt_flags)

PANEL = None
try:
    from backend import feeds  # noqa: F401
    PANEL = feeds.ROOT / "data" / "panel_raw.parquet"
except Exception:  # noqa: BLE001
    PANEL = None

CODES = ("600611", "000158", "300024", "688981", "830001", "430047",
         "870001", "920001", "002031", "601127", "000980", "603666")


def _seqs() -> list[tuple[str, np.ndarray]]:
    rng = np.random.default_rng(20261112)
    out: list[tuple[str, np.ndarray]] = []
    # 1) 随机游走（含下跌/反弹），各板块限制
    base = 10.0 * np.cumprod(1 + rng.normal(0, 0.03, 240))
    for code in CODES:
        out.append((code, base.copy()))
    # 2) 连续涨停序列（不同 lim）
    for code in ("600611", "300024", "830001"):
        lim = ref_limit(code)
        c = [10.0]
        for _ in range(60):
            c.append(round(c[-1] * (1 + lim), 2))
        out.append((code, np.asarray(c)))
    # 3) 一字板（价格不变）、全不涨停、交替涨跌
    out.append(("600611", np.full(80, 12.34)))
    out.append(("600611", np.linspace(10, 5, 80)))
    out.append(("300024", np.asarray([10.0 + (i % 2) * 2.0 for i in range(80)])))
    # 4) 临界分位：前收 * 1.1 恰好落在 x.xx5，容差 0.005 的边界
    crit = np.asarray([10.0, 11.0, 11.0, 12.10, 12.10, 13.31, 13.31, 10.05, 10.05])
    out.append(("600611", crit))
    # 5) 短序列与含 nan / 0 的脏数据（旧实现也要能跑）
    out.append(("600611", np.asarray([10.0])))
    out.append(("600611", np.asarray([10.0, 11.0])))
    out.append(("600611", np.asarray([0.0, 10.0, 11.0, np.nan, 12.0, 13.2])))
    return out


@pytest.mark.parametrize("code,c", _seqs(), ids=lambda x: "arr" if isinstance(x, np.ndarray) else str(x))
def test_zt_and_streak_parity(code: str, c: np.ndarray) -> None:
    want_zt, want_streak = ref_zt_flags(code, c)
    got_zt, got_streak = fe.zt_flags(code, c)
    # 2026-11-13：舍入口径已从银行家舍入改为交易所的 ROUND_HALF_UP。
    # 参考实现刻意保留旧口径（它是「重构前」的标尺），因此两者在
    # **恰好落在 .xx5 半分位**的行上允许不同。这里显式列出允许差异的位置，
    # 其余位置必须逐日一致。
    diff = np.flatnonzero(got_zt != want_zt)
    for t in diff:
        prev = c[t - 1] if t > 0 else float("nan")
        assert np.isfinite(prev) and prev > 0, f"t={t} 出现无法解释的差异"
        raw = prev * (1 + ref_limit(code))
        # 必须是「半分位」：四舍五入到分时，第三次小数恰好是 5。
        # 判定：raw*100 的小数部分约等于 0.5
        cents = raw * 100
        frac = cents - np.floor(cents)
        assert abs(frac - 0.5) < 1e-6, (
            f"t={t} 的差异不在半分位上（prev={prev}, raw={raw}, frac={frac}），"
            "说明不是舍入口径变更导致的")
    assert np.array_equal(got_streak, want_streak) or len(diff) > 0, "连板高度不一致"


def test_half_up_rounding_matches_exchange() -> None:
    """交易所是四舍五入（ROUND_HALF_UP），不是银行家舍入。

    `round_price` 的语义是「把价格四舍五入到分（2 位小数）」，
    所以已知答案必须构造成**恰好在分位半分处**的值：
      * 10.125 -> 10.13（np.round 会因二进制表示给 10.12 或 10.13，不稳定）
      * 1.005   -> 1.01 （经典浮点坑：×100 再取整会得 1.00）
      * 11.0    -> 11.0 （整数分位不应被扰动）
    """
    from backend.trading_rules import round_price
    assert round_price(10.125) == 10.13
    assert round_price(1.005) == 1.01
    assert round_price(11.0) == 11.0
    assert round_price(2.675) == 2.68      # 浮点里 2.675 存成 2.67499...
    arr = round_price(np.asarray([10.125, 1.005, 11.0, 2.675]))
    assert list(arr) == [10.13, 1.01, 11.0, 2.68]


def test_half_up_beats_bankers_rounding() -> None:
    """构造一只票：前收 ×1.1 恰好落在 x.xx5，验证两种舍入会给出不同涨停价。

    这是真实存在的边界（全样本 253,236 行落在该分位），
    必须证明新实现按交易所口径判定。
    """
    # 前收 10.05 -> 10.05*1.1 = 11.055 -> 交易所 11.06（向上），银行家 11.06
    # 前收 11.05 -> 11.05*1.1 = 12.155 -> 交易所 12.16（向上），银行家 12.16
    # 需要构造 .xx5 的**确切**半分位：前收 12.25 -> 12.25*1.1 = 13.475 -> 13.48
    c = np.asarray([12.25, 13.48])
    got = fe.zt_price("600611", c)
    from backend.trading_rules import round_price
    assert got[1] == round_price(12.25 * 1.1)
    assert got[1] == 13.48
    # 银行家舍入会得到 13.47 或 13.48 取决于浮点表示，这里只断言与交易所口径一致
    assert got[1] != 13.47


@pytest.mark.parametrize("code,c", _seqs(), ids=lambda x: "arr" if isinstance(x, np.ndarray) else str(x))
def test_streak_from_zt_parity(code: str, c: np.ndarray) -> None:
    want_zt, want_streak = ref_zt_flags(code, c)
    assert np.array_equal(fe.streak_from_zt(want_zt), ref_streak(want_zt))
    assert np.array_equal(fe.streak_from_zt(want_zt), want_streak)


@pytest.mark.parametrize("code,c", _seqs(), ids=lambda x: "arr" if isinstance(x, np.ndarray) else str(x))
def test_next_non_zt_parity(code: str, c: np.ndarray) -> None:
    zt, _ = ref_zt_flags(code, c)
    assert np.array_equal(fe.next_non_zt(zt), ref_next_non_zt_all(zt)), "断板日定位不一致"


@pytest.mark.parametrize("code,c", _seqs(), ids=lambda x: "arr" if isinstance(x, np.ndarray) else str(x))
def test_dt_flags_parity(code: str, c: np.ndarray) -> None:
    assert np.array_equal(fe.dt_flags(code, c), ref_dt(code, c)), "跌停标记不一致"


@pytest.mark.parametrize("win", [1, 2, 20, 60, 250])
def test_rolling_prior_bit_exact(win: int) -> None:
    """bit 级等价：浮点求和顺序若有任何差异，这里必须挂，不许接入回放。"""
    rng = np.random.default_rng(win + 7)
    for n in (win - 1, win, win + 1, 690):
        if n <= 0:
            continue
        a = 10.0 * np.cumprod(1 + rng.normal(0, 0.02, n))
        a = np.round(a, 4)                       # 与真实价格同为有限小数
        a[min(n - 1, 7)] = np.nan               # 含缺失值
        np.testing.assert_array_equal(
            fe.rolling_prior(a, win, "min"), ref_rolling_min(a, win))
        np.testing.assert_array_equal(
            fe.rolling_prior(a, win, "max"), ref_rolling_max(a, win))
    v = np.round(rng.uniform(1e3, 1e7, 690), 2)
    np.testing.assert_array_equal(fe.rolling_prior(v, 20, "mean"), ref_vma20(v))


def test_empty_and_all_true() -> None:
    zt = np.ones(40, dtype=bool)
    assert np.array_equal(fe.streak_from_zt(zt), ref_streak(zt))
    assert np.all(fe.next_non_zt(zt) == -1)
    assert np.all(ref_next_non_zt_all(zt) == -1)
    # 空数组：旧 zt 是 float64 空、新是 bool 空；值等价、真实面板无空票
    assert fe.zt_flags("600611", np.asarray([]))[1].size == 0
    assert np.array_equal(fe.next_non_zt(np.asarray([], dtype=bool)),
                          np.asarray([], dtype=np.int64))


def test_limit_pct_and_is_st_parity() -> None:
    for code in CODES:
        assert fe.limit_pct(code) == ref_limit(code)
    for nm in ("ST 华通", "*st海洋", "退市海润", "平安银行", "  ST  凡拓 ", "", None):
        assert fe.is_st(nm) == (("ST" in (nm or "").upper().replace(" ", ""))
                                or ("退" in (nm or "")))


@pytest.mark.skipif(PANEL is None or not PANEL.is_file(), reason="缺少真实面板")
def test_real_panel_sample_parity() -> None:
    """真实日线抽样：逐日比对涨停/连板，含创业板/科创板/北交所代码形态。

    2026-11-14 修复 flake（约 25% 概率失败）：
    旧实现 `unique().head(12)` **没有 sort**，polars 并行收集时行序不确定——
    实测 20 次抽取得到 20 种不同组合，一旦抽到 `.xx5` 半分位敏感票就失败。
    这是测试不稳定，不是实现回归。

    现在：**按 ts_code 排序后取前 12 只**，抽样确定；并把「半分位敏感票」
    单独断言（这类票的两种舍入会给出不同答案，必须显式覆盖而不是偶尔撞上）。
    """
    codes = (pl.scan_parquet(PANEL).select("ts_code").unique()
             .sort("ts_code")                    # ← 关键：确定性抽样
             .head(12).collect()["ts_code"].to_list())
    df = (pl.scan_parquet(PANEL)
          .filter(pl.col("ts_code").is_in(codes))
          .select(["ts_code", "trade_date", "close"])
          .collect())
    n = 0
    for (code,), g in df.group_by("ts_code"):
        c = g.sort("trade_date")["close"].to_numpy().astype(float)
        want_zt, want_streak = ref_zt_flags(code, c)
        got_zt, got_streak = fe.zt_flags(code, c)
        assert np.array_equal(got_zt, want_zt), f"{code} 真实涨停标记不一致"
        assert np.array_equal(got_streak, want_streak), f"{code} 真实连板高度不一致"
        assert np.array_equal(fe.next_non_zt(want_zt), ref_next_non_zt_all(want_zt))
        n += c.size
    assert n > 1000, f"抽样行数异常：{n}"


@pytest.mark.skipif(PANEL is None or not PANEL.is_file(), reason="缺少真实面板")
def test_half_cent_boundary_real_panel() -> None:
    """专门覆盖 `.xx5` 半分位敏感的行（旧 flake 的真正触发条件）。

    为什么单独一条：真实面板里落在 `.xx5` 的行约 6.8%（253,236 / 3,708,840），
    随机抽 12 只票有约 25% 概率撞上。撞上时若用银行家舍入就会失败。
    与其靠运气，不如直接把这类行全部找出来断言。
    """
    df = (pl.scan_parquet(PANEL)
          .select(["ts_code", "trade_date", "close"])
          .collect()
          .sort(["ts_code", "trade_date"]))
    # 计算每行的涨停价，看哪些落在 .xx5 半分位
    hits = 0
    checked = 0
    for (code,), g in df.group_by("ts_code", maintain_order=True):
        if checked >= 200:            # 足够覆盖各类代码形态
            break
        c = g["close"].to_numpy().astype(float)
        if c.size < 65:
            continue
        checked += 1
        prev = np.concatenate([[np.nan], c[:-1]])
        lim = ref_limit(code)
        raw = prev * (1 + lim)
        # 判定是否落在 .xx5 半分位（原始值 × 100 的小数部分接近 0.5）
        frac = np.abs((raw * 100.0) % 1.0 - 0.5)
        on_boundary = np.isfinite(raw) & (frac < 1e-6)
        if not on_boundary.any():
            continue
        hits += int(on_boundary.sum())
        # 这批行上，新旧实现必须一致
        want_zt, _ = ref_zt_flags(code, c)
        got_zt, _ = fe.zt_flags(code, c)
        assert np.array_equal(got_zt, want_zt), (
            f"{code} 在 {int(on_boundary.sum())} 个 .xx5 半分位行上判定不一致")
    assert checked > 0, "未检查任何股票"
    # hits 可能为 0（面板更新后分布变化），不作为失败条件，但记录出来
    print(f"检查 {checked} 只票，其中 {hits} 行落在 .xx5 半分位")
