"""题材池（同花顺三池）采集 · 真实数据，零合成，零回填。

为什么要有这个模块
==================
2026-11-16 实测确认：我们此前用的 `feeds.industry_map()` 是**东财行业分类**
（`水泥` / `一般零售` / `通信服务`），那是**分类归属，不是题材**。
项目里 `leader_casebook.py:209` 早就写过这句话：

> 行业是分类归属，不是「题材」；题材需消息面/概念标签，仓库没有历史标签。

同花顺公开接口提供了真正的题材：`reason_type = "AI漫剧+剧集发行+北京国资"`。

数据源（三池同一套字段，一套解析）
================================
    GET https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool          涨停池
    GET https://data.10jqka.com.cn/dataapi/limit_up/open_limit_pool       炸板池
    GET https://data.10jqka.com.cn/dataapi/limit_up/continuous_limit_pool 连板池

参数：`date=YYYYMMDD&page=N&limit=100&filter=HS,GEM2STAR&field=<位号>`

实测结论（2026-11-16，详见 `docs/题材数据源与实时刷新架构-2026-11-16.md`）
======================================================================
1. **口径与参考图逐位一致**：11-13 涨停 37 / 跌停 33 / 炸板 21 / 封板率 64%，
   `limit_up_count.today` 完全吻合 → 参考图用的就是这套数据。
2. **历史边界 2025-11-03**：更早返回 0 行（实测 2025-10-31 及以前全空）。
3. **`limit=100` 是硬上限**：`limit>100` 直接返回空列表，必须 100 一页翻。
4. **「N 板」是区间涨停次数，不是连续连板**（用户 2026-11-16 拍板：用原生写法）。
   解码规则：`high_days_value // 65536` = 板数，`% 65536` = 天数。
   与本地 `feature_engine.streak` 吻合度仅 18.9%~25.0%，**不可混用**。
5. **`reason_type` 存在解析陷阱**：标签可能被 `“…”` 包住且内含 `+`，
   朴素 `split("+")` 会把 `“AI+零售”` 切成 `“AI` / `零售”`。
   6 个交易日实测 27 行不一致 → 必须用 `split_tags()` 的引号感知解析。

数据纪律（DATA-TRUTH-001）
=========================
- **观测 / 缺失 可区分**：接口失败 → 该日整表缺失，写 `_missing.json` 记录原因，
  **不写空表冒充「当天零涨停」**；
- **追加不改写**：一天一个文件 `themes/{YYYYMMDD}.parquet`，重跑同日写 `.revision-N`；
- **不插值**：某天没抓到就是没有，不做前后向填充；
- **不猜字段**：字段缺失升 `DataShapeError`，不默默跳过。

用法
====
    from backend import theme_pool
    df = theme_pool.collect("20261116")          # 抓取（网络）
    df = theme_pool.load("2026-11-16")           # 读已落盘
    theme_pool.collect_range("2026-11-03", "2026-11-16")   # 批量补
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import polars as pl

from backend import paths

# ---------------------------------------------------------------------------
# 常量（集中可审计）
# ---------------------------------------------------------------------------

BASE = "https://data.10jqka.com.cn/dataapi/limit_up"

#: 三个池。key = 落盘用的池名，value = URL 路径。
POOLS: dict[str, str] = {
    "zt": "limit_up_pool",           # 涨停
    "zb": "open_limit_pool",         # 炸板（触板未封住）
    "lianban": "continuous_limit_pool",  # 连板
}

#: `field` 位号串。实测缺任何一个都会导致对应字段为 null。
FIELDS = ("199112,10,9001,330323,330324,330325,9002,330329,133971,133970,"
          "1968584,3475914,9004,9005")

#: limit 硬上限（实测 >100 返回空）
PAGE_LIMIT = 100
MAX_PAGES = 5           # 单池最多翻 5 页 = 500 只；实测最大日 200 上下

#: 历史边界（实测：2025-10-31 及更早返回 0 行）
HISTORY_START = "2025-11-03"

#: 请求头。`Referer` 必需，否则 403。
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Referer": "https://data.10jqka.com.cn/",
}

#: 落库列（显式 schema，不靠推断 —— 教训见 踩坑与复利 坑2）
SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Utf8,            # YYYY-MM-DD
    "pool": pl.Utf8,            # zt / zb / lianban
    "code": pl.Utf8,            # 6 位
    "name": pl.Utf8,
    "reason_raw": pl.Utf8,      # reason_type 原文
    "tags": pl.List(pl.Utf8),   # 引号感知切分后的标签
    "high_days_raw": pl.Utf8,   # "6天5板" 原生写法（用户 2026-11-16 拍板保留）
    "n_board": pl.Int32,        # 板数 = hdv // 65536
    "n_days": pl.Int32,         # 天数 = hdv % 65536
    "limit_up_type": pl.Utf8,   # 换手板 / T字板 / 一字板
    "order_amount_yi": pl.Float64,   # 封单额（亿）
    "open_num": pl.Int32,       # 开板次数
    "turnover_rate": pl.Float64,
    "first_seal": pl.Utf8,      # 首次封板时刻 HH:MM:SS
    "last_seal": pl.Utf8,
    "change_tag": pl.Utf8,      # LIMIT_BACK 等
    "change_rate": pl.Float64,
}


class DataShapeError(RuntimeError):
    """接口返回结构不符合预期。宁可报错，不猜。"""


# ---------------------------------------------------------------------------
# 标签分类（T/F/A）—— 决定「能不能当题材行」
# ---------------------------------------------------------------------------
# 为什么必须有这一步：实测 23 个交易日、1831 个唯一标签里，
# `三季报增长` 出现 161 次（第一高频！）、`三季报扭亏` 32 次、`央企` 14 次。
# 不分类直接成行，矩阵第一行就是「三季报增长 52 只」——那是业绩榜，不是题材榜。
#
# 分类结果（`tools/_probe_theme_classify.py` 实测，Top300 标签 1237 槽位）：
#   T 题材/概念 36.5%  → 进题材轴
#   F 业绩/事件 14.7%  → 独立因子，**绝不成行**
#   A 属性       7.6%  → 过滤标签，**绝不成行**
#   ? 未分类    41.2%  → 落入「其他」，不单独成行（避免 200 行碎片）

#: F 类：业绩 / 事件 / 资本运作。命中即不是题材。
_RE_F = re.compile(
    r"三季报|半年报|年报|一季报|季报|业绩|扭亏|减亏|高增|预增|大增|"
    r"净利|增长|营收|"
    r"控制权变更|实控人变更|资产重组|重大重组|重组|并购|定增|可转债|"
    r"股份回购|回购注销|回购|股权激励|摘帽|退市|分红|送转|"
    r"拟收购|拟投|签约|中标|订单|涨价|提价|扩产|投产|"
    r"减持|增持|质押|解禁|H股|港股|分拆|借壳|复牌|股权转让|停牌"
)

#: A 类：属性 / 归属 / 资金风格。命中即不是题材。
_RE_A = re.compile(
    r"国资|央企|国企|地方国资|省国资|市国资|控股$|"
    r"次新股|次新|注册制|低价股|小盘|超跌|破净|高股息|"
    r"指数成分|沪深300|中证|国证|"
    r"融资融券|两融|沪股通|深股通|北向|MSCI|转债标的|"
    r"字辈|谐音|“东方”|“东方系”"
)


def classify_tag(tag: str) -> str:
    """标签三分类：`T` 题材 / `F` 业绩事件 / `A` 属性。

    >>> classify_tag("三季报增长")
    'F'
    >>> classify_tag("成都国资")
    'A'
    >>> classify_tag("AI漫剧")
    'T'
    """
    t = (tag or "").strip()
    if not t:
        return "A"
    if _RE_A.search(t):
        return "A"
    if _RE_F.search(t):
        return "F"
    return "T"


def pick_theme_tag(tags: list[str], axis_map: dict) -> tuple[str, str, bool, str]:
    """从一只票的标签里选出「主题材」。

    优先级（这是本模块的核心判定，别改顺序）：
      1. **命中人工映射表**的标签，取第一个 → 最可靠
      2. 否则取第一个 **T 类**标签（跳过业绩/属性）→ 长尾也能归位
      3. 都没有 → 返回 `("其他", "其他", False, "no_theme")`，**不单独成行**

    返回 (key, 展示名, 是否命中映射表, 原因)。

    >>> amap = {"AI漫剧": {"key": "ai_media", "name": "AI传媒"}}
    >>> pick_theme_tag(["三季报增长", "AI漫剧"], amap)[:3]
    ('ai_media', 'AI传媒', True)
    >>> pick_theme_tag(["三季报增长", "央企"], amap)[:3]
    ('other', '其他', False)
    """
    tags = [t for t in (tags or []) if t]
    for t in tags:
        hit = axis_map.get(t)
        if hit:
            return hit["key"], hit["name"], True, "axis"
    for t in tags:
        if classify_tag(t) == "T":
            # 未命中映射表的 T 类：用自身名，但归到 other 组避免碎片
            return "other", "其他", False, "longtail"
    return "other", "其他", False, "no_theme"



# ---------------------------------------------------------------------------
# 解析（纯函数，可单测，不联网）
# ---------------------------------------------------------------------------

#: 引号感知的标签切分。两类引号后缀要被整体吃掉：
#:   `“羊”字辈`  → 标签是「“羊”字辈」，不是「“羊”」+「字辈」
#:   `“东方系”`   → 整段就是一个标签
#:   `“AI+零售”`  → 引号内的 `+` 不能当分隔符
_TAG_RE = re.compile(r"“[^”]*”字辈|“[^”]*”系|“[^”]*”|[^+“”]+")


def split_tags(reason_raw: str | None) -> list[str]:
    """把 `reason_type` 切成标签列表（引号感知）。

    实测对照（6 个交易日）：
        朴素 split("+")   → 916 个标签
        引号感知          → 941 个标签（正确）
        不一致 27 行，全部是引号内含 `+` 或 `“…”字辈` 被切坏。

    >>> split_tags('“AI+零售”+超市调改+央企')
    ['“AI+零售”', '超市调改', '央企']
    >>> split_tags('“羊”字辈+低温乳制品')
    ['“羊”字辈', '低温乳制品']
    >>> split_tags(None)
    []
    """
    if not reason_raw:
        return []
    return [t.strip() for t in _TAG_RE.findall(str(reason_raw)) if t.strip()]


def parse_high_days(hd: str | None, hdv: Any = None) -> tuple[int, int, str]:
    """解析 `high_days` → (板数, 天数, 原生写法)。

    ⚠️ 用户 2026-11-16 拍板：**保留 `6天5板` 原生写法**，不用连续连板 streak。
    原因：与参考图/同花顺口径一致；连续 streak 会把「文投控股 6天5板」显示成 2 板。

    两种形态（实测穷举，另有一种空字符串）：
        "首板"   → (1, 1, "首板")
        "6天5板" → (5, 6, "6天5板")
        ""/None  → (0, 0, "—")      ← 炸板池的 high_days 恒为 null

    解码交叉校验：`high_days_value // 65536 == 板数`、`% 65536 == 天数`。
    两者不一致时**以 hdv 为准并标记**（hdv 是机器可读源，hd 是展示串）。

    >>> parse_high_days("6天5板", 327686)
    (5, 6, '6天5板')
    >>> parse_high_days("首板", 65537)
    (1, 1, '首板')
    >>> parse_high_days(None, None)
    (0, 0, '—')
    """
    s = str(hd or "").strip()
    if not s:
        return (0, 0, "—")
    if s.startswith("首板"):
        return (1, 1, "首板")
    m = re.match(r"^(\d+)天(\d+)板$", s)
    if not m:
        # 形态未知：不猜，返回 0 并保留原文，由调用方按「未识别」处理
        return (0, 0, s)
    days, board = int(m.group(1)), int(m.group(2))
    # hdv 交叉校验（存在则优先）
    try:
        if hdv is not None:
            n = int(hdv)
            b2, d2 = n // 65536, n % 65536
            if b2 and (b2, d2) != (board, days):
                return (b2, d2, s)
    except (TypeError, ValueError):
        pass
    return (board, days, s)


def _hhmmss(v: Any) -> str | None:
    """同花顺 `first_limit_up_time` 是 Unix 秒；转 HH:MM:SS。失败返回 None。"""
    try:
        return datetime.fromtimestamp(int(v)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def _row(x: dict, day: str, pool: str) -> dict[str, Any]:
    """单行原始记录 → 落库行。字段缺失按 schema 的 null 处理，不猜。"""
    raw = x.get("reason_type")
    board, days, hd_native = parse_high_days(x.get("high_days"),
                                             x.get("high_days_value"))
    amt = x.get("order_amount")
    return {
        "date": day,
        "pool": pool,
        "code": str(x.get("code") or "").zfill(6),
        "name": x.get("name"),
        "reason_raw": raw,
        "tags": split_tags(raw),
        "high_days_raw": hd_native,
        "n_board": board,
        "n_days": days,
        "limit_up_type": x.get("limit_up_type"),
        "order_amount_yi": (round(float(amt) / 1e8, 4)
                            if isinstance(amt, (int, float)) else None),
        "open_num": (int(x["open_num"])
                     if isinstance(x.get("open_num"), (int, float)) else None),
        "turnover_rate": (float(x["turnover_rate"])
                          if isinstance(x.get("turnover_rate"), (int, float))
                          else None),
        "first_seal": _hhmmss(x.get("first_limit_up_time")),
        "last_seal": _hhmmss(x.get("last_limit_up_time")),
        "change_tag": x.get("change_tag"),
        "change_rate": (float(x["change_rate"])
                        if isinstance(x.get("change_rate"), (int, float))
                        else None),
    }


def to_frame(rows: list[dict]) -> pl.DataFrame:
    """显式 schema 构造（禁止推断 —— 空字符串/null 列会推断崩）。"""
    if not rows:
        return pl.DataFrame(schema=SCHEMA)
    return pl.DataFrame(rows, schema=SCHEMA, orient="row")


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def _fetch_page(pool: str, day8: str, page: int,
                session=None) -> tuple[list[dict], dict]:
    """抓一页。返回 (info 列表, data 元信息)。失败抛异常，不吞。"""
    import requests

    url = f"{BASE}/{POOLS[pool]}"
    params = {
        "page": page, "limit": PAGE_LIMIT, "field": FIELDS,
        "filter": "HS,GEM2STAR", "order_field": "330324",
        "order_type": "0", "date": day8,
    }
    get = (session or requests).get
    r = get(url, params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    j = r.json()
    if "data" not in j:
        raise DataShapeError(f"{pool} 返回缺 data 字段: {list(j.keys())}")
    d = j.get("data") or {}
    info = d.get("info")
    if info is None:
        info = []
    if not isinstance(info, list):
        raise DataShapeError(f"{pool} info 不是 list: {type(info)}")
    return info, {k: v for k, v in d.items() if k != "info"}


def collect(day: str | date, *, pools: Iterable[str] = tuple(POOLS),
            save: bool = True, session=None,
            meta_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """抓某一天的全部池并落盘。

    Parameters
    ----------
    day : "2026-11-16" / "20261116" / date
    meta_extra : 写进 `_manifest.json` 当日条目的附加字段。盘中实时采集传
        `{"intraday": True, "snapshot_at": ...}`，让「盘中快照」与
        「收盘定格」在留痕里可区分（同一天后续 upsert 会覆盖为最后一次）。

    Returns
    -------
    dict  含 rows / per_pool / meta（limit_up_count 等官方口径）/ saved 路径

    Raises
    ------
    DataShapeError  接口结构异常（不静默降级）
    RuntimeError    网络失败（调用方决定是否重试）
    """
    d = _norm_day(day)
    day8 = d.replace("-", "")
    all_rows: list[dict] = []
    per_pool: dict[str, Any] = {}
    metas: dict[str, Any] = {}

    for p in pools:
        rows_p: list[dict] = []
        meta: dict = {}
        for page in range(1, MAX_PAGES + 1):
            info, meta = _fetch_page(p, day8, page, session=session)
            for x in info:
                rows_p.append(_row(x, d, p))
            if len(info) < PAGE_LIMIT:
                break
            time.sleep(0.5)          # 翻页限速
        per_pool[p] = len(rows_p)
        metas[p] = meta
        all_rows.extend(rows_p)
        time.sleep(0.4)

    df = to_frame(all_rows)
    out: dict[str, Any] = {
        "date": d, "per_pool": per_pool,
        "n_rows": df.height,
        "meta": _meta_digest(metas),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    if save:
        # 存量更新：官方数据没变就不写盘（返回 unchanged=True）
        out.update(upsert(df, d, save=True, meta_extra=meta_extra))
    return out


def _meta_digest(metas: dict[str, Any]) -> dict[str, Any]:
    """从官方 meta 里抽走「可以直接用」的口径值（不做二次计算）。

    `limit_up_count.today` 结构：
        {"num": 32, "history_num": 48, "rate": 0.6667, "open_num": 16}
        num = 收盘涨停家数；history_num = 曾涨停家数；rate = 封板率；open_num = 炸板
    """
    zt = (metas.get("zt") or {}).get("limit_up_count") or {}
    dt = (metas.get("zt") or {}).get("limit_down_count") or {}
    t = zt.get("today") or {}
    td = dt.get("today") or {}
    status = (metas.get("zt") or {}).get("trade_status") or {}
    return {
        "zt_num": t.get("num"),
        "zt_history_num": t.get("history_num"),
        "seal_rate": t.get("rate"),
        "zb_open_num": t.get("open_num"),
        "dt_num": td.get("num"),
        "dt_history_num": td.get("history_num"),
        "trade_status": status.get("name"),
        "source": "ths_pool",
    }


def _norm_day(day: str | date) -> str:
    if isinstance(day, date):
        return day.isoformat()
    s = str(day).strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    raise ValueError(f"无法识别的日期: {day!r}")


# ---------------------------------------------------------------------------
# 落盘 / 读取（存量更新 · 单文件）
# ---------------------------------------------------------------------------
# 2026-11-16 重构：从「一天一个文件」改为「单文件 + 按天 upsert」。
#
# 为什么改（用户要求：存量更新，不能每天重拉重存浪费空间）
# ------------------------------------------------------
# 旧实现的三个浪费，实测数据：
#   1. 一天一个 parquet：25 个文件 343KB / 2,522 行 = **136 B/行**。
#      单文件 zstd 只需 **47.8 B/行**（120KB）→ 合并可省 **64.9%**。
#      原因是每个小 parquet 都有独立的 footer / schema / row-group 元数据。
#   2. `revision-N` 存的是**完整副本**：20261116 有 2 个文件 = 当天数据存两遍。
#      正确做法是只存「变了什么」的差异摘要，不存整份数据。
#   3. `collect()` 每次全量拉三池 + 整表重写；`collect_range(skip_existing=True)`
#      只跳过已有日期，既不补残缺也不识别「官方事后修订」。
#
# 新架构：
#   data/themes/themes.parquet        唯一数据文件（zstd，按 (date,pool,code) 幂等 upsert）
#   data/themes/_manifest.json        每日指纹（行数 + content hash），判断要不要重拉
#   data/themes/_revisions.jsonl      更新留痕（**只记差异摘要**，不存数据）
#
# 幂等键 = (date, pool, code)。upsert 语义：
#   - 同 key 已存在 → 用新行覆盖（官方修订会被吸收）
#   - 新 key → 追加
#   - 旧数据里该日有、新数据里没有 → **删除**（官方撤下说明那只票不成立）
#   整个过程只重写一次文件，不会产生第二份数据。

THEMES_NAME = "themes.parquet"
MANIFEST_NAME = "_manifest.json"
REVISIONS_NAME = "_revisions.jsonl"


def themes_dir() -> Path:
    return paths.data_dir() / "themes"


def themes_file() -> Path:
    """唯一数据文件。"""
    return themes_dir() / THEMES_NAME


def manifest_file() -> Path:
    return themes_dir() / MANIFEST_NAME


def revisions_file() -> Path:
    return themes_dir() / REVISIONS_NAME


# ---------------------------------------------------------------- 元数据

def _read_manifest() -> dict[str, Any]:
    p = manifest_file()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_manifest(m: dict[str, Any]) -> None:
    p = manifest_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(p)


def _append_revision(rec: dict[str, Any]) -> None:
    """差异留痕（一行一条）。只存摘要与变动代码，不存整份数据。"""
    p = revisions_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _fingerprint(df_day: pl.DataFrame) -> str:
    """单日数据的稳定指纹（用于判断「官方有没有修订」）。

    只对业务列做 hash，不含抓取时间 —— 否则每次抓取指纹都变，判断失效。
    """
    cols = ["pool", "code", "reason_raw", "high_days_raw", "limit_up_type",
            "order_amount_yi", "open_num", "turnover_rate", "change_rate"]
    use = [c for c in cols if c in df_day.columns]
    if not use or df_day.height == 0:
        return "empty"
    s = df_day.select(use).sort(["pool", "code"]).to_dicts()
    raw = json.dumps(s, ensure_ascii=False, sort_keys=True, default=str)
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 读写

def _read_all() -> pl.DataFrame:
    """读全表（缺失返回空表，不是 None）。"""
    p = themes_file()
    if not p.is_file():
        return pl.DataFrame(schema=SCHEMA)
    try:
        return pl.read_parquet(p)
    except Exception:  # noqa: BLE001
        return pl.DataFrame(schema=SCHEMA)


def upsert(df_new: pl.DataFrame, day: str, *, source: str = "ths_pool",
           save: bool = True, meta_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """把某一天的抓取结果**存量更新**进单文件（幂等）。

    Returns
    -------
    {"added": n, "updated": n, "removed": n, "unchanged": bool, "path": ...}

    `unchanged=True` 时**不写盘** —— 官方数据没变就一个字节都不动。
    """
    d = _norm_day(day)
    if df_new.height == 0:
        return {"added": 0, "updated": 0, "removed": 0,
                "unchanged": True, "reason": "no rows", "path": None}

    cur = _read_all()
    old_day = (cur.filter(pl.col("date") == d) if cur.height
               else pl.DataFrame(schema=SCHEMA))

    fp_new = _fingerprint(df_new)
    m = _read_manifest()
    prev = m.get(d) or {}
    if prev.get("fp") == fp_new and prev.get("rows") == df_new.height:
        # 数据没变不重写 parquet，但抓取时刻仍要更新：
        # 盘中每 60 秒的实时采集若内容不变，界面需要知道「刚抓过」。
        m.setdefault(d, {}).update(
            {"fetched_at": datetime.now().isoformat(timespec="seconds")})
        if meta_extra:
            m[d].update(meta_extra)
        _write_manifest(m)
        return {"added": 0, "updated": 0, "removed": 0,
                "unchanged": True, "fp": fp_new, "path": str(themes_file())}

    # 幂等键 (pool, code)
    key = ["pool", "code"]
    old_keys = {(r["pool"], r["code"]) for r in old_day.select(key).to_dicts()}
    new_keys = {(r["pool"], r["code"]) for r in df_new.select(key).to_dicts()}
    added = len(new_keys - old_keys)
    removed = len(old_keys - new_keys)
    updated = len(new_keys & old_keys)

    if not save:
        return {"added": added, "updated": updated, "removed": removed,
                "unchanged": False, "dry_run": True, "path": None}

    # 去掉该日旧行，拼上新行 —— 单表单次原子重写
    rest = cur.filter(pl.col("date") != d) if cur.height else cur
    merged = (pl.concat([rest.select(cur.columns), df_new.select(cur.columns)],
                        how="vertical_relaxed")
              if rest.height else df_new.select(cur.columns))
    merged = merged.sort(["date", "pool", "code"])

    dst = themes_file()
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".tmp.parquet")
    merged.write_parquet(tmp, compression="zstd")     # zstd：47.8 B/行
    tmp.replace(dst)

    m[d] = {"fp": fp_new, "rows": df_new.height,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "source": source}
    if meta_extra:
        m[d].update(meta_extra)
    _write_manifest(m)
    _append_revision({
        "at": datetime.now().isoformat(timespec="seconds"),
        "date": d, "source": source,
        "added": added, "updated": updated, "removed": removed,
        "rows": df_new.height, "fp": fp_new, "prev_fp": prev.get("fp"),
    })
    return {"added": added, "updated": updated, "removed": removed,
            "unchanged": False, "fp": fp_new, "rows": merged.height,
            "path": str(dst)}


#: 兼容旧名（外部脚本可能还在调）
save_day = upsert


def load(day: str | date) -> pl.DataFrame:
    """读某天题材表。走谓词下推，不加载整表。缺失返回空表（**不是 None**）。"""
    d = _norm_day(day)
    p = themes_file()
    if not p.is_file():
        return pl.DataFrame(schema=SCHEMA)
    try:
        return (pl.scan_parquet(p)
                .filter(pl.col("date") == d)
                .collect())
    except Exception:  # noqa: BLE001
        return pl.DataFrame(schema=SCHEMA)


def load_range(start: str, end: str) -> pl.DataFrame:
    s, e = _norm_day(start), _norm_day(end)
    p = themes_file()
    if not p.is_file():
        return pl.DataFrame(schema=SCHEMA)
    return (pl.scan_parquet(p)
            .filter((pl.col("date") >= s) & (pl.col("date") <= e))
            .collect())


def available_days() -> list[str]:
    """已落盘的所有日期（升序）。"""
    m = _read_manifest()
    if m:
        return sorted(m)
    p = themes_file()
    if not p.is_file():
        return []
    s = pl.scan_parquet(p).select(pl.col("date").unique().sort()).collect()
    return [str(x) for x in s["date"].to_list()]


def stats() -> dict[str, Any]:
    """存储用量与新鲜度（给 /api/health 和每日巡检看）。"""
    p = themes_file()
    if not p.is_file():
        return {"available": False, "reason": "题材表未建立"}
    size = p.stat().st_size
    df = _read_all()
    days = available_days()
    m = _read_manifest()
    rev_n = 0
    rp = revisions_file()
    if rp.is_file():
        rev_n = sum(1 for _ in rp.open(encoding="utf-8"))
    return {
        "available": True,
        "file": str(p),
        "bytes": size,
        "rows": df.height,
        "bytes_per_row": round(size / df.height, 1) if df.height else None,
        "days": len(days),
        "date_min": days[0] if days else None,
        "date_max": days[-1] if days else None,
        "last_fetch": (m.get(days[-1]) or {}).get("fetched_at") if days else None,
        "revisions_logged": rev_n,
        "note": "单文件 zstd 存量更新；revision 只记差异摘要，不存副本",
    }


# ---------------------------------------------------------------------------
# 批量补数
# ---------------------------------------------------------------------------

def collect_range(start: str, end: str, *, skip_existing: bool = True,
                  force: bool = False, pause: float = 1.2,
                  log=print) -> dict[str, Any]:
    """按区间逐日补数（只补交易日）。

    存量更新语义（2026-11-16 重构）
    ------------------------------
    `skip_existing=True`（默认）：清单里已有该日 → **不重拉**。这是省流量的主路径。
    `force=True`：无视清单，逐日重拉一遍 → 走 upsert，只把真正变化的部分写回；
                  官方没变的那天 `unchanged=True`，**一个字节都不写**。
    `skip_existing=False` 且 `force=False`：等价于 force（但仍会跳过非交易日）。

    失败**不中断**：逐日记录 ok/failed，最后汇总。缺的那天就是缺，不填。
    """
    s, e = _norm_day(start), _norm_day(end)
    cal = _trading_days()
    days = [d for d in cal if s <= d <= e]
    have = set(available_days())
    ok, skipped, failed, unchanged = [], [], [], []
    for d in days:
        if skip_existing and not force and d in have:
            skipped.append(d)
            continue
        try:
            r = collect(d, save=True)
            if r["n_rows"] == 0:
                failed.append({"date": d, "reason": "0 rows（非交易日或早于历史边界）"})
                log(f"  {d}  EMPTY")
            elif r.get("unchanged"):
                unchanged.append(d)
                log(f"  {d}  UNCHANGED（官方未修订，未写盘）")
            else:
                ok.append({"date": d, "n": r["n_rows"],
                           "per_pool": r["per_pool"],
                           "added": r.get("added"), "updated": r.get("updated"),
                           "removed": r.get("removed")})
                log(f"  {d}  OK {r['n_rows']} 行 "
                    f"(+{r.get('added')} ~{r.get('updated')} -{r.get('removed')})")
        except Exception as ex:  # noqa: BLE001
            failed.append({"date": d, "reason": f"{type(ex).__name__}: {ex}"})
            log(f"  {d}  FAIL {type(ex).__name__}")
        time.sleep(pause)
    return {"start": s, "end": e, "n_days": len(days),
            "ok": ok, "skipped": skipped, "unchanged": unchanged,
            "failed": failed, "storage": stats()}


def _trading_days(end: str | None = None) -> list[str]:
    """交易日列表（用于批量补数）。

    ⚠️ 2026-11-16 修正：旧实现**只读 `panel_raw`**，而面板停在 11-12
    （BUG-1 导致 `panel_live` 合并不进来），于是 `collect_range` 永远看不到
    11-13 / 11-16，`force=True` 也补不到 —— 这是"实时链路断在上游"的同一根因。

    新口径：**三源并集**，取长不看短
      1. `panel_raw` 的交易日（权威，但可能滞后）
      2. 已落盘题材表的日期（说明那天确实抓到过数据）
      3. `end`（含今天）之间的**工作日**（法定节假日无法离线判断，
         但接口对非交易日返回 0 行，由 `collect_range` 记 failed 而非记 0 涨停）

    这样即使面板滞后，今天仍会被尝试抓取；抓不到就是抓不到，不伪造。
    """
    import polars as pl

    days: set[str] = set()
    p = paths.panel_raw_file()
    if p.is_file():
        s = pl.scan_parquet(p).select(pl.col("trade_date").unique()).collect()
        days.update(str(x) for x in s["trade_date"].to_list())
    days.update(available_days())

    hi = _norm_day(end) if end else datetime.now().date().isoformat()
    if days:
        lo = min(days)
        d = datetime.fromisoformat(lo).date()
        stop = datetime.fromisoformat(hi).date()
        while d <= stop:
            if d.weekday() < 5:          # 周一到周五；节假日交给接口返回空
                days.add(d.isoformat())
            d += timedelta(days=1)
    else:
        days.add(hi)
    return sorted(d for d in days if d <= hi)


# ---------------------------------------------------------------------------
# 题材轴聚合（L2 派生，可随 yaml 版本重算）
# ---------------------------------------------------------------------------

AXIS_FILE = "theme_axis.yaml"


def axis_path() -> Path:
    return paths.data_dir().parent / "configs" / AXIS_FILE


def load_axis() -> dict[str, Any]:
    """读人工映射表。缺失返回空表 + 标记（调用方必须降级，不许当成"没有题材"）。"""
    p = axis_path()
    if not p.is_file():
        return {"available": False, "reason": f"缺少 {p}", "axes": {}, "version": 0}
    import yaml
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    axes = {}
    for a in raw.get("axes") or []:
        for t in a.get("match") or []:
            axes[t] = {"key": a["key"], "name": a.get("name") or a["key"]}
    return {"available": True, "version": raw.get("version", 0),
            "axes": axes, "raw": raw}


def ladder_matrix(day: str | date, *, axis: dict | None = None) -> dict[str, Any]:
    """题材 × 身位 矩阵（参考图 3 的数据结构）。

    「身位」用 `n_board`（区间板数，用户拍板），展示用 `high_days_raw` 原生串。

    结构判定（这是参考图 3 最核心的算法，不是打分）：
        conn        = 1..K 层每层都有人 → 梯队连贯
        missing     = 缺失的层
        top_heavy   = 最高板 ≥4 且只有 1 只、且中间层几乎没人 → 只有龙头没有兵

    Returns
    -------
    {"available", "as_of", "rows":[{...}], "gaps":[...], "axis_version"}
    """
    df = load(day)
    if df.height == 0:
        return {"available": False, "as_of": _norm_day(day),
                "reason": "该日题材表未落盘", "rows": [],
                "hint": "先跑 backend.theme_pool.collect()"}
    zt = df.filter(pl.col("pool") == "zt")
    ax = axis or load_axis()
    amap = ax.get("axes") or {}

    rows_by: dict[str, dict] = {}
    longtail: list[str] = []      # 未命中映射的 T 类标签（人工复核队列）
    for r in zt.iter_rows(named=True):
        key, name, matched, why = pick_theme_tag(r["tags"], amap)
        if why == "longtail":
            # 记录长尾标签，供人工把高频的补进 yaml
            longtail.extend(t for t in (r["tags"] or [])
                            if classify_tag(t) == "T" and t not in amap)
        b = int(r["n_board"] or 0)
        slot = rows_by.setdefault(key, {
            "theme": name, "key": key, "matched": matched,
            "h1": 0, "h2": 0, "h3": 0, "h4": 0, "h5p": 0,
            "total": 0, "max_board": 0, "codes": [], "high_days": [],
        })
        if b <= 0:
            continue
        slot["total"] += 1
        slot["codes"].append(r["code"])
        slot["high_days"].append(r["high_days_raw"])
        slot["max_board"] = max(slot["max_board"], b)
        if b == 1:
            slot["h1"] += 1
        elif b == 2:
            slot["h2"] += 1
        elif b == 3:
            slot["h3"] += 1
        elif b == 4:
            slot["h4"] += 1
        else:
            slot["h5p"] += 1

    out = []
    for slot in rows_by.values():
        K = slot["max_board"]
        missing = [k for k in range(2, K + 1)
                   if {2: slot["h2"], 3: slot["h3"], 4: slot["h4"]}
                   .get(k, slot["h5p"] if k >= 5 else 0) == 0]
        conn = not missing
        below = sum(v for k, v in ((1, slot["h1"]), (2, slot["h2"]),
                                   (3, slot["h3"]), (4, slot["h4"]))
                    if k < K) + (slot["h5p"] if K >= 5 else 0)
        top_heavy = bool(K >= 4 and below <= 1)
        out.append({**slot, "conn": conn, "missing": missing,
                    "top_heavy": top_heavy})
    out.sort(key=lambda x: (-x["max_board"], -x["total"]))
    from collections import Counter
    lc = Counter(longtail)
    return {
        "available": True,
        "as_of": _norm_day(day),
        "axis_version": ax.get("version"),
        "axis_available": ax.get("available", False),
        "matched_rows": sum(1 for x in out if x["matched"]),
        "n_themes": len(out),
        "n_zt": zt.height,
        "rows": out,
        # 长尾复核队列：出现 ≥2 次的未映射 T 类标签，直接抄进 yaml 即可
        "longtail_top": [{"tag": t, "n": v} for t, v in lc.most_common(20)],
        "gaps": ([] if ax.get("available")
                 else ["题材轴映射表缺失：所有票落进「其他」，矩阵无意义"]),
    }


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    res = collect(arg)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    m = ladder_matrix(arg)
    print(f"\n题材矩阵：{m.get('n_themes')} 行 / {m.get('n_zt')} 只涨停")
    for x in (m.get("rows") or [])[:12]:
        hd = "/".join(x["high_days"][:3])
        flag = ("梯队连贯" if x["conn"]
                else f"断层:缺{x['missing']}")
        if x["top_heavy"]:
            flag += " ·只有龙头没有兵"
        print(f"  {x['theme'][:16]:18} 首{x['h1']} 2板{x['h2']} 3板{x['h3']} "
              f"4板{x['h4']} 5+{x['h5p']} 合计{x['total']:2d} "
              f"最高{x['max_board']} [{hd}] → {flag}")
