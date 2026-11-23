# -*- coding: utf-8 -*-
"""补齐各板块的核心原始标签（轴名本身/高频原文），并验证。"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
AXIS = ROOT / "configs" / "theme_axis.yaml"

# 每个板块的"锚标签"：轴名本身 + 真实数据里最高频的原始标签
CORE: dict[str, list[str]] = {
    "compute": ["算力", "AI算力", "算力租赁", "智算业务", "智算中心"],
    "ai_media": ["AI传媒", "AI应用", "人工智能", "AIGC应用", "AI应用"],
    "robot": ["机器人概念", "机器人应用", "人形机器人概念"],
    "new_energy": ["新能源", "锂电", "光伏概念", "风电概念", "储能概念"],
    "aerospace": ["军工", "航天", "航空", "国防军工", "核聚变"],
    "medical": ["医药", "医疗", "创新药", "生物医药"],
    "consumer": ["消费", "大消费", "食品饮料", "零售", "电商"],
    "data_elem": ["数据要素", "信创", "数据资产"],
    "chemical": ["化工", "有色金属", "黄金概念", "小金属"],
    "infra": ["基建", "房地产", "建筑", "水电", "电网"],
    "xuanxue": ["玄学", "蛇年", "马年"],
}


def main() -> None:
    d = yaml.safe_load(AXIS.read_text(encoding="utf-8"))
    by_key = {a["key"]: a for a in d["axes"]}
    before = sum(len(a.get("match") or []) for a in d["axes"])
    added = 0
    for k, tags in CORE.items():
        a = by_key.get(k)
        if not a:
            print(f"⚠ {k} 不存在")
            continue
        cur = [str(x) for x in (a.get("match") or [])]
        cur_set = set(cur)
        for t in tags:
            if t not in cur_set:
                cur.append(t)
                cur_set.add(t)
                added += 1
        a["match"] = cur
    after = sum(len(a.get("match") or []) for a in d["axes"])
    d["version"] = 3
    d.setdefault("changelog", []).append({
        "v3b": (f"补各板块锚标签（轴名/高频原文）{added} 个，标签总数 {before}→{after}。"
                "修复：v3 扩轴时漏了板块名本身（如 ai_media 未含「AI传媒」原文），"
                "导致最核心的原始标签查不到 key。")
    })
    AXIS.write_text(yaml.dump(d, allow_unicode=True, sort_keys=False,
                              default_flow_style=False, width=200),
                    encoding="utf-8")
    print(f"补 {added} 个，总标签 {before} → {after}")

    # 立即验证
    import importlib
    sys.path.insert(0, str(ROOT))
    from backend import theme_match as TM
    idx = TM._index()
    print("\n验证核心标签：")
    for t in ("AI传媒", "算力", "人工智能", "机器人概念", "数据要素"):
        print(f"  {t} -> {idx.tag_to_key.get(t)}")


if __name__ == "__main__":
    main()
