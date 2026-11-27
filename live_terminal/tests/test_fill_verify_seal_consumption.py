# -*- coding: utf-8 -*-
"""排板成交判定契约测试（2026-11-20 封单消耗修正）。

用户质疑：「板了之后也会有交易，不开板≠买不进」。
本测试锁住：不开板时也要看封单是否被吃穿，而不是直接断言"队列不可能被吃穿"。
"""
from __future__ import annotations

import pytest

from backend import fill_verify as fv


def _frame(ts: str, code: str, *, price=10.0, seal=None, broken=0, turnover=1.0):
    return {
        "ts": ts,
        "zt": {"all_complete": True, "all": [{
            "ts_code": code, "name": "测试", "price": price,
            "seal_fund": seal, "broken": broken, "turnover": turnover,
            "first_seal": "093000", "last_seal": "150000",
        }]},
        "dt": {"all": []},
    }


class TestSealConsumptionWithoutOpenBoard:
    def test_seal_eaten_through_marks_filled(self, monkeypatch):
        """全天未开板但封单被吃穿（消耗比≥1）→ 判为可成交（edge）。"""
        frames = [
            _frame("2026-11-18T09:38:12", "000001", seal=1e8, turnover=5.0),
            # 封单归零 = 前面队列全被吃掉（现实中通常伴随开板，这里刻意只测数值判定）
            _frame("2026-11-18T14:57:00", "000001", seal=0.0, turnover=20.0),
        ]
        monkeypatch.setattr(fv, "daily_amount", lambda day, c: 30e8)
        from backend import minute_flow as mf
        monkeypatch.setattr(mf, "amount_between",
                            lambda *a, **k: {"ok": False, "reason": "test-no-minute"})
        r = fv.simulate_fill("20261118", "000001", "2026-11-18T09:38:12",
                             frames, entry_price=10.0)
        assert r["status"] == "filled", r.get("detail")
        assert r["seal_consumed_yi"] > 0
        assert "封单" in r["detail"]

    def test_seal_not_eaten_stays_unfilled_with_correct_reason(self, monkeypatch):
        """封单只被少量消耗 → 未成交，但理由必须承认涨停价上有成交。"""
        frames = [
            _frame("2026-11-18T09:38:12", "000001", seal=1.58e8, turnover=7.11),
            _frame("2026-11-18T14:57:00", "000001", seal=1.41e8, turnover=7.81),
        ]
        monkeypatch.setattr(fv, "daily_amount", lambda day, c: 1.86e8)
        r = fv.simulate_fill("20261118", "000001", "2026-11-18T09:38:12",
                             frames, entry_price=10.0)
        assert r["status"] == "queued_unfilled"
        assert r["seal_consumed_yi"] == pytest.approx(0.17, abs=0.01)
        detail = r["detail"]
        assert "涨停价虽有成交" in detail, "必须承认板上成交，不再说'不可能被吃穿'"
        assert "没吃穿" in detail

    def test_old_absolute_claim_never_in_output(self, monkeypatch):
        """输出 detail 里不得再有「未开板→不可能被吃穿」的绝对断言。"""
        frames = [
            _frame("2026-11-18T09:38:12", "000001", seal=1.58e8, turnover=7.11),
            _frame("2026-11-18T14:57:00", "000001", seal=1.41e8, turnover=7.81),
        ]
        monkeypatch.setattr(fv, "daily_amount", lambda day, c: 1.86e8)
        r = fv.simulate_fill("20261118", "000001", "2026-11-18T09:38:12",
                             frames, entry_price=10.0)
        assert "不可能被吃穿" not in r["detail"]
