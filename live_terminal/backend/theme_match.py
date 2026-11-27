"""多标签题材匹配层 · B1「题材内一字兔 → 买同题材其他二板」的匹配核心

为什么需要这一层（与单板块硬归并的区别）
==========================================
`live_sim.collect_guitu_targets` 原先对每只票只留**一个板块**（`theme_axis.yaml`
的 9 个 key），然后按板块分组。实测（2026-11-20 落盘数据）这里丢了三类信息：

1. **每只票实际有 3~4 个原始标签**（平均 3.0 个），例如
   真视通 = `液冷算力 / 华为算力 / 多媒体视讯`。
   硬归并成 `算力` 一个 key 之后，「它还能和谁同框」这件事被永久抹掉了。
2. **轴只收录 165 个标签**，而 32 个交易日出现过 1,831 个唯一标签 →
   长尾标签必然漏。漏掉的票（如 11-18 的摩恩电气 `电磁线/特种电缆`、
   西陇科学 `光刻胶配套试剂/电子化学品`）在单板块口径里连「题材」都没有，
   连锚点都当不上。
3. **单板块分组会把「同题材」判断降级成「同 key」**，而两只票本来可以
   通过**任意一个**共享标签同框（多标签交集）。

所以本模块只做一件事：**给定两只票的原始标签（+行业），判定它们是不是同题材，
并明确标注这个判定的强度等级**。它不选股、不写文件、不读行情，纯函数。

三级降级（level）—— 调用方自己决定用不用低级别
================================================
    axis      两票命中的**板块 key 有交集** → 最严格，建议默认采用
    tag       板块无交集，但**原始标签原文完全相同** → 长尾兜底
    industry  前两级都没有，但**行业(industry)相同** → 仅行业相同，非严格题材
    None      三者皆无 → 判定为不同题材（matched=False）

「不要硬凑」的具体落点（三处，都可被审计）：

* `tag` 级只认 **T 类**标签。`三季报增长` / `央企` / `回购` 这类业绩·属性标签
  原文相同**不算同题材** —— 否则「今天都发了三季报」就能凑成一组，那是硬凑。
  （判据复用 `theme_pool.classify_tag`，单一事实来源，不在这里重写正则。）
* `industry` 级在 `reason` 里明写「仅行业相同，非严格题材」，且 `level='industry'`，
  调用方想只信严格口径时直接过滤 `level == 'axis'` 即可。
* `group_by_theme` **不把无标签票塞进任何组**（哪怕它有行业）：行业是分类归属，
  不是题材（`leader_casebook.py:209` 的原话），塞进去就是造伪。

真实覆盖率（可复算，勿手抄）
============================
`data/themes/themes.parquet`，32 个交易日，2026-11-20 收盘口径实测：

    口径                                   有标签    能映射到轴(axis)
    ----------------------------------------------------------------
    最近一天 2026-11-20 涨停 28 只           100.0%    71.4%
    全部 zt 行 1,738 行                      99.7%     56.5%
    轴 v2 设计窗口 10-14~11-16（1,384 行）   100.0%    59.7%  ← 旧口径「59%」
    全部 zt 行里**命中 ≥2 个板块**的比        —        12.7%（221 行）
    唯一票（940 只）                          —        53.1%

也就是说：**轴覆盖率的天花板是「标签有没有被收录」，不是匹配算法**。
多标签匹配不会凭空造出轴标签，但它把「已命中≥2 板块的 12.7%」从单 key
的强制二选一里解放出来；剩下的缺口只能靠补 yaml（见 `__main__` 打印的
`unmapped` 清单）。这是本层能力的边界，写在这里免得被当成 bug。

纯函数约定（可单测、可并发）
============================
* 只读 `configs/theme_axis.yaml`（路径口径复用 `theme_pool.axis_path()`）；
* **不 import `live_sim`**（`live_sim` 未来会反过来 import 本模块，避免循环依赖）；
* 不写任何文件、不联网、不读 parquet（`covarage` 的输入由调用方给）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from backend import theme_pool

# ---------------------------------------------------------------------------
# 常量（集中可审计）
# ---------------------------------------------------------------------------

AXIS_FILE = "theme_axis.yaml"

#: 三级 level 的字面量。用常量而不是散落的字符串，调用方可以 `tm.AXIS_LEVEL`。
AXIS_LEVEL = "axis"
TAG_LEVEL = "tag"
INDUSTRY_LEVEL = "industry"

#: level 优先级排序（axis > tag > industry）。数越小越严格。
LEVEL_RANK: dict[str, int] = {AXIS_LEVEL: 0, TAG_LEVEL: 1, INDUSTRY_LEVEL: 2}

#: industry 级必须写进 reason 的免责声明（要求：不硬凑，必须自证是弱匹配）。
INDUSTRY_CAVEAT = "仅行业相同，非严格题材"

#: 允许作为「题材标签」的 T/F/A 分类（只认 T）。
THEME_TAG_CLASS = "T"

#: 百分比保留位数（覆盖率先给人看，1 位小数够用且不会显得过度精确）。
PCT_DIGITS = 1

#: 轴缓存：{cache_key: (version, axes)}。命中 key 含 mtime+size，
#: 所以「yaml 被改过」会自动失效，不会拿旧映射骗人。
_AXIS_CACHE: dict[str, tuple[int, list[dict[str, Any]]]] = {}

#: 占位/无意义标签（同花顺常给未归类涨停股标「其它」，绝不当有效题材）
_PLACEHOLDER_TAGS: frozenset[str] = frozenset({"其它", "其他", "暂无", "未知", "无"})

#: 高精度语义词根 → 板块 key（解决 exact-only 匹配漏掉「按摩机器人」「阿尔茨海默病」「数据中心EPC」等变体）
#: 仅收录 ≥2 字且语义高度专一的领域词根，按先匹配优先。
_SEMANTIC_ROOTS: tuple[tuple[str, str], ...] = (
    # 机器人
    ("机器人", "robot"), ("具身智能", "robot"), ("灵巧手", "robot"), ("丝杠", "robot"),
    ("减速器", "robot"), ("伺服电机", "robot"), ("伺服系统", "robot"), ("关节模组", "robot"),
    ("电子皮肤", "robot"),
    # 医药（放在 AI 之前，确保「AI医疗」归入医药而非纯传媒）
    ("医药", "medical"), ("医疗", "medical"), ("创新药", "medical"), ("原料药", "medical"),
    ("中成药", "medical"), ("中药", "medical"), ("流感", "medical"), ("疫苗", "medical"),
    ("减肥药", "medical"), ("阿尔茨海默", "medical"), ("布洛芬", "medical"), ("儿童药", "medical"),
    ("解热镇痛", "medical"), ("体外诊断", "medical"), ("超声诊断", "medical"), ("脑机接口", "medical"),
    ("合成生物", "medical"), ("基因检测", "medical"), ("干细胞", "medical"), ("血制品", "medical"),
    ("GLP-1", "medical"), ("CXO", "medical"), ("CRO", "medical"),
    # 算力 / 半导体 / 前沿通信
    ("算力", "compute"), ("数据中心", "compute"), ("东数西算", "compute"), ("液冷", "compute"),
    ("光模块", "compute"), ("光通信", "compute"), ("光刻", "compute"), ("半导体", "compute"),
    ("存储芯片", "compute"), ("AI芯片", "compute"), ("GPU", "compute"), ("芯片", "compute"),
    ("服务器", "compute"), ("覆铜板", "compute"), ("电路板", "compute"), ("PCB", "compute"),
    ("CPO", "compute"), ("封测", "compute"), ("晶圆", "compute"), ("铜缆", "compute"),
    ("云计算", "compute"), ("智算", "compute"), ("交换机", "compute"), ("量子", "compute"),
    ("超导", "compute"), ("6G", "compute"), ("碳化硅", "compute"), ("氮化镓", "compute"),
    ("华为昇腾", "compute"), ("华为海思", "compute"), ("电子化学品", "compute"),
    ("抛光液", "compute"), ("石英晶体", "compute"),
    # AI传媒 / 内容应用
    ("短剧", "ai_media"), ("漫剧", "ai_media"), ("影视", "ai_media"), ("院线", "ai_media"),
    ("影院", "ai_media"), ("游戏", "ai_media"), ("手游", "ai_media"), ("传媒", "ai_media"),
    ("出版", "ai_media"), ("AIGC", "ai_media"), ("大模型", "ai_media"), ("智能体", "ai_media"),
    ("多模态", "ai_media"), ("AI应用", "ai_media"), ("AI视频", "ai_media"), ("AI营销", "ai_media"),
    ("AI玩具", "ai_media"), ("IP运营", "ai_media"),
    # 新能源
    ("固态电池", "new_energy"), ("固态电解质", "new_energy"), ("锂电", "new_energy"),
    ("碳酸锂", "new_energy"), ("碳汇", "new_energy"),
    ("磷酸铁", "new_energy"), ("钠电", "new_energy"), ("钠离子", "new_energy"),
    ("储能", "new_energy"), ("光伏", "new_energy"), ("钙钛矿", "new_energy"),
    ("风电", "new_energy"), ("氢能", "new_energy"), ("充电桩", "new_energy"),
    ("清洁能源", "new_energy"), ("新能源", "new_energy"), ("硅碳负极", "new_energy"),
    # 军工航天
    ("商业航天", "aerospace"), ("卫星", "aerospace"), ("航天", "aerospace"), ("低空", "aerospace"),
    ("飞行汽车", "aerospace"), ("无人机", "aerospace"), ("军工", "aerospace"), ("大飞机", "aerospace"),
    ("航空", "aerospace"), ("核聚变", "aerospace"), ("核电", "aerospace"), ("燃气轮机", "aerospace"),
    ("北斗", "aerospace"), ("民爆", "aerospace"),
    # 智能驾驶 / 汽车
    ("智能驾驶", "auto_drive"), ("自动驾驶", "auto_drive"), ("无人驾驶", "auto_drive"),
    ("车路云", "auto_drive"), ("车联网", "auto_drive"), ("智能座舱", "auto_drive"),
    ("一体压铸", "auto_drive"), ("一体化压铸", "auto_drive"), ("线控底盘", "auto_drive"),
    ("激光雷达", "auto_drive"), ("鸿蒙智行", "auto_drive"), ("华为汽车", "auto_drive"),
    ("小米汽车", "auto_drive"), ("汽车零部件", "auto_drive"), ("汽车内饰", "auto_drive"),
    ("汽车电子", "auto_drive"), ("汽车制动", "auto_drive"), ("汽车底盘", "auto_drive"),
    ("Robotaxi", "auto_drive"),
    # 大金融
    ("券商", "finance"), ("互联网金融", "finance"), ("金融科技", "finance"), ("跨境支付", "finance"),
    ("数字货币", "finance"), ("数字人民币", "finance"), ("稳定币", "finance"), ("多元金融", "finance"),
    ("区块链", "finance"), ("第三方支付", "finance"), ("移动支付", "finance"), ("财税数字化", "finance"),
    # 数据要素 / 信创安全
    ("数据要素", "data_elem"), ("数据资产", "data_elem"), ("数交所", "data_elem"),
    ("网络安全", "data_elem"), ("信息安全", "data_elem"), ("数据安全", "data_elem"),
    ("鸿蒙", "data_elem"), ("信创", "data_elem"), ("国产软件", "data_elem"), ("国资云", "data_elem"),
    ("AI政务", "data_elem"), ("电子政务", "data_elem"), ("eSIM", "data_elem"),
    # 消费
    ("免税", "consumer"), ("百货", "consumer"), ("零售", "consumer"), ("电商", "consumer"),
    ("白酒", "consumer"), ("乳制", "consumer"), ("乳品", "consumer"), ("预制菜", "consumer"),
    ("食品", "consumer"), ("餐饮", "consumer"), ("旅游", "consumer"), ("酒店", "consumer"),
    ("饲料", "consumer"), ("养殖", "consumer"), ("生猪", "consumer"), ("水产", "consumer"),
    ("远洋捕捞", "consumer"), ("渔业", "consumer"), ("粮油", "consumer"), ("口腔护理", "consumer"),
    ("种业", "consumer"), ("家电", "consumer"), ("宠物", "consumer"), ("医美", "consumer"),
    ("消费电子", "consumer"),
    # 化工周期 / 资源
    ("钛白粉", "chemical"), ("磷化工", "chemical"), ("氟化工", "chemical"), ("氯碱", "chemical"),
    ("煤化工", "chemical"), ("甲醇", "chemical"), ("染料", "chemical"), ("农药", "chemical"),
    ("化肥", "chemical"), ("制冷剂", "chemical"), ("碳纤维", "chemical"), ("有机硅", "chemical"),
    ("聚氨酯", "chemical"), ("纯碱", "chemical"), ("烧碱", "chemical"), ("稀土", "chemical"),
    ("锂矿", "chemical"), ("铜箔", "chemical"), ("黄金", "chemical"), ("白银", "chemical"),
    ("贵金属", "chemical"), ("小金属", "chemical"), ("有色金属", "chemical"), ("油气", "chemical"),
    ("页岩气", "chemical"), ("石油", "chemical"), ("天然气", "chemical"), ("煤炭", "chemical"),
    ("钢铁", "chemical"), ("橡胶", "chemical"), ("化工", "chemical"), ("造纸", "chemical"), ("建材", "chemical"),
    # 基建地产 / 电力航运 / 深海
    ("特高压", "infra"), ("电网", "infra"), ("虚拟电厂", "infra"), ("电力", "infra"),
    ("水电", "infra"), ("水利", "infra"), ("基建", "infra"), ("房地产", "infra"),
    ("建筑", "infra"), ("装修装饰", "infra"), ("钢结构", "infra"), ("管道", "infra"),
    ("管网", "infra"), ("一带一路", "infra"), ("轨交", "infra"), ("深海", "infra"),
    ("海洋经济", "infra"), ("航运", "infra"), ("造船", "infra"), ("城市更新", "infra"),
    # 玄学
    ("字辈", "xuanxue"), ("东方系", "xuanxue"), ("谐音", "xuanxue"),
)

#: 泛化二字前缀停用表（防止新兴概念前缀聚类把「智能XX」「中国XX」硬凑成一组）
_STEM_STOP_PREFIXES: frozenset[str] = frozenset({
    "中国", "中华", "国产", "国内", "国际", "全球", "海外", "国家", "地方",
    "智能", "智慧", "数字", "现代", "新型", "新质", "高端", "先进", "精密",
    "特种", "核心", "战略", "综合", "通用", "专用", "大型", "小型", "微型",
    "绿色", "低碳", "环保", "生态", "节能", "高效", "高速", "超导", "高纯",
    "工业", "商业", "行业", "产业", "企业", "集团", "科技", "技术", "设备",
    "材料", "产品", "服务", "业务", "项目", "概念", "主题", "相关", "主要",
    "两岸", "海峡", "区域", "西部", "东部", "南部", "北部", "省级", "市级",
    "拟收", "拟投", "重大", "股权", "资产", "股份", "撤销", "恢复", "预计",
})

#: A 股关联产业链邻接表（中文板块名 → 上下游/共振关联板块，供 B2 影子挑战者识别「跨细分子板块共振」）
RELATED_THEME_CHAINS: dict[str, tuple[str, ...]] = {
    "算力": ("AI传媒", "数据要素", "机器人"),
    "AI传媒": ("算力", "数据要素", "消费"),
    "数据要素": ("算力", "AI传媒", "大金融"),
    "机器人": ("智能驾驶", "算力", "军工航天"),
    "智能驾驶": ("机器人", "新能源", "算力"),
    "新能源": ("化工周期", "智能驾驶", "基建地产"),
    "化工周期": ("新能源", "基建地产"),
    "军工航天": ("算力", "机器人"),
    "医药": ("消费",),
    "消费": ("医药", "AI传媒"),
    "大金融": ("数据要素", "基建地产"),
    "基建地产": ("化工周期", "新能源", "大金融"),
}


def related_themes(theme: str) -> tuple[str, ...]:
    """返回某板块的上下游/大主线关联板块（纯只读查询）。"""
    return RELATED_THEME_CHAINS.get(str(theme or "").strip(), ())

#: 同票共现学习缓存与历史个股题材记忆缓存（按 themes.parquet mtime+size 自动失效）
_COOC_CACHE: dict[str, dict[str, dict[str, Any]]] = {}
_HIST_TAGS_CACHE: dict[str, dict[str, list[str]]] = {}


# ---------------------------------------------------------------------------
# 轴的读取（缓存 + 失效）
# ---------------------------------------------------------------------------

def axis_path() -> Path:
    """题材轴 yaml 路径。

    刻意复用 `theme_pool.axis_path()` 而不是在这里再拼一次路径：
    路径规则只允许有一个出处，否则 `WUKONG_DATA_HOME` 一改就会出现
    「两个模块读两个文件」。配置跟随代码（不是数据），
    由 `paths.configs_dir()` 解析。
    """
    return theme_pool.axis_path()


def clear_cache() -> None:
    """清空轴/共现/历史记忆缓存。测试改 yaml / monkeypatch 路径后必须调，生产不用。"""
    _AXIS_CACHE.clear()
    _COOC_CACHE.clear()
    _HIST_TAGS_CACHE.clear()


def load_axis() -> dict:
    """读 `configs/theme_axis.yaml`，返回 {'version':..., 'axes':[{key,name,match}]}。

    与 `theme_pool.load_axis()` 的区别：那边返回的是 **tag → {key,name}** 的扁平
    字典（给单标签选主题材用），本模块要的是**板块列表**（多标签展开、按板块遍历），
    所以在这里把 yaml 读成列表形态。数据只有一份，形状按用途各取所需。

    降级约定（**不抛异常、不猜**）：文件缺失或 yaml 解析失败 →
    返回 `{'version': 0, 'axes': []}`。调用方看到空 axes 必须知道自己已降级
    （`coverage_stats` 的 `axis_pct` 会是 0，一眼能看出来），
    **绝不退回「按原始标签当板块」冒充严格匹配**。

    >>> ax = load_axis()
    >>> ax["version"] >= 2
    True
    >>> [a["key"] for a in ax["axes"]][:2]
    ['robot', 'ai_media']
    """
    p = axis_path()
    try:
        st = p.stat()
        key = f"{p}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        return {"version": 0, "axes": []}

    hit = _AXIS_CACHE.get(key)
    if hit is not None:
        version, axes = hit
        # 返回浅拷贝，防止调用方改到缓存里的 dict（纯函数不许有外部副作用）
        return {"version": version, "axes": [dict(a) for a in axes]}

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {"version": 0, "axes": []}

    axes: list[dict[str, Any]] = []
    for a in raw.get("axes") or []:
        if not isinstance(a, dict) or not a.get("key"):
            continue
        match = [str(t).strip() for t in (a.get("match") or [])
                 if str(t).strip()]
        axes.append({
            "key": str(a["key"]),
            "name": str(a.get("name") or a["key"]),
            "match": match,
        })
    version = int(raw.get("version") or 0)
    _AXIS_CACHE[key] = (version, axes)
    return {"version": version, "axes": [dict(a) for a in axes]}


class _AxisIndex:
    """轴的反查表（tag → key、key → 展示名、key 的声明顺序）。

    单独抽出来是因为 `map_to_axes` 会被 `group_by_theme` / `coverage_stats`
    按「每只票一次」地调用；每次都重建 165 条映射是纯浪费。
    """

    __slots__ = ("version", "tag_to_key", "key_name", "key_order")

    def __init__(self, axis: dict) -> None:
        self.version: int = int(axis.get("version") or 0)
        self.tag_to_key: dict[str, str] = {}
        self.key_name: dict[str, str] = {}
        self.key_order: dict[str, int] = {}
        for i, a in enumerate(axis.get("axes") or []):
            k = a["key"]
            self.key_name[k] = a.get("name") or k
            self.key_order.setdefault(k, i)
            for t in a.get("match") or []:
                # 同一个标签出现在两个板块里 → 以**先声明**的板块为准（不静默二选一，
                # 这是 yaml 的书写顺序，可审计）。实测 v2 的 165 个标签无重复。
                self.tag_to_key.setdefault(t, k)


def _index() -> _AxisIndex:
    """取（带缓存的）轴反查表。"""
    return _AxisIndex(load_axis())


def is_theme_tag(tag: str) -> bool:
    """这个原始标签能不能当「题材」用（T 类），排除业绩/事件(F)与属性(A)。

    判据直接复用 `theme_pool.classify_tag`，不在这里重写正则 ——
    「三季报增长」到底是 F 还是 T，全项目只能有一个答案。

    >>> is_theme_tag("AI漫剧")
    True
    >>> is_theme_tag("三季报增长")   # F：业绩
    False
    >>> is_theme_tag("成都国资")     # A：属性
    False
    """
    return theme_pool.classify_tag(str(tag)) == THEME_TAG_CLASS


# ---------------------------------------------------------------------------
# 输入归一化
# ---------------------------------------------------------------------------

def _norm_tags(tags: Iterable[Any] | None) -> list[str]:
    """标签列表归一：转字符串、去空白、**保序去重**、丢掉空值。

    去重是必要的：同一只票在不同池/不同帧里标签可能重复，
    不去重会让 `shared` 里出现两个一样的标签，也会让覆盖率分母看起来更大。
    """
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        s = "" if t is None else str(t).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _norm_industry(v: Any) -> str:
    """行业归一：None/空白 → ""（**不是** "未知"）。空行业不参与匹配。"""
    if v is None:
        return ""
    return str(v).strip()


def _norm_items(items: Sequence[dict] | None) -> list[dict[str, Any]]:
    """把调用方给的 items 归一成内部结构，并按 code 去重合并。

    合并规则（防止同一天同一只票出现两行时把它自己算成「两只票」）：
      * tags   → 并集（保序去重）
      * streak → 取最大（板位只会被低估，不会被高估）
      * name / industry → 取第一个非空

    没有 code 的项直接丢弃：没有标识就无法回到真实票，留着只会污染统计。
    """
    by_code: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip().zfill(6)
        if not code.strip("0"):
            continue
        tags = _norm_tags(it.get("tags"))
        industry = _norm_industry(it.get("industry"))
        try:
            streak = int(it.get("streak") or 0)
        except (TypeError, ValueError):
            streak = 0
        cur = by_code.get(code)
        if cur is None:
            by_code[code] = {
                "code": code,
                "name": str(it.get("name") or "").strip(),
                "tags": tags,
                "industry": industry,
                "streak": streak,
            }
            order.append(code)
            continue
        for t in tags:
            if t not in cur["tags"]:
                cur["tags"].append(t)
        cur["streak"] = max(cur["streak"], streak)
        if not cur["name"]:
            cur["name"] = str(it.get("name") or "").strip()
        if not cur["industry"]:
            cur["industry"] = industry
    return [by_code[c] for c in order]


def _pct(part: int, total: int) -> float:
    """百分比（0 分母返回 0.0，不返回 None —— 调用方不该为 0 只票写特判）。"""
    if total <= 0:
        return 0.0
    return round(part / total * 100.0, PCT_DIGITS)


def _semantic_axes_for_tag(tag: str, idx: _AxisIndex) -> list[str]:
    """高精度语义词根 + YAML 长词子串归并（绝不碰纯占位词/纯业绩属性词）。"""
    t = str(tag or "").strip()
    if not t or t in _PLACEHOLDER_TAGS:
        return []
    out: list[str] = []
    # 1) 优先匹配高精度领域词根（即使带「免税重组」这类混合后缀也能保住核心题材）
    for root, key in _SEMANTIC_ROOTS:
        if key in idx.key_name and root in t and key not in out:
            out.append(key)
    if out:
        return out
    # 2) 纯业绩/属性标签（且未命中任何领域词根）直接跳过
    if not is_theme_tag(t):
        return []
    # 3) YAML 中长度 ≥3 的明确题材词做子串归并（如「阿尔茨海默」→「阿尔茨海默病」）
    for yaml_tag, key in idx.tag_to_key.items():
        if len(yaml_tag) >= 3 and yaml_tag in t and key not in out:
            out.append(key)
    return out


def _themes_file_key(extra: str = "") -> str | None:
    p = theme_pool.themes_file()
    try:
        st = p.stat()
        return f"{p}|{st.st_mtime_ns}|{st.st_size}|{extra}"
    except OSError:
        return None


def learned_tag_mappings(*, before_day: str | None = None,
                         min_stocks: int = 2,
                         min_purity: float = 0.60) -> dict[str, dict[str, Any]]:
    """纯数据驱动「同票共现自学习归轴」：从 themes.parquet 自动学会未进 YAML 的新概念归属。

    原理：每只涨停股通常带 3~4 个标签（例如 ['中昊芯英', 'AI芯片', '算力']）。
    当某个未收录的 T 类新标签 U 在 ≥min_stocks 只不同个股上，与已知轴 K 共现纯度 ≥min_purity，
    系统自动建立 `U -> K` 映射，市场发明新概念时无需天天手改 YAML。
    """
    cut = theme_pool._norm_day(before_day) if before_day else ""
    ck = _themes_file_key(f"cooc|{cut}|{min_stocks}|{min_purity}")
    if ck is None:
        return {}
    if ck in _COOC_CACHE:
        return _COOC_CACHE[ck]

    idx = _index()
    if not idx.key_name:
        return {}
    learned: dict[str, dict[str, Any]] = {}
    try:
        import polars as pl
        from collections import defaultdict

        lf = pl.scan_parquet(theme_pool.themes_file()).select(["date", "code", "tags"])
        if cut:
            lf = lf.filter(pl.col("date") < cut)
        df = lf.collect()
        if df.height == 0:
            _COOC_CACHE[ck] = {}
            return {}

        # tag -> axis_key -> set(codes)
        tag_axis_codes: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for r in df.iter_rows(named=True):
            code = str(r.get("code") or "").zfill(6)
            tags = _norm_tags(r.get("tags"))
            if not code or len(tags) < 2:
                continue
            known_axes: set[str] = set()
            unknown_t: list[str] = []
            for t in tags:
                if t in _PLACEHOLDER_TAGS:
                    continue
                k0 = idx.tag_to_key.get(t)
                if k0:
                    known_axes.add(k0)
                    continue
                sem = _semantic_axes_for_tag(t, idx)
                if sem:
                    known_axes.update(sem)
                elif is_theme_tag(t):
                    unknown_t.append(t)
            if not known_axes or not unknown_t:
                continue
            for u in unknown_t:
                for k in known_axes:
                    tag_axis_codes[u][k].add(code)

        for u, ax_map in tag_axis_codes.items():
            counts = {k: len(cs) for k, cs in ax_map.items()}
            total = sum(counts.values())
            if total <= 0:
                continue
            top_k, top_n = max(counts.items(), key=lambda kv: kv[1])
            purity = top_n / total
            if top_n >= min_stocks and purity >= min_purity:
                learned[u] = {
                    "key": top_k,
                    "name": idx.key_name.get(top_k, top_k),
                    "stocks": top_n,
                    "purity": round(purity, 3),
                }
    except Exception:  # noqa: BLE001
        learned = {}

    _COOC_CACHE[ck] = learned
    return learned


def historical_stock_tags(before_day: str | None = None, *,
                          lookback_days: int = 60,
                          max_tags: int = 5) -> dict[str, list[str]]:
    """严格因果历史个股题材记忆库：从 themes.parquet (< before_day) 提取每只票近期的有效 T 类标签。

    用于拯救当日盘中标签滞后、缺标签或同花顺写「其它」的股票（如 11-25 中京电子 tags=['其它']）。
    """
    cut = theme_pool._norm_day(before_day) if before_day else ""
    ck = _themes_file_key(f"hist|{cut}|{lookback_days}|{max_tags}")
    if ck is None:
        return {}
    if ck in _HIST_TAGS_CACHE:
        return _HIST_TAGS_CACHE[ck]

    out: dict[str, list[str]] = {}
    try:
        import polars as pl
        lf = pl.scan_parquet(theme_pool.themes_file()).select(["date", "code", "tags"])
        if cut:
            lf = lf.filter(pl.col("date") < cut)
        df = lf.collect()
        if df.height == 0:
            _HIST_TAGS_CACHE[ck] = {}
            return {}
        days = sorted({str(d) for d in df["date"].unique().to_list()})
        if len(days) > lookback_days:
            cut_lo = days[-lookback_days]
            df = df.filter(pl.col("date") >= cut_lo)
        # 按日期降序，越近的题材越靠前
        df = df.sort("date", descending=True)
        for r in df.iter_rows(named=True):
            code = str(r.get("code") or "").zfill(6)
            if not code or code == "000000":
                continue
            cur = out.setdefault(code, [])
            if len(cur) >= max_tags:
                continue
            for t in _norm_tags(r.get("tags")):
                if t in _PLACEHOLDER_TAGS or not is_theme_tag(t):
                    continue
                if t not in cur:
                    cur.append(t)
                    if len(cur) >= max_tags:
                        break
        out = {k: v for k, v in out.items() if v}
    except Exception:  # noqa: BLE001
        out = {}

    _HIST_TAGS_CACHE[ck] = out
    return out


_NON_THEME_CONCEPTS: frozenset[str] = frozenset({
    "富时罗素", "MSCI中国", "标普概念", "深股通", "沪股通", "融资融券", "转融券标的",
    "昨日涨停", "昨日连板", "昨日涨停_含一字", "昨日连板_含一字", "中证500", "沪深300",
    "证金持股", "机构重仓", "HS300_", "深成500", "创业板综", "科创50", "百元股",
    "破净股", "低价股", "高送转", "预盈预增", "预亏预减", "ST股", "注册制次新股",
})

_CONCEPT_CACHE_MEM: dict[str, list[str]] | None = None


def _concept_cache_path() -> Path:
    from backend import paths
    p = paths.themes_dir() / "stock_concepts_cache.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_concept_cache() -> dict[str, list[str]]:
    global _CONCEPT_CACHE_MEM
    if _CONCEPT_CACHE_MEM is not None:
        return _CONCEPT_CACHE_MEM
    cp = _concept_cache_path()
    if cp.is_file():
        try:
            d = json.loads(cp.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                _CONCEPT_CACHE_MEM = {str(k).zfill(6): list(v) for k, v in d.items() if isinstance(v, list)}
                return _CONCEPT_CACHE_MEM
        except Exception:  # noqa: BLE001
            pass
    _CONCEPT_CACHE_MEM = {}
    return _CONCEPT_CACHE_MEM


def _save_concept_cache(cache: dict[str, list[str]]) -> None:
    try:
        cp = _concept_cache_path()
        tmp = cp.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cp)
    except Exception:  # noqa: BLE001
        pass


def _news_tags_for_codes(before_day: str | None = None) -> dict[str, list[str]]:
    """L2 兜底：从本地 7×24 快讯留档 data/live/<YYYYMMDD>/news.jsonl 反推近 7 日提及该股的题材标签（零外网请求）。"""
    out: dict[str, list[str]] = {}
    try:
        import json
        from backend import paths
        live_dir = paths.data_dir() / "live"
        if not live_dir.is_dir():
            return out
        d_cut = str(before_day or "").replace("-", "")[:8]
        day_dirs = sorted(
            (p for p in live_dir.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 8),
            key=lambda p: p.name,
            reverse=True,
        )
        n_read = 0
        for dp in day_dirs:
            if d_cut and dp.name > d_cut:
                continue
            nf = dp / "news.jsonl"
            if not nf.is_file():
                continue
            n_read += 1
            if n_read > 7:
                break
            for ln in nf.read_text(encoding="utf-8", errors="ignore").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    it = json.loads(ln)
                except Exception:
                    continue
                ths = it.get("themes") or []
                if not ths:
                    continue
                if it.get("sentiment") == "danger" or it.get("is_clarification_cooling"):
                    continue
                try:
                    from backend import news_feed as _nf_clr
                    txt_clr = f"{it.get('title') or ''} {it.get('digest') or ''}"
                    if _nf_clr.CLARIFICATION_COOLING_RE.search(txt_clr):
                        continue
                except Exception:  # noqa: BLE001
                    pass
                for c in (it.get("codes") or []):
                    c6 = str(c).zfill(6)
                    bag = out.setdefault(c6, [])
                    for th in ths:
                        if th and str(th) not in bag:
                            bag.append(str(th))
    except Exception:  # noqa: BLE001
        return out
    return out


def _lhb_tags_for_codes(before_day: str | None = None) -> dict[str, list[str]]:
    """L3 兜底：从本地龙虎榜缓存（严格 < before_day）提取标的关联题材/名称语义词根（零外网请求）。"""
    out: dict[str, list[str]] = {}
    try:
        import json
        from backend import paths
        ld = paths.data_dir() / "lhb"
        if not ld.is_dir():
            return out
        d_cut = str(before_day or "")[:10]
        files = sorted(ld.glob("*.json"), reverse=True)
        n_read = 0
        for fp in files:
            if d_cut and fp.stem >= d_cut:
                continue
            n_read += 1
            if n_read > 5:
                break
            d = json.loads(fp.read_text(encoding="utf-8"))
            # 1) 优先读 top_stocks / stock_profiles 中已挂载的 themes 字段
            for st in (d.get("top_stocks") or []):
                c6 = str(st.get("ts_code") or "").zfill(6)
                for th in (st.get("themes") or []):
                    if c6 and th:
                        bag = out.setdefault(c6, [])
                        if str(th) not in bag:
                            bag.append(str(th))
            # 2) 其次对 stock_profiles 的 name / themes / reason 做语义轴匹配，并用历史 themes.parquet 补齐龙虎榜标的真实题材
            hist_map: dict[str, list[str]] = {}
            try:
                hist_map = _history_tags_for_codes(before_day=fp.stem)
            except Exception:  # noqa: BLE001
                hist_map = {}
            for c6, prof in (d.get("stock_profiles") or {}).items():
                c_norm = str(c6).zfill(6)
                direct_ths = list(prof.get("themes") or [])
                if not direct_ths and c_norm in hist_map:
                    direct_ths = list(hist_map[c_norm])
                if direct_ths:
                    bag = out.setdefault(c_norm, [])
                    for th in direct_ths:
                        if th and str(th) not in bag:
                            bag.append(str(th))
                txt = " ".join(str(prof.get(k) or "") for k in ("name", "reason", "industry", "note"))
                hits = map_to_axes([txt])
                if hits:
                    bag = out.setdefault(c_norm, [])
                    for h in hits:
                        if h not in bag:
                            bag.append(h)
    except Exception:  # noqa: BLE001
        return out
    return out


def _fetch_f10_concepts(code: str, *, allow_remote: bool = False, timeout: float = 3.0) -> list[str]:
    """L4 兜底：东财 F10 核心所属概念快查（本地永久缓存，查一次后永远 0ms 离线命中）。"""
    c6 = str(code).zfill(6)
    cache = _load_concept_cache()
    if c6 in cache:
        return list(cache[c6])
    if not allow_remote:
        return []
    try:
        import urllib.parse
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        p = urllib.parse.urlencode({
            "reportName": "RPT_F10_CORETHEME_BOARDTYPE",
            "columns": "SECURITY_CODE,BOARD_NAME",
            "filter": f'(SECURITY_CODE="{c6}")',
            "pageSize": "15",
            "pageNumber": "1",
        })
        url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?{p}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(opener.open(req, timeout=timeout).read().decode("utf-8"))
        rows = (d.get("result") or {}).get("data") or []
        got: list[str] = []
        for r in rows:
            bn = str(r.get("BOARD_NAME") or "").strip()
            if not bn or bn in _NON_THEME_CONCEPTS or bn in _PLACEHOLDER_TAGS:
                continue
            if bn not in got:
                got.append(bn)
            if len(got) >= 3:
                break
        cache[c6] = got
        _save_concept_cache(cache)
        return got
    except Exception:  # noqa: BLE001
        return []


def enrich_stock_tags(code_tags: dict[str, list[str]], *,
                      before_day: str | None = None,
                      codes: Iterable[str] | None = None,
                      allow_remote: bool = False,
                      max_remote: int = 15) -> dict[str, list[str]]:
    """四级极速多源补全缺标签个股：L1 历史题材记忆 → L2 本地 7×24 快讯 → L3 本地龙虎榜 → L4 东财核心概念缓存。"""
    out: dict[str, list[str]] = {}
    all_codes: set[str] = set(str(c).zfill(6) for c in (code_tags or {}).keys())
    if codes is not None:
        all_codes.update(str(c).zfill(6) for c in codes if c)

    hist: dict[str, list[str]] | None = None
    news_m: dict[str, list[str]] | None = None
    lhb_m: dict[str, list[str]] | None = None
    remote_used = 0

    # 统计当日原生池（code_tags）中已出现的活跃板块与题材词，防止 L4 F10 静态概念把多年前的「僵尸概念」强行配对
    active_axes: set[str] = set()
    active_tags: set[str] = set()
    native_tagged_n = 0
    for _c_raw, _tlist in (code_tags or {}).items():
        _clean = [t for t in _norm_tags(_tlist) if t not in _PLACEHOLDER_TAGS]
        _axes = map_to_axes(_clean)
        _ttags = [t for t in _clean if is_theme_tag(t)]
        if _axes or _ttags:
            native_tagged_n += 1
            active_axes.update(_axes)
            active_tags.update(_ttags)

    for c in sorted(all_codes):
        raw = [t for t in _norm_tags((code_tags or {}).get(c)) if t not in _PLACEHOLDER_TAGS]
        has_valid_theme = any(is_theme_tag(t) or bool(map_to_axes([t])) for t in raw)

        # L1: 历史题材记忆（themes.parquet 近 60 日，严格 < before_day）
        if not has_valid_theme:
            if hist is None:
                hist = historical_stock_tags(before_day=before_day)
            mem = hist.get(c) or []
            if mem:
                for mt in mem:
                    if mt not in raw:
                        raw.append(mt)
                has_valid_theme = any(is_theme_tag(t) or bool(map_to_axes([t])) for t in raw)

        # L2: 本地 7×24 快讯提及题材
        if not has_valid_theme:
            if news_m is None:
                news_m = _news_tags_for_codes(before_day=before_day)
            for nt in (news_m.get(c) or []):
                if nt not in raw:
                    raw.append(nt)
            has_valid_theme = any(is_theme_tag(t) or bool(map_to_axes([t])) for t in raw)

        # L3: 本地龙虎榜关联题材（严格 < before_day）
        if not has_valid_theme:
            if lhb_m is None:
                lhb_m = _lhb_tags_for_codes(before_day=before_day)
            for lt in (lhb_m.get(c) or []):
                if lt not in raw:
                    raw.append(lt)
            has_valid_theme = any(is_theme_tag(t) or bool(map_to_axes([t])) for t in raw)

        # L4: 东财 F10 核心概念快查（优先读本地永久缓存；allow_remote=True 时在线补查并落盘）
        # 防僵尸概念污染：若当日原生池已有 ≥2 只带标个股，F10 概念必须与当日活跃板块/标签有交集才采纳
        if not has_valid_theme:
            can_net = bool(allow_remote and remote_used < max_remote)
            was_cached = c in _load_concept_cache()
            f10 = _fetch_f10_concepts(c, allow_remote=can_net)
            if can_net and not was_cached:
                remote_used += 1
            for ft in f10:
                if native_tagged_n >= 2:
                    ft_axes = set(map_to_axes([ft]))
                    if ft not in active_tags and not (ft_axes & active_axes):
                        continue
                if ft not in raw:
                    raw.append(ft)

        if raw:
            out[c] = raw
    return out


# ---------------------------------------------------------------------------
# 核心 1：单票原始标签 → 板块 key（精确 → 语义词根 → 共现自学习）
# ---------------------------------------------------------------------------

def map_to_axes(tags: list[str], *,
                use_semantic: bool = True,
                use_cooccurrence: bool = True) -> list[str]:
    """原始标签 → 命中的板块 key 列表（可能多个，保序去重）。

    三层自适应归并（不再局限于静态 exact-only 字符串匹配）：
      1. 精确命中 `configs/theme_axis.yaml`；
      2. 高精度语义词根 / YAML 长度≥3 题材词子串匹配（自动覆盖「按摩机器人」「数据中心EPC」「阿尔茨海默病」等变体）；
      3. 同票共现自学习映射（自动吸收市场新发明且与主轴高频共现的概念标签）。
    """
    idx = _index()
    cooc: dict[str, dict[str, Any]] | None = None
    out: list[str] = []
    for t in _norm_tags(tags):
        if t in _PLACEHOLDER_TAGS:
            continue
        k = idx.tag_to_key.get(t)
        if k:
            if k not in out:
                out.append(k)
            continue
        if use_semantic:
            sem_keys = _semantic_axes_for_tag(t, idx)
            if sem_keys:
                for sk in sem_keys:
                    if sk not in out:
                        out.append(sk)
                continue
        if use_cooccurrence and is_theme_tag(t):
            if cooc is None:
                cooc = learned_tag_mappings()
            hit = cooc.get(t)
            if hit and hit.get("key") in idx.key_name:
                ck = str(hit["key"])
                if ck not in out:
                    out.append(ck)
    return out


# ---------------------------------------------------------------------------
# 二级细分簇与主营纯度校验（防 13 个粗粒度大筐强行拉郎配）
# ---------------------------------------------------------------------------

_SUB_CLUSTERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # compute 细分（严禁放入 "算力" / "GPU" / "化工" 等一级母筐泛词）
    ("compute", "compute_huawei_ai", ("华为昇腾", "华为海思", "华为算力", "国产算力", "AI算力", "AI芯片", "寒武纪", "国产GPU", "AI服务器", "智算", "算力基础设施", "算力中心", "算力租赁", "云计算", "东数西算")),
    ("compute", "compute_optics_pcb", ("光模块", "光模块检测", "CPO", "光通信", "液冷", "铜缆", "PCB", "高多层HDI", "HDI", "覆铜板", "印制电路板", "交换机", "AI服务器电源", "服务器电源", "AI电源")),
    ("compute", "compute_semi", ("半导体", "光刻机", "光刻胶", "晶圆", "封测", "存储芯片", "存储", "先进封装", "碳化硅", "氮化镓")),
    ("compute", "compute_frontier", ("量子", "6G", "超导", "太空算力")),
    # robot 细分
    ("robot", "robot_core", ("人形机器人", "具身智能", "工业机器人", "灵巧手", "丝杠", "减速器", "关节模组", "伺服", "电子皮肤", "机器视觉", "特斯拉机器人")),
    ("robot", "robot_appliance", ("扫地机器人", "家庭机器人", "小家电", "家电")),
    # consumer 细分
    ("consumer", "consumer_retail", ("免税", "百货", "零售", "电商", "旅游", "酒店", "商业")),
    ("consumer", "consumer_food", ("白酒", "啤酒", "食品", "乳品", "乳制", "预制菜", "餐饮", "调味品")),
    ("consumer", "consumer_agri", ("生猪", "猪肉", "养殖", "饲料", "种业", "粮食", "转基因", "水产")),
    ("consumer", "consumer_appliance", ("小家电", "家电", "消费电子")),
    # chemical 细分
    ("chemical", "chemical_gold_metal", ("黄金", "白银", "贵金属", "铜矿", "铝业", "小金属", "有色", "稀土", "锂矿")),
    ("chemical", "chemical_chem", ("染料", "农药", "化肥", "磷化工", "氟化工", "氯碱", "纯碱", "烧碱", "联碱", "钛白粉", "甲醇", "碳纤维", "基础化工")),
    ("chemical", "chemical_energy", ("煤炭", "焦炭", "石油", "油气", "天然气", "页岩气")),
    # infra 细分
    ("infra", "infra_grid_power", ("特高压", "电网", "虚拟电厂", "电力", "水电", "绿色电力")),
    ("infra", "infra_decor_build", ("装修", "建筑装饰", "室内设计", "建筑设计", "基建", "房地产", "钢结构")),
    ("infra", "infra_marine", ("深海", "海洋经济", "航运", "港口", "造船")),
    # new_energy 细分
    ("new_energy", "ne_battery", ("固态电池", "锂电", "钠离子", "钠电池", "储能", "磷酸铁锂", "电解液")),
    ("new_energy", "ne_pv_wind", ("光伏", "风电", "钙钛矿", "硅料", "氢能")),
    # aerospace 细分
    ("aerospace", "aero_space", ("商业航天", "卫星", "航天", "低空", "飞行汽车", "无人机")),
    ("aerospace", "aero_defense", ("军工", "航空发动机", "核电", "核聚变", "民爆")),
    # medical 细分
    ("medical", "med_pharma", ("创新药", "中药", "中成药", "化学制药", "减肥药", "流感", "疫苗")),
    ("medical", "med_device_bio", ("医疗器械", "体外诊断", "脑机接口", "基因", "干细胞", "CRO", "CXO")),
)

# 算力核心硬科技协同链（国产芯片 ↔ 先进封装/半导体 ↔ 光模块/PCB/服务器电源/液冷）
# 前沿/边缘概念（compute_frontier 等）不在此列，仍与核心算力隔离。
_SUB_CLUSTER_COMPATIBLE: dict[str, frozenset[str]] = {
    "compute_huawei_ai": frozenset({"compute_huawei_ai", "compute_optics_pcb", "compute_semi"}),
    "compute_optics_pcb": frozenset({"compute_huawei_ai", "compute_optics_pcb", "compute_semi"}),
    "compute_semi": frozenset({"compute_huawei_ai", "compute_optics_pcb", "compute_semi"}),
}


def _subclusters_match(r_subs: Sequence[str], t_subs: Sequence[str]) -> list[str]:
    """返回直接共享或属于核心硬件协同链的细分簇匹配列表。"""
    exact = [s for s in r_subs if s in t_subs]
    if exact:
        return exact
    compat: list[str] = []
    for rs in r_subs:
        allowed = _SUB_CLUSTER_COMPATIBLE.get(rs, ())
        for ts in t_subs:
            if ts in allowed:
                pair = f"{rs}↔{ts}"
                if pair not in compat:
                    compat.append(pair)
    return compat


# 与高科技主线明显风马牛不相及的传统行业/边缘业务词（硬一票否决，绝不允许靠宽泛概念词绕过）
_MISMATCH_INDUSTRY_HINTS: dict[str, tuple[str, ...]] = {
    "算力": ("装修", "装饰", "家装", "建筑", "包装", "纸箱", "纸包装", "包装印刷", "锯条", "锯切", "食品", "白酒", "养殖", "纺织", "服装", "地产"),
    "机器人": ("豆浆机", "小家电", "厨房", "食品", "饮料", "白酒", "装修", "纺织", "养殖", "地产", "包装", "纸箱", "包装印刷"),
    "新能源": ("包装", "纸箱", "纸包装", "包装印刷", "装修", "装饰", "食品", "白酒", "纺织", "服装"),
    "AI传媒": ("装修", "包装", "钢铁", "煤炭", "养殖", "化工"),
}


def map_to_subclusters(tags: Sequence[str], *, axis_key: str | None = None) -> list[str]:
    """将标签映射至二级细分簇（保留原始序并去重）。"""
    out: list[str] = []
    for t in _norm_tags(tags):
        if t in _PLACEHOLDER_TAGS:
            continue
        for ax, sub_k, roots in _SUB_CLUSTERS:
            if axis_key and ax != axis_key:
                continue
            if any(rt in t for rt in roots):
                if sub_k not in out:
                    out.append(sub_k)
    return out


def check_pair_purity(
    rabbit_tags: Sequence[str],
    turtle_tags: Sequence[str],
    *,
    turtle_name: str = "",
    turtle_industry: str | None = None,
    axis_name: str | None = None,
    top_n_core: int = 4,
) -> dict[str, Any]:
    """检验龟兔配对的二级细分簇一致性与主营纯度，拦截跨细分簇和蹭边缘概念杂毛。"""
    rt = _norm_tags(rabbit_tags)
    tt = _norm_tags(turtle_tags)
    if not rt or not tt:
        return {"pure": True, "reason": "缺标签时保持中性放行", "shared_subclusters": []}

    idx = _index()
    ax_key = None
    if axis_name:
        for k, nm in idx.key_name.items():
            if nm == axis_name or k == axis_name:
                ax_key = k
                break

    # 1) 传统跨界行业硬一票否决（防装修/纸箱/锯条/小家电借泛词标签绕过黑名单）
    core_tt = tt[:max(1, int(top_n_core))]
    bad_hints = _MISMATCH_INDUSTRY_HINTS.get(str(axis_name or ""), ())
    r_subs = map_to_subclusters(rt, axis_key=ax_key)
    t_core_subs = map_to_subclusters(core_tt, axis_key=ax_key)
    t_all_subs = map_to_subclusters(tt, axis_key=ax_key)

    if bad_hints and any(bh in (turtle_industry or "") or bh in " ".join(core_tt) for bh in bad_hints):
        return {
            "pure": False,
            "reason": f"主营行业({turtle_industry or core_tt[0]})与「{axis_name}」兔子主线不符（防跨界蹭概念）",
            "shared_subclusters": [],
        }

    # 2) 二级细分簇校验（同轴硬件链 compute_huawei_ai ↔ compute_optics_pcb ↔ compute_semi 允许协同，前沿边缘与异簇隔离）
    if r_subs and t_all_subs:
        matched_all = _subclusters_match(r_subs, t_all_subs)
        if not matched_all:
            return {
                "pure": False,
                "reason": f"二级细分簇错位（兔 {r_subs[0]} vs 龟 {t_all_subs[0]}，禁止大筐拉郎配）",
                "shared_subclusters": [],
            }
        matched_core = _subclusters_match(r_subs, t_core_subs)
        if (t_core_subs or len(tt) > top_n_core) and not matched_core:
            return {
                "pure": False,
                "reason": f"乌龟前{top_n_core}核心概念({','.join(core_tt)})未命中兔子细分簇({matched_all[0]})，属边缘蹭概念后排",
                "shared_subclusters": [],
            }
        chosen = matched_core or matched_all
        return {"pure": True, "reason": f"细分簇一致({chosen[0]})", "shared_subclusters": chosen}

    if r_subs and not t_all_subs and ax_key == "compute":
        return {
            "pure": False,
            "reason": f"「{axis_name}」核心硬件细分标签缺失（{','.join(core_tt)}），拦截边缘蹭概念",
            "shared_subclusters": [],
        }

    # 3) 前3核心标签纯度校验：若乌龟标签较多(>3)但前3核心标签完全未落入当前大轴，而第4+才落入 → 蹭概念剔除
    if ax_key and len(tt) > top_n_core:
        core_axes = map_to_axes(core_tt)
        if ax_key not in core_axes:
            return {
                "pure": False,
                "reason": f"前{top_n_core}核心概念({','.join(core_tt)})未归属「{axis_name}」，属边缘概念后排",
                "shared_subclusters": [],
            }

    return {"pure": True, "reason": "核心主轴一致", "shared_subclusters": []}


# ---------------------------------------------------------------------------
# 核心 2：两只票是否同题材（三级降级）
# ---------------------------------------------------------------------------

def common_themes(tags_a: list[str], tags_b: list[str],
                  industry_a: str | None = None,
                  industry_b: str | None = None) -> dict:
    """判定两只票是否同题材，返回 {'matched', 'level', 'shared', 'reason'}。

    判定顺序**不可调换**（严格 → 宽松，命中即返回）：

      1. ``axis``     两票板块 key 有交集。最严格：命中同一个**人工标注**板块。
      2. ``tag``      板块无交集，但原始标签**原文完全相同**（只认 T 类题材标签）。
                      这一级是长尾兜底：例如 11-18 的长虹美菱/四川长虹 都含
                      `家电`，而 `家电` 没被轴收录 —— 它们确实是同题材（家电），
                      只是轴还没补。业绩/属性标签不触发本级（见模块 docstring）。
      3. ``industry`` 前两级都无，但行业相同。**弱匹配**：行业是分类归属不是题材，
                      同一行业里两家公司可以毫无关系。`reason` 必带「仅行业相同，
                      非严格题材」，`level` 必为 'industry'，由调用方决定是否采用。
      4. 都没有 → ``matched=False``、``level=None``、``shared=[]``。

    Parameters
    ----------
    tags_a, tags_b : 两票的原始标签（`theme_pool.split_tags` 的输出形态，已切分）。
    industry_a, industry_b : 行业名（`feeds` 的 `hybk`）。给 None/空串就不参与第 3 级。

    Returns
    -------
    dict
        ``matched``: bool
        ``level``:   ``'axis'`` / ``'tag'`` / ``'industry'`` / ``None``
        ``shared``:  共享的**具体标识** —— axis 级给板块展示名，
                     tag 级给标签原文，industry 级给行业名
        ``reason``:  人能读懂的一句话，含级别与「为什么这样判」

    >>> common_themes(["液冷算力"], ["华为昇腾"], "IT服务Ⅱ", "计算机设")
    ... # doctest: +ELLIPSIS
    {'matched': True, 'level': 'axis', 'shared': ['算力'], 'reason': ...}
    >>> common_themes(["家电"], ["家电"], "白色家电", "白色家电")["level"]
    'tag'
    >>> common_themes(["三季报增长"], ["三季报增长"], "IT服务Ⅱ", "IT服务Ⅱ")["level"]
    'industry'
    >>> common_themes(["液冷算力"], ["白酒"], "IT服务Ⅱ", "白酒")["matched"]
    False
    """
    ta = _norm_tags(tags_a)
    tb = _norm_tags(tags_b)
    idx = _index()

    # ---- 1. axis：板块 key 交集（最严格，优先） --------------------------
    ka = map_to_axes(ta)
    kb = set(map_to_axes(tb))
    shared_keys = [k for k in ka if k in kb]
    if shared_keys:
        shared = [idx.key_name.get(k, k) for k in shared_keys]
        return {
            "matched": True,
            "level": AXIS_LEVEL,
            "shared": shared,
            "reason": (f"板块交集：{'、'.join(shared)}"
                       "（axis 级：两票命中同一人工题材板块，最严格）"),
        }

    # ---- 2. tag：原始标签原文相同（只认 T 类，业绩/属性不凑数） ----------
    set_b = set(tb)
    shared_tags = [t for t in ta if t in set_b and is_theme_tag(t)]
    if shared_tags:
        return {
            "matched": True,
            "level": TAG_LEVEL,
            "shared": shared_tags,
            "reason": (f"原始标签相同：{'、'.join(shared_tags)}"
                       "（tag 级：该标签未进题材轴，属长尾兜底；"
                       "同板块级不等价，采用与否由调用方决定）"),
        }

    # ---- 3. industry：行业相同（弱匹配，必须自证） -----------------------
    # 2026-11-23 用户裁定：同题材/同板块 = 同一个概念，行业兜底不作为硬凑依据。
    # 无标签票即使行业相同也不配 —— 旧实现在 `not ta or not tb` 时仍会走到
    # 这里按行业判 True，等于给"数据缺失"的票开了后门（造伪）。
    # 现在改为：industry 级**只对"有标签但三位皆空"的票**保留（见 group_by_theme），
    # 且 level 标注 + reason 自证；两票任一无题材标签 → 直接判 False。
    ia = _norm_industry(industry_a)
    ib = _norm_industry(industry_b)
    if ia and ia == ib and ta and tb:
        return {
            "matched": True,
            "level": INDUSTRY_LEVEL,
            "shared": [ia],
            "reason": (f"行业相同：{ia}"
                       f"（industry 级：{INDUSTRY_CAVEAT}，"
                       "同一行业里两家公司可以毫无关系，不要当题材用）"),
        }

    # ---- 4. 都不是 -------------------------------------------------------
    if not ta or not tb:
        why = "两票中至少一只没有任何题材标签，无法判定同题材（不猜）"
    else:
        why = "无共享板块 / 无相同题材标签 / 行业不同"
    return {"matched": False, "level": None, "shared": [], "reason": why}


# ---------------------------------------------------------------------------
# 核心 3：当日全部涨停股按题材分组（多标签展开）
# ---------------------------------------------------------------------------

def group_by_theme(items: list[dict]) -> dict:
    """把当日全部涨停股按题材分组（多标签展开），返回 {'groups', 'unlabeled'}。

    items 每项: ``{'code','name','tags','industry','streak'}``。

    分组规则（**多标签展开**是重点：一只票可以进多个组）：

      * ``level='axis'``: 每个命中的板块 key 一组，组名用板块中文名（如「算力」）。
        一只票命中 3 个板块就进 3 个组 —— 这样它和「只靠第 2 个板块才同框」的
        另一只票能被正确凑到一起，而这正是旧单板块口径丢掉的东西。
      * ``level='tag'``: 只对**未进轴的 T 类原始标签**建组（如 `家电`）。
        已进轴的标签不再建组（否则同一批票在 axis 组和 tag 组里各出现一次，
        组名不同、内容重复，下游要写去重逻辑，得不偿失）。
      * ``level='industry'``: 只有「既没有板块、也没有未进轴题材标签、但确实有标签
        和行业」的票才按行业成组（即只有业绩/属性标签的票）。
      * ``unlabeled``: **无任何标签的票**，以及「有标签但三位皆空」的票。
        它们不塞进任何组 —— 行业是分类归属不是题材，塞进去就是造伪。
        无标签票**即使有行业也不进 industry 组**（这条有测试锁着）。

    组内 `codes` 保持输入顺序（调用方按 `streak` 排过序的话，组内也是那个顺序）。
    单票组也照常返回（不是 bug）：只有单票的组意味着「这个题材今天没搭档」，
    让调用方看见比悄悄删掉更诚实。

    Returns
    -------
    dict
        ``groups``:    ``[{'theme': str, 'level': str, 'codes': [code, ...]}, ...]``
                       已按 level 严格度 → 组内票数降序 → 组名 排序
        ``unlabeled``: ``[{'code','name','streak'}, ...]``（板位降序、代码升序）

    >>> out = group_by_theme([
    ...     {"code": "1", "name": "兔", "tags": ["液冷算力"], "industry": "IT服务Ⅱ", "streak": 2},
    ...     {"code": "2", "name": "龟", "tags": ["华为昇腾", "家电"], "industry": "计算机设", "streak": 2},
    ...     {"code": "3", "name": "无标签", "tags": [], "industry": "计算机设", "streak": 2},
    ... ])
    >>> [(g["theme"], g["level"], g["codes"]) for g in out["groups"]]
    [('算力', 'axis', ['1', '2']), ('家电', 'tag', ['2'])]
    >>> [u["code"] for u in out["unlabeled"]]
    ['000003']
    """
    norm = _norm_items(items)
    idx = _index()

    axis_groups: dict[str, list[str]] = {}
    tag_groups: dict[str, list[str]] = {}
    ind_groups: dict[str, list[str]] = {}
    unlabeled: list[dict[str, Any]] = []

    for it in norm:
        codes: list[str] = [it["code"]]
        keys = map_to_axes(it["tags"])
        # 过滤掉已命中任意轴（含语义/共现）的标签，避免在 axis 组和 tag 组重复建组
        longtail = [t for t in it["tags"]
                    if t not in _PLACEHOLDER_TAGS
                    and not map_to_axes([t])
                    and is_theme_tag(t)]
        industry = it["industry"]

        placed = False
        for k in keys:
            axis_groups.setdefault(k, []).extend(codes)
            placed = True
        for t in longtail:
            tag_groups.setdefault(t, []).extend(codes)
            placed = True
        if not placed and it["tags"] and industry:
            ind_groups.setdefault(industry, []).extend(codes)
            placed = True
        if not placed:
            unlabeled.append({"code": it["code"], "name": it["name"],
                              "streak": it["streak"]})

    # 第 2b 层：新兴概念词根前缀聚类（Day-1 首次出现且未进轴的相近概念，如「深海科技」+「深海装备」→「深海*」）
    # 仅在 ≥2 只不同个股携带不同原始标签但共享同一专有二字中文前缀时建组，绝不把停用词硬凑成组。
    stem_codes: dict[str, list[str]] = {}
    stem_tags: dict[str, set[str]] = {}
    for t, cs in tag_groups.items():
        if len(t) >= 3 and all("\u4e00" <= ch <= "\u9fff" for ch in t[:2]):
            pref = t[:2]
            if pref in _STEM_STOP_PREFIXES:
                continue
            stem_tags.setdefault(pref, set()).add(t)
            bucket = stem_codes.setdefault(pref, [])
            for c in cs:
                if c not in bucket:
                    bucket.append(c)
    for pref, cs in stem_codes.items():
        if len(cs) >= 2 and len(stem_tags.get(pref) or ()) >= 2:
            gname = f"{pref}概念"
            if gname not in tag_groups:
                tag_groups[gname] = cs

    groups: list[dict[str, Any]] = []
    for k, codes in axis_groups.items():
        groups.append({"theme": idx.key_name.get(k, k),
                       "level": AXIS_LEVEL, "codes": codes})
    for t, codes in tag_groups.items():
        groups.append({"theme": t, "level": TAG_LEVEL, "codes": codes})
    for t, codes in ind_groups.items():
        groups.append({"theme": t, "level": INDUSTRY_LEVEL, "codes": codes})

    groups.sort(key=lambda g: (LEVEL_RANK.get(g["level"], 9),
                               -len(g["codes"]), g["theme"]))
    unlabeled.sort(key=lambda u: (-int(u.get("streak") or 0), u["code"]))
    return {"groups": groups, "unlabeled": unlabeled}


# ---------------------------------------------------------------------------
# 核心 4：覆盖率统计（回答「有多少票根本没题材可用」）
# ---------------------------------------------------------------------------

def coverage_stats(items: list[dict]) -> dict:
    """覆盖率统计：总数 / 有标签数 / 能映射到轴的数 / 无标签与未收录明细。

    回答的是**数据问题**，不是策略问题：
      * ``tagged_pct``  有标签的比例 —— 没有标签的票，任何题材匹配都救不了；
      * ``axis_pct``    标签能落到人工板块的比例 —— 这个数低说明 yaml 该补了，
        **不是**匹配算法不行；
      * ``unmapped``    有标签但一个板块都没命中的票（按板位降序）→ 这就是
        「该补哪些标签」的施工清单；
      * ``unlabeled``   完全没有标签的票（按板位降序）。

    排序统一「板位降序、代码升序」：板位高的漏了才真的痛，先看它们。

    Returns
    -------
    dict
        ``n`` / ``n_tagged`` / ``n_axis`` / ``tagged_pct`` / ``axis_pct``
        （两个 pct 都是 0~100 的百分比，1 位小数，0 分母返回 0.0）
        ``unlabeled``: ``[{'code','name','streak'}]``
        ``unmapped``:  ``[{'code','name','tags'}]``

    自洽关系（有测试锁）：
        ``n_tagged <= n``、``n_axis <= n_tagged``、
        ``len(unlabeled) == n - n_tagged``、
        ``len(unmapped) == n_tagged - n_axis``。
    """
    norm = _norm_items(items)
    n = len(norm)

    unlabeled: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    n_tagged = 0
    n_axis = 0

    for it in norm:
        if not it["tags"]:
            unlabeled.append({"code": it["code"], "name": it["name"],
                              "streak": it["streak"]})
            continue
        n_tagged += 1
        if map_to_axes(it["tags"]):
            n_axis += 1
        else:
            unmapped.append({"code": it["code"], "name": it["name"],
                             "tags": list(it["tags"])})

    unlabeled.sort(key=lambda u: (-int(u.get("streak") or 0), u["code"]))
    unmapped.sort(key=lambda u: u["code"])

    return {
        "n": n,
        "n_tagged": n_tagged,
        "n_axis": n_axis,
        "tagged_pct": _pct(n_tagged, n),
        "axis_pct": _pct(n_axis, n),
        "unlabeled": unlabeled,
        "unmapped": unmapped,
    }


# ---------------------------------------------------------------------------
# 自检入口：把「覆盖率」与「该补哪些标签」直接打出来（只读，不写文件）
# ---------------------------------------------------------------------------
#   python -m backend.theme_match              # 最近一天
#   python -m backend.theme_match 2026-11-18   # 指定日
#
# 为什么要有这个入口：覆盖率是会随 yaml 版本变的**可复算事实**。
# 写在注释里的数字一年后就是谣言，跑一下就有真数。

def _items_from_parquet(day: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """从 `data/themes/themes.parquet` 取某日涨停（zt 池），转成本模块的 items。

    ⚠️ parquet 里**没有 industry 列**（行业来自实时行情 `feeds`，不落这张表），
    所以这里 industry 一律 None → 第 3 级(industry)不参与。这是数据的真实边界，
    不在这里编一个行业出来。
    """
    import polars as pl

    from backend import theme_pool

    p = theme_pool.themes_file()
    if not p.is_file():
        return [], ""
    df = pl.read_parquet(p)
    if df.height == 0:
        return [], ""
    if day:
        d = theme_pool._norm_day(day)
    else:
        d = str(df["date"].max())
    rows = df.filter((pl.col("date") == d) & (pl.col("pool") == "zt"))
    items: list[dict[str, Any]] = []
    for r in rows.iter_rows(named=True):
        items.append({
            "code": str(r.get("code") or "").zfill(6),
            "name": r.get("name") or "",
            "tags": list(r.get("tags") or []),
            "industry": None,
            "streak": int(r.get("n_board") or 0),
        })
    return items, d


def _main(argv: list[str] | None = None) -> None:
    import sys
    from collections import Counter

    args = list(sys.argv[1:] if argv is None else argv)
    items, day = _items_from_parquet(args[0] if args else None)
    ax = load_axis()
    n_match = sum(len(a["match"]) for a in ax["axes"])
    print("=" * 78)
    print(f"题材轴 v{ax['version']}：{len(ax['axes'])} 个板块 / {n_match} 个标签")
    print(f"数据日：{day or '（无落盘）'}  涨停 {len(items)} 只")
    print("=" * 78)
    if not items:
        print("该日无涨停数据，先跑 backend.theme_pool.collect()")
        return

    st = coverage_stats(items)
    print(f"  有原始标签      {st['n_tagged']:>4}/{st['n']:<4} = {st['tagged_pct']:>5.1f}%")
    print(f"  能映射到题材轴  {st['n_axis']:>4}/{st['n']:<4} = {st['axis_pct']:>5.1f}%")
    print(f"  无标签          {len(st['unlabeled']):>4} 只")
    print(f"  有标签但轴未收录 {len(st['unmapped']):>4} 只")

    if st["unmapped"]:
        print("\n【有标签但一个板块都没命中的票 —— 这就是补轴的施工清单】")
        for u in st["unmapped"][:30]:
            print(f"  {u['code']} {u['name'][:8]:<9} tags={u['tags']}")

    # 未收录 T 类标签频次（下次改 yaml 直接拿这个抄）
    cnt: Counter[str] = Counter()
    for u in st["unmapped"]:
        for t in u["tags"]:
            if is_theme_tag(t):
                cnt[t] += 1
    if cnt:
        print("\n【未收录的 T 类标签 Top 20（按票数）】")
        for t, c in cnt.most_common(20):
            print(f"  {t:<22} {c:>2} 只")

    g = group_by_theme(items)
    by_level = Counter(x["level"] for x in g["groups"])
    paired = sum(1 for x in g["groups"] if len(x["codes"]) >= 2)
    print(f"\n【分组】{len(g['groups'])} 组（{dict(by_level)}），"
          f"其中 ≥2 只票的组 {paired} 个；未入组 {len(g['unlabeled'])} 只")
    for x in g["groups"][:15]:
        print(f"  [{x['level']:<8}] {x['theme'][:16]:<18} {len(x['codes'])} 只  "
              f"{x['codes'][:6]}{'...' if len(x['codes']) > 6 else ''}")


if __name__ == "__main__":
    _main()
