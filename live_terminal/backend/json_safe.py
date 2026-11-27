"""HTTP 序列化边界的非有限浮点净化（唯一实现）。

pandas / polars 计算缺值时会产出 NaN，而 Flask 的 jsonify 默认
allow_nan=True，会把裸 NaN 写进响应体。这不是合法 JSON：浏览器的
JSON.parse 会抛 SyntaxError，导致整个策略计划区块渲染中断。

这里把 NaN / ±Inf 统一转成 null（表示「无值」）。只在 HTTP 响应边界
生效，不改动任何业务数值，也不改写落盘数据。
"""
from __future__ import annotations

import math
from numbers import Real
from typing import Any


def sanitize(value: Any) -> Any:
    """递归返回可严格序列化的对象；非有限浮点 → None。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(v) for v in value]
    if isinstance(value, Real) and not isinstance(value, bool):
        # numpy 标量等 Real 子类：能转 float 的顺带检查有限性
        try:
            return value if math.isfinite(float(value)) else None
        except (OverflowError, ValueError):
            return None
    return value
