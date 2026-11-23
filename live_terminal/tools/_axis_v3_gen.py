# -*- coding: utf-8 -*-
"""生成 theme_axis v3 的补充标签（按板块分组，基于真实未收录高频标签）。

输出 JSON，供人工审阅后写入 yaml。
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import polars as pl
import yaml

ROOT = Path(__file__).resolve().parents[1]

# 关键词 → 板块 key（规则归并，覆盖长尾同义标签）
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("compute", ("算力", "液冷", "数据中心", "IDC", "服务器", "GPU", "AI芯片",
                 "光模块", "CPO", "交换机", "存储", "半导体", "芯片", "光刻",
                 "覆铜板", "印制电路", "PCB", "封测", "晶圆", "功率半导体",
                 "半导体材料", "半导体装备", "玻璃基封装", "磷化铟", "MLCC",
                 "数据库", "云计算", "边缘计算", "AI算力", "算力租赁")),
    ("ai_media", ("AI", "AIGC", "传媒", "影视", "游戏", "短剧", "漫剧", "出版",
                  "数字教育", "数字阅读", "传媒", "IP", "视频", "智能体",
                  "多模态", "大模型", "ChatGPT", "营销", "广告", "影院")),
    ("robot", ("机器人", "具身", "减速器", "灵巧手", "丝杠", "线性驱动",
               "伺服", "工业自动化", "谐波", "关节", "人形")),
    ("new_energy", ("锂电", "电池", "光伏", "储能", "风电", "氢能", "充电桩",
                    "钠电", "电解液", "正极", "负极", "隔膜", "逆变器",
                    "硅料", "组件", "钙钛矿", "固态电池", "镍氢", "圆柱锂电",
                    "铝塑膜", "锂电隔热", "新能源汽车", "汽车零部件", "汽车轻量化",
                    "汽车电子", "汽车管路", "汽车轴承", "一体压铸")),
    ("aerospace", ("军工", "航天", "卫星", "导弹", "无人机", "低空", "航空",
                   "船舶", "雷达", "军民融合", "北斗", "商业航天", "核电",
                   "燃气轮机", "民爆")),
    ("medical", ("医药", "医疗", "创新药", "CXO", "疫苗", "基因", "测序",
                 "生物", "减肥药", "GLP", "脑机", "器械", "中药", "生育",
                 "AI医疗", "医疗服务")),
    ("consumer", ("消费", "食品", "白酒", "零售", "商贸", "家电", "纺织",
                  "服装", "旅游", "酒店", "餐饮", "化妆品", "宠物", "养殖",
                  "农业", "水产", "生猪", "饲料", "休闲食品", "乳制品",
                  "电商", "跨境电商", "免税", "预制菜", "调味品")),
    ("data_elem", ("数据要素", "数据资产", "数据交易", "数交所", "信创",
                   "网络安全", "数据安全", "AI安全", "信息安全", "国产软件",
                   "操作系统", "鸿蒙")),
    ("xuanxue", ("玄学", "蛇", "龙", "马", "羊", "字辈", "生肖", "谐音")),
    ("chemical", ("化工", "染料", "农药", "化肥", "磷", "氟", "钛白", "纯碱",
                  "水泥", "玻璃", "钢铁", "煤炭", "焦炭", "有色", "黄金",
                  "稀土", "锂矿", "铜", "铝", "石油", "天然气", "橡胶",
                  "MDI", "TDI", "维生素", "添加剂", "陶瓷纤维", "制冷剂")),
    ("infra", ("基建", "地产", "建筑", "装饰", "装修", "水利", "电网",
               "特高压", "轨交", "高速公路", "管道", "PCCP", "钢结构",
               "一带一路", "房地产开发", "商办")),
]


def classify_f_a(tag: str) -> str | None:
    """返回 'F'/'A' 表示应排除；None 表示可能是题材。"""
    A = ("国资", "央企", "国企", "次新", "回购", "股权", "控制权", "重组",
         "转让", "复牌", "停牌", "变更", "选举", "换届", "中标", "合同",
         "收购", "出售", "注销", "解禁", "质押", "增持", "减持", "入股",
         "注资", "拟入主", "订单")
    F = ("三季报", "业绩", "扭亏", "减亏", "预增", "高增", "净利", "营收",
         "年报", "中报", "季报", "分红", "增长", "亏损", "超跌", "反弹", "估值")
    if any(p in tag for p in A):
        return "A"
    if any(p in tag for p in F):
        return "F"
    return None


def main() -> None:
    axis = yaml.safe_load((ROOT / "configs" / "theme_axis.yaml").read_text(encoding="utf-8"))
    existing = set()
    for a in axis["axes"]:
        existing.update(str(t) for t in (a.get("match") or []))

    df = pl.read_parquet(ROOT / "data" / "themes" / "themes.parquet")
    cnt: Counter = Counter()
    for r in df.iter_rows(named=True):
        for t in (r.get("tags") or []):
            s = str(t).strip()
            if s and s not in existing:
                cnt[s] += 1

    added: dict[str, list[str]] = defaultdict(list)
    skipped_fa: list[tuple[str, int, str]] = []
    no_rule: list[tuple[str, int]] = []
    for tag, n in cnt.most_common():
        kind = classify_f_a(tag)
        if kind:
            skipped_fa.append((tag, n, kind))
            continue
        hit = None
        for key, pats in RULES:
            if any(p.upper() in tag.upper() for p in pats):
                hit = key
                break
        if hit:
            added[hit].append(tag)
        else:
            no_rule.append((tag, n))

    print("=" * 80)
    print(f"已有标签 {len(existing)} 个；未收录 {len(cnt)} 个")
    print(f"规则可归并 → {sum(len(v) for v in added.values())} 个标签")
    print(f"排除（业绩/属性）→ {len(skipped_fa)} 个")
    print(f"规则未命中 → {len(no_rule)} 个")
    print("=" * 80)
    for k, v in added.items():
        print(f"\n[{k}] 新增 {len(v)} 个：")
        print("  " + " / ".join(v[:40]))

    print("\n" + "=" * 80)
    print("仍未命中规则的高频标签 Top 30（需人工判断）：")
    for t, n in no_rule[:30]:
        print(f"  {t:<22} {n:>3}")

    out = ROOT / "configs" / "_theme_axis_v3_add.json"
    out.write_text(json.dumps({"added": dict(added),
                               "skipped_fa": skipped_fa,
                               "no_rule": no_rule},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出：{out}")


if __name__ == "__main__":
    main()
