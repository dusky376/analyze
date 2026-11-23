# -*- coding: utf-8 -*-
"""批量回填冰点样本库（用户裁定 B：命中即新增样本，参与校准）。

用法：
    python tools/backfill_ice_samples.py          # 回填最近 250 个交易日
    python tools/backfill_ice_samples.py --days 500
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import feeds, ice_gate, ice_samples  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250,
                    help="回填最近多少个交易日（默认 250）")
    args = ap.parse_args()

    panel = feeds.build_panel()
    m = ice_gate.metrics_all(panel).tail(args.days)
    days = [r["day"] for r in m.to_dicts()]
    if not days:
        print("无可用交易日")
        return

    # 用全历史分位（一次算好，避免逐日重复）
    bounds = ice_gate.compute_bounds(ice_gate.history(panel, days=250))

    st = ice_samples.load()
    n_new = n_ice = 0
    for d in days:
        rec = ice_gate.day_metrics(panel, d)
        if rec is None:
            continue
        dims = ice_gate.dim_levels(rec, bounds)
        lv = ice_gate.severity(dims)
        ice = ice_gate.is_ice(rec, dims)
        factor = ice_gate.size_factor(lv, ice)
        itype = ice_samples.ice_type(dims, rec) if ice else None
        if ice:
            n_ice += 1
        if ice_samples.record_day(st, day=d, metrics=rec, dims=dims, level=lv,
                                  is_ice=ice, size_factor=factor, itype=itype,
                                  source="auto"):
            n_new += 1

    # 记录漂移快照
    dr = ice_samples.drift(st)
    st.setdefault("drift_history", []).append({
        "at": ice_samples._now(),
        "anchor_n": dr["anchor_n"],
        "auto_n": dr["auto_n"],
        "overlap_n": dr["overlap_n"],
        "anchor_hit_rate": dr["anchor_hit_rate"],
        "window_days": args.days,
    })
    st["bounds_snapshot"] = bounds
    ice_samples.save(st)

    print(f"回填完成：交易日 {len(days)} 天（{days[0]} ~ {days[-1]}）")
    print(f"  冰点命中 {n_ice} 天，新增 auto 样本 {n_new} 条")
    print(f"  auto 样本总数 {len(st['auto_samples'])}，history {len(st['history'])}")
    print(f"  漂移监控：anchor={dr['anchor_n']} auto={dr['auto_n']} "
          f"重合={dr['overlap_n']} 命中率={dr['anchor_hit_rate']}")


if __name__ == "__main__":
    main()
