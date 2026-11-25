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
    """清空轴缓存。测试改 yaml / monkeypatch 路径后必须调，生产不用。"""
    _AXIS_CACHE.clear()


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


# ---------------------------------------------------------------------------
# 核心 1：单票原始标签 → 板块 key（多标签展开）
# ---------------------------------------------------------------------------

def map_to_axes(tags: list[str]) -> list[str]:
    """原始标签 → 命中的板块 key 列表（**可能多个**，保序去重）。

    这是本模块与旧实现的分界：旧口径一只票只留**一个**板块，这里保留**全部**
    命中板块。真视通 `液冷算力/华为算力/多媒体视讯` → `['compute']`；
    中鼎股份 `飞行汽车/人形机器人/数据中心液冷/汽车零部件`
    → `['aerospace', 'robot', 'compute']`（真实数据 2026-10-16）。

    「保序」指的是**按标签出现顺序**排列，即保留这只票自己的主次关系：
    `reason_type` 里越靠前的标签通常是越核心的题材。不做字典序重排——
    重排会把这个信息抹掉。两个 key 之间谁先出现不重要（判定用的是集合），
    但输出必须稳定可断言。

    未命中任何板块 → 返回 `[]`（**不是** `['other']`）：
    空列表是「信息不足」这个事实本身，调用方据此降级到 tag / industry 级。

    >>> map_to_axes(["液冷算力", "华为算力", "多媒体视讯"])
    ['compute']
    >>> map_to_axes(["三季报增长", "央企"])
    []
    >>> map_to_axes([])
    []
    """
    idx = _index()
    out: list[str] = []
    for t in _norm_tags(tags):
        k = idx.tag_to_key.get(t)
        if k and k not in out:
            out.append(k)
    return out


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
        longtail = [t for t in it["tags"]
                    if t not in idx.tag_to_key and is_theme_tag(t)]
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
