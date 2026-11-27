"""HTTP 响应必须是严格合法 JSON：浏览器 JSON.parse 不接受裸 NaN/Infinity。"""
import json
import math

import pytest

from backend.json_safe import sanitize


def test_non_finite_floats_become_null():
    raw = {
        "amount": float("nan"),
        "high": float("inf"),
        "low": float("-inf"),
        "chg": -0.05532,
        "n": 15,
        "flag": True,
        "none": None,
        "text": "NaN 只是字符串内容，不该被动到",
    }
    out = sanitize(raw)
    assert out["amount"] is None
    assert out["high"] is None
    assert out["low"] is None
    assert out["chg"] == -0.05532
    assert out["n"] == 15
    assert out["flag"] is True
    assert out["none"] is None
    assert out["text"] == raw["text"]
    encoded = json.dumps(out, allow_nan=False, ensure_ascii=False)
    json.loads(encoded, parse_constant=lambda c: pytest.fail(f"非法字面量: {c}"))


def test_nested_structures_and_tuples():
    raw = {
        "rows": [{"turnover": float("nan")}, {"xs": [float("inf"), 2]}],
        "pair": (float("nan"), 3),
        "uniq": {float("inf"), 1},
    }
    out = sanitize(raw)
    assert out["rows"][0]["turnover"] is None
    assert out["rows"][1]["xs"] == [None, 2]
    assert out["pair"] == [None, 3]
    assert all(v is None or v == 1 for v in out["uniq"])
    json.dumps(out, allow_nan=False)


def test_input_is_not_mutated():
    raw = {"x": float("nan"), "rows": [float("inf")]}
    sanitize(raw)
    assert math.isnan(raw["x"])
    assert math.isinf(raw["rows"][0])
