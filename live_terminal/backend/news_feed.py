# -*- coding: utf-8 -*-
"""国内政策与 7×24 实时快讯引擎：题材映射 + 个股利空雷区拦截。

核心设计原则
------------
1. **高信噪比免费直连（零 API Key、零虚构）**：
   - 主源：同花顺 7×24 快讯（含重要度、红标、官方标签与关联股票代码）
   - 热备：东方财富 7×24 快讯（含 `stockList` 关联代码与 `titleColor`）
2. **确定性两级映射（复用 `configs/theme_axis.yaml`）**：
   - 板块级：用 11 大题材轴的 447 个真实标签 + 政策关键词匹配到统一板块名（如 `算力 / 机器人 / 消费 / 医药`）；
   - 个股级：直接提取快讯自带的 6 位 A 股代码 + 正文/标题精确命中，识别减持/立案/问询/核查等硬雷区。
3. **严格时间因果与可回溯落盘**：
   - 每条快讯保留真实发布时刻 `published_at`，原子合并落盘至 `data/live/<day>/news.jsonl`；
   - 支持按 `cutoff_ts` 截断查询（早盘只看 09:25 前隔夜消息，杜绝事后新闻污染）。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import paths, process_guard

_LOCK = threading.RLock()
_MEM_CACHE: tuple[float, dict[str, Any]] | None = None
_TTL_SEC = 120.0
_AXIS_CACHE: list[tuple[str, list[str]]] | None = None
_STOCK_DICT_CACHE: tuple[dict[str, str], dict[str, str]] | None = None

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: 否定前缀掩码（分句级扫描，防止“承诺24个月内不减持 / 不以其他任何方式转让或减持”隔多个字失效）
NEGATED_DANGER_RE = re.compile(
    r"(?:承诺|保证|声明|决定|拟)[^，。；！？\n]{0,18}(?:不|未|无|不再)[^，。；！？\n]{0,14}(?:转让或)?减持"
    r"|(?:不|未|无|不会|不再|提前终止|终止|停止|放弃|取消|解除|撤销)[^，。；！？\n]{0,18}(?:转让或)?减持"
    r"|(?:不|未|无|不会|不再|承诺不|决定不|拟不|终止|停止|放弃|取消|解除|撤销)"
    r"(?:实施|进行|存在|构成|触及|涉及)?"
    r"(?:立案|立案调查|退市风险|ST|风险警示|违规|违规担保|资金占用|处罚|停牌核查)"
)

#: 监管立案与被动涉诉正则（严防把公司作为原告去法院讨债的「法院已立案」混同成证监会立案调查）
_REG_INVESTIGATION_RE = re.compile(
    r"(?:证监会|证监局|监管局|交易所|监察委|纪委|公安|警方|涉嫌(?:信息披露)?违法(?:违规)?)"
    r"[^，。；！？\n]{0,14}立案"
    r"|立案调查|被立案|遭立案|收到[^，。；！？\n]{0,8}立案"
)
_PLAINTIFF_LAWSUIT_RE = re.compile(
    r"(?:公司|子公司|全资子公司|控股子公司)[^，。；！？\n]{0,30}"
    r"(?:作为原告|提起诉讼|向[^，。；！？\n]{0,14}法院(?:提起)?诉讼|追讨)"
    r"|就[^，。；！？\n]{0,16}提起诉讼"
)
_PASSIVE_LAWSUIT_RE = re.compile(
    r"被起诉|作为被告|(?:被|遭)[^，。；！？\n]{0,8}(?<!提)起诉|被法院立案|被申请破产|被强制执行"
)
_FUND_OCCUPY_RE = re.compile(
    r"(?:控股股东|实控人|实际控制人|关联方|大股东)[^，。；！？\n]{0,12}资金占用|非经营性资金占用"
)

#: 中国央行 vs 海外央行（防止英国央行/印尼央行被当成国内放水利好或映射进 AI传媒）
_CN_CENTRAL_BANK_RE = re.compile(
    r"中国央行|中国人民银行|人民银行|央行(?:降准|降息|逆回购|MLF|LPR|互换便利|再贷款|净投放|护盘|稳市|买断式|开展)"
)
_FOREIGN_CENTRAL_BANK_RE = re.compile(
    r"英国央行|欧洲央行|日本央行|印尼央行|韩国央行|加拿大央行|瑞士央行|澳洲联储|澳大利亚央行|"
    r"新西兰联储|印度央行|巴西央行|土耳其央行|俄罗斯央行|美联储|英格兰银行"
)

#: 异动降温 / 辟谣澄清公告识别（严禁把否认/尚未创收公告当作题材催化剂喂给选股池）
CLARIFICATION_COOLING_RE = re.compile(
    r"尚未产生(?:营业)?收入|未产生(?:营业)?收入|尚未实现盈利|尚处于起步阶段|尚处于建设阶段|"
    r"占公司(?:营业)?收入比重(?:极小|较小|较低|不足)|营收占比(?:极小|较小|较低|不足)|"
    r"暂无[^，。；！？\n]{0,10}相关业务|不涉及[^，。；！？\n]{0,10}业务|未开展[^，。；！？\n]{0,10}业务|"
    r"敬请广大投资者注意二级市场交易风险|交易风险提示|异动(?:公告|提示)[^，。；！？\n]{0,16}未"
)

#: 过于宽泛、易在外交/宏观通稿里误匹配的短标签过滤词
_GENERIC_THEME_STOP: set[str] = {
    "合作", "发展", "投资", "安全", "建设", "运营", "服务", "材料", "装备", "制造",
    "集团", "控股", "科技", "信息", "系统", "项目", "产品", "国际", "中国", "行业",
    "市场", "经济", "政策", "生态", "数字", "智能", "平台", "网络", "工程", "管理",
    "基础", "综合", "高端", "核心", "新型", "现代", "绿色", "文化", "体育", "教育",
    "创新", "技术", "产业", "能源", "环保", "医疗", "健康", "消费", "贸易", "金融",
    "化工", "建筑", "地产", "物流", "交通", "通信", "电子", "软件", "硬件", "设备",
    "全球", "海外", "出口", "国产", "升级", "转型", "改革", "开放", "共识", "声明",
}

#: 个股 / 板块硬利空雷区关键词（触发 REJECT / 减仓警示；立案/资金占用改由上下文正则精准识别）
DANGER_KEYWORDS: tuple[str, ...] = (
    "减持", "拟减持", "立案调查", "问询函", "关注函", "监管函", "警示函",
    "停牌核查", "核查停牌", "严重异常波动", "低于面值退市", "面值退市",
    "可能被终止上市", "终止上市风险", "存在退市风险", "可能触及退市", "退市风险",
    "实施ST", "业绩预亏", "业绩暴雷", "行政处罚", "涉嫌违规", "违规担保",
    "监管降温", "被证监会", "实控人被查", "留置", "强制退市",
)

#: 全市场级重磅稳市/宏观政策利好关键词（即使 themes=[] 也不归属单一细分板块，必须触发全市场顺风警报）
MACRO_MARKET_BULL_KEYWORDS: tuple[str, ...] = (
    "平准基金", "国家队", "稳市", "中央汇金", "中长期资金入市", "资本市场投融资",
    "活跃资本市场", "大力提振", "政治局会议", "全面降准", "超预期降息", "下调印花税",
    "互换便利", "回购增持再贷款", "井喷上涨", "两提资本市场",
)

#: 政策与产业利好催化关键词（移除宽泛词「央行」，改由 _CN_CENTRAL_BANK_RE 限定中国语境）
POSITIVE_KEYWORDS: tuple[str, ...] = (
    "国务院", "发改委", "工信部", "财政部", "人民银行", "国常会", "重磅", "支持",
    "补贴", "涨价", "提价", "突破", "量产", "中标", "大单", "订单", "预增",
    "扭亏", "战略合作", "利好", "回购", "增持", "获批", "首发", "创新高",
)

#: 一般性利空 / 压制关键词
NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "暴跌", "大跌", "重挫", "下调", "制裁", "关税", "限制出口", "出口管制",
    "亏损", "下滑", "终止筹划", "终止重组", "风险提示", "净流出", "跌停",
)

#: 政策补充词（在 theme_axis.yaml 447 标签基础上补充宏观政策高频词）
EXTRA_THEME_HINTS: dict[str, tuple[str, ...]] = {
    "算力": ("英伟达", "算力", "光模块", "CPO", "液冷", "GPU", "半导体", "芯片", "存储芯片", "HBM", "数据中心", "服务器"),
    "机器人": ("人形机器人", "具身智能", "机器人", "特斯拉", "Optimus", "丝杠", "减速器", "灵巧手", "自动驾驶", "智驾"),
    "AI传媒": ("人工智能", "大模型", "AI应用", "智能体", "Agent", "短剧", "影视", "游戏", "AIGC", "消费电子", "AI手机", "AI眼镜"),
    "消费": ("促消费", "消费补贴", "以旧换新", "白酒", "免税", "零售", "旅游", "食品饮料", "电商", "首发经济"),
    "新能源": ("固态电池", "锂电", "储能", "光伏", "风电", "新能源车", "充电桩", "钠电池", "氢能"),
    "军工航天": ("商业航天", "低空经济", "无人机", "卫星互联网", "军工", "航空发动机", "导弹", "大飞机"),
    "医药": ("创新药", "医保", "集采", "生物医药", "减肥药", "中药", "医疗器械", "脑机接口"),
    "数据要素": ("数据要素", "数据局", "数据资产", "信创", "鸿蒙", "网络安全", "跨境支付", "数字货币"),
    "化工周期": ("化工", "涨价", "原油", "黄金", "铜", "稀土", "煤炭", "有色", "磷化工", "氟化工", "锂矿"),
    "基建地产": ("房地产", "城中村", "化债", "基建", "专项债", "降准", "降息", "房贷", "保障房"),
}

_A_SHARE_RE = re.compile(r"\b((?:00|30|60|68|83|87|43|92)\d{4})\b")


def _today8() -> str:
    try:
        from backend import market_clock
        tz = market_clock.TZ_CN
        return (datetime.now(tz) if tz else datetime.now()).strftime("%Y%m%d")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y%m%d")


def _load_theme_keywords() -> list[tuple[str, list[str]]]:
    """加载 configs/theme_axis.yaml 题材轴关键词并与政策词合并（过滤易误匹配泛词）。"""
    global _AXIS_CACHE
    if _AXIS_CACHE is not None:
        return _AXIS_CACHE
    merged: dict[str, set[str]] = {k: set(v) for k, v in EXTRA_THEME_HINTS.items()}
    try:
        import yaml
        p = paths.configs_dir() / "theme_axis.yaml"
        if p.is_file():
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            for ax in doc.get("axes") or []:
                name = str(ax.get("name") or "").strip()
                if not name:
                    continue
                bag = merged.setdefault(name, set())
                if len(name) >= 3 or name not in _GENERIC_THEME_STOP:
                    bag.add(name)
                for tag in ax.get("match") or []:
                    t = str(tag or "").strip()
                    if len(t) >= 3 and t not in _GENERIC_THEME_STOP:
                        bag.add(t)
                    elif len(t) == 2 and t not in _GENERIC_THEME_STOP and t in (
                        "算力", "液冷", "光刻", "芯片", "存储", "硅片", "丝杠", "短剧",
                        "漫剧", "影游", "院线", "手游", "白酒", "免税", "锂电", "储能",
                        "光伏", "风电", "氢能", "钠电", "军工", "导弹", "航发", "医保",
                        "中药", "信创", "鸿蒙", "稀土", "锂矿", "磷矿", "氟化", "化债",
                    ):
                        bag.add(t)
    except Exception:  # noqa: BLE001
        pass
    _AXIS_CACHE = [(k, sorted(v, key=len, reverse=True)) for k, v in merged.items()]
    return _AXIS_CACHE


def _stock_name_maps() -> tuple[dict[str, str], dict[str, str]]:
    """加载真实 A 股 code↔name 字典（内存缓存，零虚构）。"""
    global _STOCK_DICT_CACHE
    if _STOCK_DICT_CACHE is not None:
        return _STOCK_DICT_CACHE
    c2n: dict[str, str] = {}
    n2c: dict[str, str] = {}
    try:
        from backend import feeds
        df = feeds.stock_list()
        if df is not None and df.height > 0:
            for r in df.iter_rows(named=True):
                c = str(r.get("ts_code") or "").strip().zfill(6)
                nm = str(r.get("name") or "").strip()
                if len(c) == 6 and c.isdigit() and nm:
                    c2n[c] = nm
                    clean = nm.replace(" ", "")
                    if len(clean) >= 3:
                        n2c[clean] = c
    except Exception:  # noqa: BLE001
        pass
    _STOCK_DICT_CACHE = (c2n, n2c)
    return _STOCK_DICT_CACHE


def build_theme_stock_map(day: str | None = None, *,
                          max_per_theme: int = 6) -> dict[str, list[dict[str, Any]]]:
    """用当日真实 theme_pool（连板/涨停/炸板三池）构建「A股题材轴 → 活跃对应个股」映射。"""
    try:
        from backend import theme_pool
        d8 = str(day or _today8()).replace("-", "")[:8]
        d_dash = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        df = theme_pool.load(d_dash)
        if df is None or df.height == 0:
            # 盘前当日池未落盘时，回退最近一个真实交易日（并标注 ref_day）
            days = theme_pool.available_days() if hasattr(theme_pool, "available_days") else []
            if days:
                d_dash = sorted(days)[-1]
                df = theme_pool.load(d_dash)
        if df is None or df.height == 0:
            return {}

        axis = theme_pool.load_axis() or {}
        amap = axis.get("axes") or {}
        tag_to_theme: dict[str, str] = {}
        for tag, info in amap.items():
            if isinstance(info, dict) and info.get("name"):
                tag_to_theme[str(tag)] = str(info["name"])

        pool_rank = {"lianban": 0, "zt": 1, "zb": 2}
        by_theme: dict[str, dict[str, dict[str, Any]]] = {}
        for r in df.iter_rows(named=True):
            code = str(r.get("code") or "").strip().zfill(6)
            name = str(r.get("name") or "").strip()
            if not code or code == "000000" or not name:
                continue
            raw_tags = list(r.get("tags") or [])
            reason = str(r.get("reason_raw") or "")
            themes: set[str] = set()
            for t in raw_tags:
                if str(t) in tag_to_theme:
                    themes.add(tag_to_theme[str(t)])
            # 补充用题材关键词匹配 reason_raw
            if not themes and reason:
                for ax_name, kws in _load_theme_keywords():
                    if any(kw in reason for kw in kws):
                        themes.add(ax_name)
            if not themes:
                continue
            pool = str(r.get("pool") or "zt")
            nb = int(r.get("n_board") or (1 if pool in ("zt", "lianban") else 0))
            rec = {
                "ts_code": code,
                "name": name,
                "pool": pool,
                "n_board": nb,
                "high_days": str(r.get("high_days_raw") or (f"{nb}板" if nb > 0 else "炸板")),
                "limit_up_type": str(r.get("limit_up_type") or ""),
                "reason": reason[:48],
                "ref_day": d_dash,
            }
            for th in themes:
                bucket = by_theme.setdefault(th, {})
                prev = bucket.get(code)
                if prev is None or (pool_rank.get(pool, 9), -nb) < (pool_rank.get(prev["pool"], 9), -prev["n_board"]):
                    bucket[code] = rec

        out: dict[str, list[dict[str, Any]]] = {}
        for th, cmap in by_theme.items():
            rows = sorted(
                cmap.values(),
                key=lambda x: (pool_rank.get(x["pool"], 9), -x["n_board"], x["ts_code"]),
            )
            out[th] = rows[:max_per_theme]
        return out
    except Exception:  # noqa: BLE001
        return {}


def _extract_a_codes(raw_stocks: Any, text: str) -> list[str]:
    """提取关联的 6 位 A 股代码（剔除港股/美股代码）。"""
    found: set[str] = set()
    if isinstance(raw_stocks, list):
        for s in raw_stocks:
            if isinstance(s, dict):
                cand = str(s.get("stockCode") or s.get("code") or "")
            else:
                cand = str(s or "")
                # 东财格式 "0.002931" / "1.600519"（港股为 116.xxxxx，美股为 105.xxx）
                if "." in cand:
                    mkt, code = cand.split(".", 1)
                    if mkt not in ("0", "1"):
                        continue
                    cand = code
            cand = cand.strip()
            if len(cand) == 6 and cand.isdigit() and cand.startswith(("00", "30", "60", "68", "83", "87", "43", "92")):
                found.add(cand)
    for m in _A_SHARE_RE.findall(text or ""):
        found.add(m)
    return sorted(found)


def classify_item(item: dict[str, Any]) -> dict[str, Any]:
    """对单条快讯执行确定性题材归类、个股名称反查、否定词屏蔽与雷区识别（纯函数）。"""
    title = str(item.get("title") or "").strip()
    digest = str(item.get("digest") or "").strip()
    tags_txt = " ".join(str(x) for x in (item.get("raw_tags") or []))
    text = f"{title} {digest} {tags_txt}"

    # 1) 题材轴匹配（海外央行宏观通稿且无A股关联时，不向国内题材轴映射噪音）
    is_foreign_cb = bool(_FOREIGN_CENTRAL_BANK_RE.search(text))
    themes: list[str] = []
    # 2) A 股代码提取 + 标题“股名：”精确反查（补全中文名）
    c2n, n2c = _stock_name_maps()
    codes_set = set(_extract_a_codes(item.get("raw_stocks"), f"{title} {digest}"))
    m_head = re.match(r"^【?([*A-Za-z\u4e00-\u9fa5]{1,2}[\u4e00-\u9fa5A-Za-z0-9]{1,7})[：:]", title)
    if m_head:
        cand_name = m_head.group(1).strip()
        if cand_name in n2c:
            codes_set.add(n2c[cand_name])
    codes = sorted(codes_set)[:8]
    stocks = [{"ts_code": c, "name": c2n.get(c, "")} for c in codes]

    if not (is_foreign_cb and not codes):
        for axis_name, kws in _load_theme_keywords():
            if any(kw in text for kw in kws):
                themes.append(axis_name)

    # 3) 极性与雷区识别（先屏蔽否定/制度规范/外资评级/无跌停/画大饼增持等上下文，杜绝机械误报）
    clean_for_danger = NEGATED_DANGER_RE.sub("", text)
    # 屏蔽“严格规范减持/限制减持/评级下调至减持/0只跌停/无跌停”
    clean_for_danger = re.sub(
        r"(?:严格)?规范[^\s，。；]{0,12}减持|限制[^\s，。；]{0,8}减持|评级[^\s，。；]{0,10}减持|下调至减持|0只?跌停|无跌停",
        "",
        clean_for_danger,
    )
    # 屏蔽“尚未实施增持/未实施增持/时间过半未增持”等画大饼利空误判为利好
    clean_for_pos = re.sub(
        r"(?:尚)?未实施增持|(?:尚)?未增持|时间过半[^\s，。；]{0,12}(?:未|0)[^\s，。；]{0,6}增持",
        "",
        text,
    )
    danger_hits = [k for k in DANGER_KEYWORDS if k in clean_for_danger]
    # 监管立案 vs 原告维权起诉（避免「公司提起诉讼……法院已立案」误判）
    is_plaintiff = bool(_PLAINTIFF_LAWSUIT_RE.search(clean_for_danger))
    if _REG_INVESTIGATION_RE.search(clean_for_danger):
        if "立案" not in danger_hits:
            danger_hits.append("立案")
    if _PASSIVE_LAWSUIT_RE.search(clean_for_danger) and not is_plaintiff:
        if "被动涉诉" not in danger_hits:
            danger_hits.append("被动涉诉")
    if _FUND_OCCUPY_RE.search(clean_for_danger) and not is_plaintiff:
        if "资金占用" not in danger_hits:
            danger_hits.append("资金占用")

    # 异动降温 / 辟谣澄清公告识别
    is_clarification_cooling = bool(CLARIFICATION_COOLING_RE.search(text))

    # 无关联 A 股代码且无题材命中的社会治安/海外政治新闻不标为个股硬利空雷区（退市风险/ST除外）
    has_delist_risk = any("退市" in k or "终止上市" in k for k in danger_hits) or ("ST" in title and bool(danger_hits))
    if danger_hits and not codes and not themes and not has_delist_risk:
        danger_hits = []
    pos_hits = [k for k in POSITIVE_KEYWORDS if k in clean_for_pos]
    if _CN_CENTRAL_BANK_RE.search(clean_for_pos) and not is_foreign_cb:
        if "中国央行" not in pos_hits:
            pos_hits.append("中国央行")
    neg_hits = [k for k in NEGATIVE_KEYWORDS if k in clean_for_danger]
    if clean_for_pos != text and "未实施增持" not in neg_hits:
        neg_hits.append("未实施增持")
    if is_clarification_cooling and "异动降温澄清" not in neg_hits:
        neg_hits.append("异动降温澄清")

    if danger_hits:
        sentiment, score = "danger", -2
    elif is_clarification_cooling:
        sentiment, score = "negative", -1
    elif len(pos_hits) > len(neg_hits):
        sentiment, score = "positive", (2 if (item.get("important") or len(pos_hits) >= 2) else 1)
    elif neg_hits:
        sentiment, score = "negative", -1
    else:
        sentiment, score = "neutral", 0

    return {
        "id": str(item.get("id") or ""),
        "source": str(item.get("source") or "unknown"),
        "published_at": str(item.get("published_at") or ""),
        "title": title or digest[:60],
        "digest": digest[:240],
        "url": str(item.get("url") or ""),
        "important": bool(item.get("important")),
        "themes": themes[:4],
        "codes": codes,
        "stocks": stocks,
        "sentiment": sentiment,
        "score": score,
        "is_clarification_cooling": is_clarification_cooling,
        "danger_hits": danger_hits[:4],
        "pos_hits": pos_hits[:4],
    }


def _fetch_ths(pagesize: int = 35) -> list[dict[str, Any]]:
    url = f"https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize={pagesize}"
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Referer": "https://news.10jqka.com.cn/"}
    )
    d = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace"))
    rows = ((d.get("data") or {}).get("list")) or []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts_raw = r.get("rtime") or r.get("ctime")
        pub = ""
        try:
            if ts_raw and str(ts_raw).isdigit():
                pub = datetime.fromtimestamp(int(ts_raw)).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001
            pub = ""
        raw_tags = [t.get("name") for t in (r.get("tags") or []) if isinstance(t, dict) and t.get("name")]
        raw_tags += [t.get("name") for t in (r.get("tagInfo") or []) if isinstance(t, dict) and t.get("name")]
        out.append({
            "id": f"ths:{r.get('id') or r.get('seq')}",
            "source": "同花顺7×24",
            "published_at": pub,
            "title": str(r.get("title") or "").strip(),
            "digest": str(r.get("digest") or r.get("short") or "").strip(),
            "url": str(r.get("url") or ""),
            "important": str(r.get("import") or "0") == "1" or str(r.get("color") or "1") != "1",
            "raw_tags": raw_tags,
            "raw_stocks": r.get("stock") or [],
        })
    return out


def _fetch_em(pagesize: int = 25) -> list[dict[str, Any]]:
    url = (
        "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
        f"?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize={pagesize}&req_trace=1"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
    )
    d = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace"))
    rows = ((d.get("data") or {}).get("fastNewsList")) or []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        st = str(r.get("showTime") or "").strip().replace(" ", "T")
        out.append({
            "id": f"em:{r.get('code')}",
            "source": "东财7×24",
            "published_at": st,
            "title": str(r.get("title") or "").strip(),
            "digest": str(r.get("summary") or "").strip(),
            "url": "",
            "important": int(r.get("titleColor") or 0) > 0,
            "raw_tags": [],
            "raw_stocks": r.get("stockList") or [],
        })
    return out


def archive_path(day: str | None = None) -> Path:
    d = str(day or _today8()).replace("-", "")[:8]
    return paths.live_dir() / d / "news.jsonl"


def load_persisted(day: str | None = None) -> list[dict[str, Any]]:
    p = archive_path(day)
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    except Exception:  # noqa: BLE001
        return []
    return out


def persist_items(items: list[dict[str, Any]], day: str | None = None) -> int:
    """按标题+id去重后原子写入当日 news.jsonl，保留历史已抓取条目。"""
    if not items:
        return 0
    existing = load_persisted(day)
    by_key: dict[str, dict[str, Any]] = {}
    for x in existing + items:
        k = re.sub(r"\s+", "", str(x.get("title") or ""))[:40] or str(x.get("id") or "")
        if k and k not in by_key:
            by_key[k] = x
    merged = sorted(by_key.values(), key=lambda x: str(x.get("published_at") or ""), reverse=True)[:300]
    p = archive_path(day)
    if not process_guard.can_write("news_feed.persist_items", p):
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.stem + ".tmp.jsonl")
    tmp.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in merged) + "\n",
        encoding="utf-8",
    )
    tmp.replace(p)
    return len(merged)


def aggregate_news(items: list[dict[str, Any]], *,
                   cutoff_ts: str | None = None,
                   theme_stocks: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """将结构化快讯聚合为「题材催化表（附对应活跃A股）」与「个股利空/利好预警表」（严格因果）。"""
    c2n, _ = _stock_name_maps()
    tmap = theme_stocks or {}
    filtered = [
        x for x in items
        if not cutoff_ts or not x.get("published_at") or str(x["published_at"]) <= cutoff_ts
    ]
    by_theme: dict[str, dict[str, Any]] = {}
    by_stock: dict[str, dict[str, Any]] = {}
    macro_hits: list[dict[str, Any]] = []
    macro_kw_seen: list[str] = []

    for it in filtered:
        sc = int(it.get("score") or 0)
        sent = str(it.get("sentiment") or "neutral")
        txt_all = f"{it.get('title') or ''} {it.get('digest') or ''}"
        m_kws = [kw for kw in MACRO_MARKET_BULL_KEYWORDS if kw in txt_all]
        if m_kws and sent != "danger":
            for kw in m_kws:
                if kw not in macro_kw_seen:
                    macro_kw_seen.append(kw)
            macro_hits.append({
                "t": str(it.get("published_at") or "")[11:16],
                "title": str(it.get("title") or "")[:90],
                "keywords": m_kws[:3],
                "score": max(1, sc),
            })
        is_cooling = bool(it.get("is_clarification_cooling"))
        for th in it.get("themes") or []:
            if is_cooling:
                # 异动降温/澄清否认公告绝不当作该题材的正面催化
                continue
            row = by_theme.setdefault(th, {
                "theme": th, "pos_n": 0, "neg_n": 0, "danger_n": 0,
                "score": 0, "headlines": [], "mapped_stocks": tmap.get(th, [])[:5],
            })
            if sent == "positive":
                row["pos_n"] += 1
            elif sent == "danger":
                row["danger_n"] += 1
                row["neg_n"] += 1
            elif sent == "negative":
                row["neg_n"] += 1
            row["score"] += sc
            if len(row["headlines"]) < 3:
                row["headlines"].append({
                    "t": str(it.get("published_at") or "")[11:16],
                    "title": it.get("title"),
                    "sentiment": sent,
                })

        for c in it.get("codes") or []:
            sr = by_stock.setdefault(c, {
                "ts_code": c, "name": c2n.get(c, ""),
                "risk_level": "neutral",
                "is_clarification_cooling": False,
                "danger_tags": [], "headlines": [],
            })
            if is_cooling:
                sr["is_clarification_cooling"] = True
            if sent == "danger":
                sr["risk_level"] = "danger"
                for dh in it.get("danger_hits") or []:
                    if dh not in sr["danger_tags"]:
                        sr["danger_tags"].append(dh)
            elif sent == "negative" and sr["risk_level"] == "neutral":
                sr["risk_level"] = "warn"
            elif sent == "positive" and sr["risk_level"] == "neutral":
                sr["risk_level"] = "positive"
            if len(sr["headlines"]) < 3:
                sr["headlines"].append({
                    "t": str(it.get("published_at") or "")[11:16],
                    "title": it.get("title"),
                    "sentiment": sent,
                })

    theme_list = []
    for row in by_theme.values():
        s = row["score"]
        row["direction"] = (
            "danger" if row["danger_n"] > 0 and s < 0
            else ("bullish" if s >= 2 else ("bearish" if s <= -2 else "neutral"))
        )
        theme_list.append(row)
    theme_list.sort(key=lambda x: (-abs(x["score"]), -(x["pos_n"] + x["neg_n"])))

    # 高信噪比精选快讯：优先展示带雷区、带题材/个股匹配或重点加红的条目，并补齐关联股名与题材前排股
    def _rank(x: dict[str, Any]) -> tuple[int, str]:
        prio = 0
        if x.get("sentiment") == "danger":
            prio += 4
        if x.get("important"):
            prio += 2
        if x.get("themes") or x.get("codes"):
            prio += 2
        if x.get("sentiment") in ("positive", "negative"):
            prio += 1
        return (prio, str(x.get("published_at") or ""))

    highlights = []
    for raw_it in sorted(filtered, key=_rank, reverse=True)[:20]:
        it = dict(raw_it)
        if not it.get("stocks") and it.get("codes"):
            it["stocks"] = [{"ts_code": c, "name": c2n.get(c, "")} for c in it["codes"]]
        # 若快讯命中了板块但未直接点名个股，附带该板块当日活跃 A 股前 3 只
        rel: list[dict[str, Any]] = []
        seen_c = set(it.get("codes") or [])
        for th in it.get("themes") or []:
            for st in tmap.get(th, [])[:3]:
                if st["ts_code"] not in seen_c:
                    seen_c.add(st["ts_code"])
                    rel.append(st)
                    if len(rel) >= 3:
                        break
            if len(rel) >= 3:
                break
        it["theme_stocks"] = rel
        highlights.append(it)
    highlights.sort(key=lambda x: str(x.get("published_at") or ""), reverse=True)

    macro_score = sum(int(x.get("score") or 1) for x in macro_hits)
    macro_active = bool(len(macro_hits) >= 2 or macro_score >= 3)
    macro_policy_alert = {
        "active": macro_active,
        "n_hits": len(macro_hits),
        "score": macro_score,
        "keywords": macro_kw_seen[:8],
        "headlines": macro_hits[:5],
        "note": (
            f"🚨 全市场重磅政策利好爆发（命中 {len(macro_hits)} 条：{'、'.join(macro_kw_seen[:5])}）"
            if macro_active else "无全市场级突发重磅政策"
        ),
    }

    return {
        "n_total": len(filtered),
        "n_danger": sum(1 for x in filtered if x.get("sentiment") == "danger"),
        "n_positive": sum(1 for x in filtered if x.get("sentiment") == "positive"),
        "macro_policy_alert": macro_policy_alert,
        "theme_catalysts": theme_list,
        "catalyst_by_theme": {r["theme"]: r for r in theme_list},
        "stock_alerts": by_stock,
        "direct_stocks": sorted(
            by_stock.values(),
            key=lambda x: (0 if x["risk_level"] == "danger" else 1 if x["risk_level"] == "positive" else 2, x["ts_code"]),
        ),
        "highlights": highlights,
    }


def clear_cache() -> None:
    global _MEM_CACHE
    with _LOCK:
        _MEM_CACHE = None


_NEWS_BURST_UNTIL: float = 0.0


def trigger_burst_mode(duration_sec: float = 900.0) -> None:
    """期货/宽基先异动突拉或命中重磅政策时，将快讯切入 3 秒极速轮询档。"""
    global _NEWS_BURST_UNTIL
    with _LOCK:
        _NEWS_BURST_UNTIL = max(_NEWS_BURST_UNTIL, time.monotonic() + float(duration_sec))


def _effective_news_ttl() -> float:
    """动态变频 TTL：常规盘中/午间(09:15-15:05) 10s；命中重磅政策或市场先异动突拉切极速 3s；盘后 120s。"""
    try:
        from backend import market_clock
        tz = market_clock.TZ_CN
        now_cn = datetime.now(tz) if tz else datetime.now()
        hm = now_cn.strftime("%H%M")
        in_session = market_clock.is_trading_day("CN", now=now_cn) and "0915" <= hm <= "1505"
    except Exception:  # noqa: BLE001
        in_session = False
    if not in_session:
        return _TTL_SEC
    if time.monotonic() < _NEWS_BURST_UNTIL:
        return 3.0
    if _MEM_CACHE is not None and (_MEM_CACHE[1].get("macro_policy_alert") or {}).get("active"):
        return 3.0
    return 10.0


def snapshot(*, force: bool = False, save: bool = True,
             day: str | None = None, cutoff_ts: str | None = None) -> dict[str, Any]:
    """拉取并结构化国内 7×24 政策/财经快讯（动态变频缓存：盘中/午间20s、重磅政策8s、盘后120s）。"""
    global _MEM_CACHE
    now_m = time.monotonic()
    eff_ttl = _effective_news_ttl()
    with _LOCK:
        if not force and cutoff_ts is None and _MEM_CACHE is not None and now_m - _MEM_CACHE[0] < eff_ttl:
            return dict(_MEM_CACHE[1])

    raw_items: list[dict[str, Any]] = []
    sources_ok: list[str] = []
    errors: list[str] = []
    try:
        ths = _fetch_ths(35)
        if ths:
            raw_items.extend(ths)
            sources_ok.append("同花顺7×24")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"ths:{type(exc).__name__}")
    try:
        em = _fetch_em(25)
        if em:
            raw_items.extend(em)
            sources_ok.append("东财7×24")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"em:{type(exc).__name__}")

    classified: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for r in raw_items:
        c = classify_item(r)
        tk = re.sub(r"\s+", "", c["title"])[:36]
        if not tk or tk in seen_titles:
            continue
        seen_titles.add(tk)
        classified.append(c)

    if save and classified:
        try:
            persist_items(classified, day=day)
        except Exception:  # noqa: BLE001
            pass

    # 合并当日已落盘快讯，并按最新规则重新归类（确保旧归档不残留误判雷区或泛词错配）
    disk_items = load_persisted(day)
    merged_src = disk_items if disk_items else classified
    all_items = [classify_item(x) for x in merged_src]
    if save and all_items:
        try:
            # 用修正后的结构覆盖更新当日落盘
            p = archive_path(day)
            if process_guard.can_write("news_feed.snapshot", p):
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(p.stem + ".tmp.jsonl")
                tmp.write_text(
                    "\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in all_items) + "\n",
                    encoding="utf-8",
                )
                tmp.replace(p)
        except Exception:  # noqa: BLE001
            pass

    if not all_items:
        return {
            "available": False,
            "day": str(day or _today8()),
            "sources": sources_ok,
            "errors": errors,
            **aggregate_news([]),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    tmap = build_theme_stock_map(day)
    agg = aggregate_news(all_items, cutoff_ts=cutoff_ts, theme_stocks=tmap)
    snap = {
        "available": True,
        "day": str(day or _today8()),
        "sources": sources_ok or ["本地留档"],
        "errors": errors,
        "theme_stock_map": tmap,
        **agg,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if cutoff_ts is None:
        with _LOCK:
            _MEM_CACHE = (now_m, snap)
    return dict(snap)
