# -*- coding: utf-8 -*-
"""诊断：旧单板块口径 vs 新多标签匹配层，今日(11-24)的差异。只读。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import live_sim, theme_match as TM  # noqa: E402

DAY = "20261124"


def main() -> None:
    fr = live_sim.read_frames(DAY)
    allzt = live_sim.collect_all_zt(fr)
    tags = live_sim._theme_tags(DAY)

    # 新层：把 allzt 转成 theme_match 需要的 items
    items = [{"code": c, "name": g["name"],
              "tags": tags.get(c) or [],
              "industry": None, "streak": int(g.get("streak") or 0)}
             for c, g in allzt.items()]
    s = TM.coverage_stats(items)
    print(f"今日涨停 {s['n']} 只 | 有标签 {s['n_tagged']} ({s['tagged_pct']}%) "
          f"| 能映射板块 {s['n_axis']} ({s['axis_pct']}%)")

    two = {c for c, g in allzt.items() if int(g.get("streak") or 0) == 2}
    rabbits = {c for c in two if live_sim.is_rabbit(allzt[c])}
    print(f"二板 {len(two)} 只，兔子 {len(rabbits)} 只: {rabbits}")

    # 旧口径分组（模拟 collect_guitu_targets 的 by_theme）
    from collections import defaultdict
    by_theme = defaultdict(list)
    for c in allzt:
        for t in tags.get(c) or []:
            by_theme[t].append(c)

    # 找出含兔子的组
    rabbit_groups = {t: cs for t, cs in by_theme.items()
                     if any(c in rabbits for c in cs)}
    print(f"\n旧口径：含兔子的标签组 {len(rabbit_groups)} 个")
    for t, cs in sorted(rabbit_groups.items()):
        others = [c for c in cs if c not in rabbits
                  and int(allzt[c].get("streak") or 0) == 2]
        print(f"  [{t}] 兔子={[c for c in cs if c in rabbits]} 其他二板={others}")

    # 新层分组
    out = TM.group_by_theme(items)
    print(f"\n新层分组（axis/tag 级）:")
    for g in out["groups"]:
        r_in = [c for c in g["codes"] if c in rabbits]
        if r_in and g["level"] == "axis":
            others = [c for c in g["codes"] if c not in rabbits
                      and c in two]
            print(f"  [{g['theme']}] 兔子={r_in} 其他二板={others}")
    print(f"\n未成组: {[(u['code'], u['name']) for u in out['unlabeled']]}")


if __name__ == "__main__":
    main()
