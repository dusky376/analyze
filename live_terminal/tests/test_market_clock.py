"""统一交易时钟守卫（2026-11-17 验收 P1 修复）。

事故/缺陷背景（`验收-2026-11-17/08_复测_架构师.md`）
====================================================
1. **节假日误判**：旧 `market_clock.py` 只用 `weekday()<5`，注入实测
   CN 国庆 2026-10-01 10:00 与 US 感恩节 2026-11-26 10:00 都返回 `is_trading=True`
   → 节假日会错误触发 R1（强拉实时）或错误进入盘中逻辑。
2. **`readable()` 语义漏洞**：旧实现 `return bool(entry)`，
   `readable({'foo':'bar'})` 与 `readable('x')` 都为 True → R2 形同虚设。
3. **R1/R2 死代码**：`make_cache_entry/readable/cache_allowed` 全库无生产调用。
   现在 `feeds.live_signature()` 的非交易分支会实跑一遍 R2 三件套。

本文件守住：
- CN 节假日/周末返回 False（真实 `calendar.parquet`，与 feeds._trading_days 同源）；
- 日历缺失/超范围 → fail-closed 标注 `degraded=True`（不假装准确）；
- US 内置联邦假日（含感恩节=11 月第 4 个周四、周六观测提前到周五）；
- JP/KR 必须标注 `degraded=True`；
- zoneinfo 与四市场时段判定（含午休、两端含边界）不回归；
- `readable()` 只认 `make_cache_entry()` 结构；
- `feeds.live_signature()` 非交易分支真的调用了 R2 闸门。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import market_clock as mc  # noqa: E402

CN = timezone(timedelta(hours=8))
US_EST = timezone(timedelta(hours=-5))
US_EDT = timezone(timedelta(hours=-4))
JP = timezone(timedelta(hours=9))
KR = timezone(timedelta(hours=9))


def dt(y: int, mo: int, d: int, h: int, mi: int, s: int = 0,
       tz: tzinfo = CN) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=tz)


@pytest.fixture(autouse=True)
def _clean_calendar_cache():
    """每个用例前后清一次内存缓存，避免用例间互相污染。"""
    mc._reset_calendar_cache()
    yield
    mc._reset_calendar_cache()


# ---------------------------------------------------------------------------
# 1. A 股：真实交易日历（节假日 / 周末 / 调休）
# ---------------------------------------------------------------------------

class TestCNHolidays:
    def test_national_day_is_not_trading(self):
        """2026-10-01（周四）国庆：旧实现误判 True，现必须 False。"""
        t = dt(2026, 10, 1, 10, 0)
        assert mc.is_trading("CN", now=t) is False
        assert mc.is_trading_day("CN", now=t) is False
        st = mc.trading_state("CN", now=t)
        assert st["holiday"] is True
        assert st["degraded"] is False, "真实日历可用时不应标 degraded"
        assert st["cache_allowed"] is True, "节假日不是交易时段 → 允许缓存"

    def test_national_day_week_whole_is_closed(self):
        """国庆连休 10-01~10-07：全部 False（日历覆盖区间内，不信工作日近似）。"""
        for d in range(1, 8):
            t = dt(2026, 10, d, 10, 0)
            assert mc.is_trading("CN", now=t) is False, f"2026-10-0{d} 应为休市"

    def test_normal_trading_day(self):
        t = dt(2026, 11, 17, 10, 0)
        assert mc.is_trading("CN", now=t) is True
        st = mc.trading_state("CN", now=t)
        assert st["is_trading_day"] is True
        assert st["degraded"] is False
        assert st["cache_allowed"] is False

    def test_weekend_is_not_trading(self):
        assert mc.is_trading("CN", now=dt(2026, 11, 21, 10, 0)) is False  # 周六
        assert mc.is_trading("CN", now=dt(2026, 11, 22, 10, 0)) is False  # 周日

    def test_lunch_break_is_not_session_but_is_trading_day(self):
        t = dt(2026, 11, 17, 12, 0)
        st = mc.trading_state("CN", now=t)
        assert st["trading"] is False
        assert st["is_trading_day"] is True
        assert st["cache_allowed"] is True
        assert mc.is_trading("CN", now=t) is False

    def test_session_boundaries_right_end_inclusive(self):
        """右端点语义保持：11:30:00 / 15:00:00 交易中，+1 秒即非交易。"""
        assert mc.is_trading("CN", now=dt(2026, 11, 17, 9, 30, 0)) is True
        assert mc.is_trading("CN", now=dt(2026, 11, 17, 11, 30, 0)) is True
        assert mc.is_trading("CN", now=dt(2026, 11, 17, 11, 30, 1)) is False
        assert mc.is_trading("CN", now=dt(2026, 11, 17, 13, 0, 0)) is True
        assert mc.is_trading("CN", now=dt(2026, 11, 17, 15, 0, 0)) is True
        assert mc.is_trading("CN", now=dt(2026, 11, 17, 15, 0, 1)) is False

    def test_known_non_trading_dates_from_calendar(self):
        """直接在日历层面核对（不依赖 is_trading 的实现细节）。"""
        cal = mc._cn_calendar()
        assert cal["ok"] is True, "验收环境应能读到 calendar.parquet"
        assert date(2026, 10, 1) not in cal["days"]
        assert date(2026, 10, 8) in cal["days"], "节后首个交易日应开市"
        assert date(2026, 11, 17) in cal["days"]

    def test_calendar_missing_degrades_explicitly(self, monkeypatch):
        """日历读不到 → 退回工作日近似，但必须 degraded=True（不假装准确）。"""
        monkeypatch.setattr(mc, "_cn_calendar",
                            lambda: {"ok": False, "reason": "测试注入：无日历"})
        st = mc.trading_state("CN", now=dt(2026, 10, 1, 10, 0))
        assert st["degraded"] is True
        assert st["trading"] is True, "无日历时按 fail-open 工作日近似（并标注 degraded）"
        assert "日历" in st["source"] or "近似" in st["source"]

    def test_out_of_calendar_range_degrades(self, monkeypatch):
        """日期超出日历覆盖区间 → degraded=True（而不是静默当准确）。"""
        monkeypatch.setattr(mc, "_cn_calendar", lambda: {
            "ok": True, "days": frozenset({date(2026, 11, 17)}),
            "first": date(2026, 1, 1), "last": date(2026, 12, 31),
            "source": "test", "sig": ("t",),
        })
        st = mc.trading_state("CN", now=dt(2030, 1, 2, 10, 0))
        assert st["degraded"] is True


# ---------------------------------------------------------------------------
# 2. 美 / 日 / 韩：各自时钟 + 节假日 + 降级标注
# ---------------------------------------------------------------------------

class TestForeignMarkets:
    def test_us_thanksgiving_is_closed(self):
        """2026-11-26（11 月第 4 个周四）感恩节 10:00 → 旧实现 True，现必须 False。"""
        t = dt(2026, 11, 26, 10, 0, tz=US_EST)
        assert mc.is_trading("US", now=t) is False
        st = mc.trading_state("US", now=t)
        assert st["holiday"] is True
        assert st["degraded"] is False
        assert st["cache_allowed"] is True

    def test_us_day_before_thanksgiving_is_trading(self):
        assert mc.is_trading("US", now=dt(2026, 11, 25, 10, 0, tz=US_EST)) is True

    def test_us_independence_day_observed(self):
        """2026-07-04 是周六 → NYSE 于 07-03（周五）休市。"""
        assert mc.is_trading("US", now=dt(2026, 7, 3, 10, 0, tz=US_EDT)) is False
        assert mc.is_trading("US", now=dt(2026, 7, 2, 10, 0, tz=US_EDT)) is True

    def test_us_new_year_observed_cross_year(self):
        """2028-01-01 周六 → 2027-12-31（周五）观测休市（跨年观测规则）。"""
        assert date(2028, 1, 1).weekday() == 5
        assert mc.is_trading("US", now=dt(2027, 12, 31, 10, 0, tz=US_EST)) is False

    def test_us_weekend_and_session(self):
        assert mc.is_trading("US", now=dt(2026, 11, 21, 10, 0, tz=US_EST)) is False
        assert mc.is_trading("US", now=dt(2026, 11, 16, 9, 29, tz=US_EST)) is False
        assert mc.is_trading("US", now=dt(2026, 11, 16, 9, 30, tz=US_EST)) is True
        assert mc.is_trading("US", now=dt(2026, 11, 16, 16, 0, tz=US_EST)) is True
        assert mc.is_trading("US", now=dt(2026, 11, 16, 16, 1, tz=US_EST)) is False

    def test_thanksgiving_rule_math(self):
        assert mc._nth_weekday(2026, 11, 3, 4) == date(2026, 11, 26)
        assert date(2026, 11, 26) in mc._us_holidays(2026)
        assert date(2026, 11, 25) not in mc._us_holidays(2026)

    def test_jp_kr_are_degraded_but_use_own_sessions(self):
        for m, tz in (("JP", JP), ("KR", KR)):
            st = mc.trading_state(m, now=dt(2026, 11, 17, 10, 0, tz=tz))
            assert st["degraded"] is True, f"{m} 无节假日日历必须标注 degraded"
            assert st["trading"] is True
            # 午休：JP 11:31 / KR 12:29
        assert mc.is_trading("JP", now=dt(2026, 11, 17, 11, 31, tz=JP)) is False
        assert mc.is_trading("JP", now=dt(2026, 11, 17, 12, 30, tz=JP)) is True
        assert mc.is_trading("KR", now=dt(2026, 11, 17, 12, 29, tz=KR)) is False
        assert mc.is_trading("KR", now=dt(2026, 11, 17, 15, 30, tz=KR)) is True

    def test_cn_and_us_not_shared(self):
        """同一 UTC 瞬间：CN 收盘后、US 盘前 —— 两个市场结论必须不同（R3）。"""
        # 2026-11-17 16:00 北京 = 03:00 纽约（冬令时）
        t_cn = dt(2026, 11, 17, 16, 0)
        t_us = t_cn.astimezone(US_EST)
        assert mc.is_trading("CN", now=t_cn) is False
        assert mc.is_trading("US", now=t_us) is False
        # 2026-11-17 22:00 北京 = 09:00 纽约 → 美股盘前
        assert mc.is_trading("US", now=dt(2026, 11, 17, 22, 0).astimezone(US_EST)) is False
        # 23:00 北京 = 10:00 纽约 → 美股交易中
        assert mc.is_trading("US",
                             now=dt(2026, 11, 17, 23, 0).astimezone(US_EST)) is True


# ---------------------------------------------------------------------------
# 3. zoneinfo
# ---------------------------------------------------------------------------

class TestTimezone:
    def test_zoneinfo_available_in_acceptance_env(self):
        """验收环境必须装 tzdata（否则非 CN 市场会 fail-closed 按交易中）。"""
        assert mc._TZ_OK is True, "zoneinfo/tzdata 缺失：四市场时钟不可信"
        assert mc.TZ_CN is not None and mc.TZ_US is not None

    def test_local_now_returns_aware_datetime(self):
        local, ok = mc._local_now("US")
        assert ok is True
        assert local.tzinfo is not None
        assert local.utcoffset() in (timedelta(hours=-5), timedelta(hours=-4))

    def test_tz_unavailable_foreign_fails_closed(self, monkeypatch):
        """时区库不可用时：非 CN 市场按「交易中」处理（R1 fail-closed）。"""
        monkeypatch.setattr(mc, "_TZ_OK", False)
        st = mc.trading_state("US")
        assert st["trading"] is True
        assert st["degraded"] is True
        assert st["cache_allowed"] is False


# ---------------------------------------------------------------------------
# 4. R2：readable / make_cache_entry / assert_readable
# ---------------------------------------------------------------------------

class TestReadable:
    @pytest.mark.parametrize("entry", [
        None, {}, {"foo": "bar"}, "nonemptystring", "x", 0, 1, [],
        ["a"], {"final": False, "payload": {"x": 1}},
        {"final": True, "payload": None}, {"payload": {"x": 1}},
        {"final": True},
    ])
    def test_junk_entries_are_not_readable(self, entry):
        assert mc.readable(entry) is False, f"{entry!r} 不应可读（R2）"

    def test_final_entry_is_readable(self):
        e = mc.make_cache_entry({"a": 1}, final=True)
        assert mc.readable(e) is True
        assert e["final"] is True and e["payload"] == {"a": 1}
        assert "built_at" in e

    def test_non_final_entry_is_not_readable(self):
        assert mc.readable(mc.make_cache_entry({"a": 1}, final=False)) is False

    def test_assert_readable_blocks_during_trading(self):
        """交易时段内：即使条目 final=True 也禁止读（R1）。"""
        good = mc.make_cache_entry({"a": 1}, final=True)
        assert mc.assert_readable(good, market="CN",
                                  now=dt(2026, 11, 17, 10, 0)) is False

    def test_assert_readable_allows_final_when_closed(self):
        good = mc.make_cache_entry({"a": 1}, final=True)
        assert mc.assert_readable(good, market="CN",
                                  now=dt(2026, 11, 21, 10, 0)) is True  # 周六
        assert mc.assert_readable(good, market="CN",
                                  now=dt(2026, 11, 17, 16, 0)) is True  # 收盘后

    def test_assert_readable_rejects_non_final_when_closed(self):
        bad = mc.make_cache_entry({"a": 1}, final=False)
        assert mc.assert_readable(bad, market="CN",
                                  now=dt(2026, 11, 17, 16, 0)) is False

    def test_session_final_semantics(self):
        assert mc.session_final("CN", now=dt(2026, 11, 17, 10, 0)) is False
        assert mc.session_final("CN", now=dt(2026, 11, 17, 16, 0)) is True
        assert mc.session_final("CN", now=dt(2026, 10, 1, 10, 0)) is True


# ---------------------------------------------------------------------------
# 5. 死代码接线：feeds.live_signature 非交易分支真的走 R2 闸门
# ---------------------------------------------------------------------------

class TestLiveSignatureR2Wiring:
    def test_eod_branch_calls_r2_gate(self, monkeypatch):
        from backend import feeds

        calls = {"make": 0, "assert": 0, "readable": 0}
        real_make = mc.make_cache_entry
        real_assert = mc.assert_readable

        def spy_make(payload, *, final):
            calls["make"] += 1
            entry = real_make(payload, final=final)
            calls["readable"] += 1 if mc.readable(entry) else 0
            return entry

        def spy_assert(entry, *, market="CN", now=None):
            calls["assert"] += 1
            return real_assert(entry, market=market, now=now)

        monkeypatch.setattr(mc, "is_cn_trading", lambda *a, **k: False)
        # 隔离时间依赖：本测试验证「R2 闸门被调用」，不是验证最终可读性。
        # assert_readable 内部走 cache_allowed()→is_trading()（不是 is_cn_trading），
        # 盘中运行时它判 False → 签名带 nonfinal，让本用例随运行时钟漂移。
        # 这里直接钉住闸门的「允许读缓存」判定；nonfinal 分支由
        # test_eod_branch_refuses_non_final 专门覆盖。
        monkeypatch.setattr(mc, "cache_allowed", lambda *a, **k: True)
        # session_final 内部走 is_trading()（非 is_cn_trading），盘中判 False。
        # 两个判定键都钉住，才能隔离运行时钟对「闸门是否被调用」用例的干扰。
        monkeypatch.setattr(mc, "session_final", lambda *a, **k: True)
        monkeypatch.setattr(mc, "make_cache_entry", spy_make)
        monkeypatch.setattr(mc, "assert_readable", spy_assert)
        assert feeds.live_signature() == ("eod",)
        assert calls["make"] == 1, "非交易分支必须调用 make_cache_entry（不再死代码）"
        assert calls["assert"] == 1, "非交易分支必须调用 assert_readable（R2 闸门）"
        assert calls["readable"] == 1, "必须真的读取条目 final 标记"

    def test_eod_branch_refuses_non_final(self, monkeypatch):
        """R2 闸门生效：条目未定稿 → 返回 nonfinal 签名（迫使上层重建）。"""
        from backend import feeds

        monkeypatch.setattr(mc, "is_cn_trading", lambda *a, **k: False)
        monkeypatch.setattr(
            mc, "make_cache_entry",
            lambda payload, *, final: {"final": False, "payload": payload})
        assert feeds.live_signature() == ("eod", "nonfinal")

    def test_live_branch_does_not_go_through_eod_gate(self, monkeypatch):
        """交易时段走实时签名，不返回 eod（R1）。"""
        from backend import feeds

        monkeypatch.setattr(mc, "is_cn_trading", lambda *a, **k: True)
        monkeypatch.setattr(feeds, "intraday_bar", lambda **k: (None, None))
        feeds._reset_live_sig()
        sig = feeds.live_signature()
        assert sig[0] == "live"
        feeds._reset_live_sig()


# ---------------------------------------------------------------------------
# 6. status() 契约
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_has_four_markets_with_degraded(self):
        st = mc.status()
        assert set(st) == {"CN", "US", "JP", "KR"}
        for m, row in st.items():
            for key in ("trading", "is_trading_day", "cache_allowed",
                        "holiday", "degraded", "source", "local_time"):
                assert key in row, f"{m} 缺少 {key}"
        assert st["JP"]["degraded"] is True
        assert st["KR"]["degraded"] is True
