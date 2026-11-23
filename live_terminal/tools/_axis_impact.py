# -*- coding: utf-8 -*-
"""验证扩轴前后，悟空真经实际选股的差异（隔离账本，只读）。

对比 11-16~11-20，看补轴后是否多识别出兔子/多产生候选。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import live_sim  # noqa: E402


def main() -> None:
    print("=" * 86)
    print("扩轴后（theme_axis v3, 422 标签 / 11 板块）的真实选股结果")
    print("=" * 86)
    for day in ["20261113", "20261116", "20261117", "20261118", "20261120"]:
        fr = live_sim.read_frames(day)
        if not fr:
            print(f"\n{day}: 无留档帧，跳过")
            continue
        allzt = live_sim.collect_all_zt(fr)
        tags = live_sim._theme_tags(day)
        two = [c for c, g in allzt.items() if int(g.get("streak") or 0) == 2]
        rabbits = [c for c in two if live_sim.is_rabbit(allzt[c])]
        labeled = [c for c in two if tags.get(c)]

        picked, notes = live_sim.collect_guitu_targets(fr, day)
        print(f"\n{day}")
        print(f"  涨停 {len(allzt)} | 二板 {len(two)} | 兔子 {len(rabbits)} "
              f"| 二板有标签 {len(labeled)}/{len(two)}")
        if rabbits:
            for c in rabbits:
                t = tags.get(c) or []
                print(f"    兔子 {c} {allzt[c]['name']:<8} 标签={t}")
        print(f"  选中 {len(picked)} 只: "
              + ", ".join(f"{c}({picked[c].get('guitu_theme')})" for c in picked))
        for n in notes:
            print(f"    · {n[:100]}")


if __name__ == "__main__":
    main()
