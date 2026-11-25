"""实时行情与市场情绪数据源（腾讯批量 + 东方财富涨停池）。

设计要点（对应本次两项优化）
----------------------------
1. **批量行情替代逐股请求**：腾讯 `qt.gtimg.cn` 单次可拉 200 只，
   全市场 5,580 只 ≈ 28 次 HTTP，2~4 秒完成；项目原 `daily_collect`
   逐股 akshare（5,580 次、约 5~10 分钟）在盘前不可用。
2. **面板化历史缓存**：日线历史只在首次读取 5,580 个 parquet，
   之后落一个 `panel_raw.parquet`（长表），增量追加当日 bar；
   扫描信号时一次 `scan_parquet` 代替 5,580 次小文件 I/O。

所有网络失败都返回空/降级结果，绝不阻塞页面；调用方通过 `health` 判断新鲜度。
"""
from __future__ import annotations

from backend import limit_counts, paths

import threading
import time
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Iterable

import polars as pl
import requests

ROOT = Path(__file__).resolve().parents[1]
# 数据与代码分离：数据根统一由 backend.paths 解析（支持 WUKONG_DATA_HOME）。
DATA = paths.data_dir()
DATA.mkdir(parents=True, exist_ok=True)

# 项目既有全市场日线缓存（只读，不修改原项目）
PROJECT_CACHE = paths.MARKET_CACHE  # 由 paths.py 统一解析

TX_BATCH = 180
#: 腾讯批量行情的并发批数（2026-11-14 多线程改造）。
#: 实测：串行 6.47s / 4 线程 1.50s / 6 线程 1.12s / 8 线程 0.99s / 12 线程 0.73s。
#: 取 6 是安全与收益的平衡点：8/12 只再快 0.13~0.39s，但腾讯 WAF 在
#: 并发 12 时会返回 HTTP 501 并冷却 >10 分钟（踩坑日志坑 19）。
#: 调高前必须重跑 tools/_concurrency_probe.py 验证零失败。
TX_CONCURRENCY = 6
TX_URL = "http://qt.gtimg.cn/q="
EM_ZT = "https://push2ex.eastmoney.com/getTopicZTPool"
EM_ZB = "https://push2ex.eastmoney.com/getTopicZBPool"
EM_DT = "https://push2ex.eastmoney.com/getTopicDTPool"
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

_lock = threading.RLock()
_cache: dict[str, tuple[float, Any]] = {}
_panel_mem: tuple[float, pl.DataFrame] | None = None


def _cached(key: str, ttl: float, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def tx_symbol(code: str) -> str:
    """6 位代码 -> 腾讯行情 symbol。

    ⚠️ 2026-11-13 修复：北交所 `920xxx` 段曾被判成 `sh`。
    原实现 `if c.startswith(("6", "9")): return "sh" + c` 把 `920000` 归到上海，
    但 920 是**北交所新段**（原 43/83/87 迁移而来）。后果：
    腾讯对 `sh920000` 返回 `v_pv_none_match="1"`，`_parse_tx_line` 判 `len(p) < 50`
    返回 None，**整段 355 只在实时快照里静默消失**（全市场 5,580 -> 实得 5,225）。
    现在按交易所段位显式判定，北交所优先，不再让 "9" 前缀兜底。
    """
    c = str(code).zfill(6)
    # 北交所：43/83/87 老三板转板段 + 920 新段。必须先判，否则 920 会被 "9" 吃掉。
    if c.startswith(("43", "83", "87", "92")):
        return "bj" + c
    # 上交所：60x 主板、688/689 科创板、9xx B 股
    if c.startswith("6") or c.startswith("9"):
        return "sh" + c
    # 深交所：000/001/002/003 主板、300/301/302 创业板
    if c.startswith(("0", "3")):
        return "sz" + c
    return "bj" + c


def _num(parts: list[str], i: int, default: float | None = None) -> float | None:
    try:
        v = parts[i]
        if v in ("", "-", "~"):
            return default
        return float(v)
    except (IndexError, TypeError, ValueError):
        return default


def _parse_tx_line(line: str) -> dict[str, Any] | None:
    if "=" not in line or '"' not in line:
        return None
    body = line.split('"', 1)[1].rsplit('"', 1)[0]
    p = body.split("~")
    if len(p) < 50:
        return None
    code = p[2]
    price = _num(p, 3)
    if price is None or price <= 0:
        return None
    return {
        "ts_code": code,
        "name": p[1],
        "price": price,
        "prev_close": _num(p, 4),
        "open": _num(p, 5),
        # 与项目 daily_raw 口径一致：成交量单位=手（不是股）
        "volume": (_num(p, 36) or 0.0),
        "amount": (_num(p, 37) or 0.0) * 1e4,          # 万 -> 元
        "high": _num(p, 33),
        "low": _num(p, 34),
        "pct": _num(p, 32),
        "change": _num(p, 31),
        "turnover": _num(p, 38),
        "float_mv": (_num(p, 44) or 0.0) * 1e8,
        "total_mv": (_num(p, 45) or 0.0) * 1e8,
        "limit_up": _num(p, 47),
        "limit_down": _num(p, 48),
        # 五档盘口只取一档：判定「封死涨跌停」用（2026-11-21 统一涨跌停口径）。
        # 跌停封死 = 买一为空；涨停封死 = 卖一为空。
        "bid1": _num(p, 9),
        "bid1_vol": _num(p, 10),
        "ask1": _num(p, 19),
        "ask1_vol": _num(p, 20),
        "quote_time": p[30] if len(p) > 30 else "",
    }


def _tx_get(symbols: Iterable[str], timeout: float = 8.0) -> list[dict[str, Any]]:
    q = ",".join(symbols)
    r = requests.get(TX_URL + q, timeout=timeout, headers=_UA)
    r.encoding = "gbk"
    out: list[dict[str, Any]] = []
    for line in r.text.split(";"):
        v = _parse_tx_line(line)
        if v:
            out.append(v)
    return out


SINA_URL = "https://hq.sinajs.cn/list="
_SINA_UA = {
    "User-Agent": _UA["User-Agent"],
    "Referer": "https://finance.sina.com.cn/",
}


def _parse_sina_line(line: str) -> dict[str, Any] | None:
    """解析新浪实时行情行（作为腾讯 WAF/超时时的真实热备源）。"""
    if '="' not in line:
        return None
    left, right = line.split('="', 1)
    body = right.rsplit('"', 1)[0]
    if not body:
        return None
    sym = left.rsplit("_", 1)[-1].strip()
    code = sym[2:] if len(sym) >= 8 else sym
    p = body.split(",")
    if len(p) < 32:
        return None
    try:
        price = float(p[3])
        prev_close = float(p[2])
        op = float(p[1])
        high = float(p[4])
        low = float(p[5])
        vol_shares = float(p[8])
        amt = float(p[9])
        bid1_vol = float(p[10]) / 100.0
        bid1 = float(p[11])
        ask1_vol = float(p[20]) / 100.0
        ask1 = float(p[21])
    except (TypeError, ValueError, IndexError):
        return None
    if price <= 0:
        return None
    pct = round((price - prev_close) / prev_close * 100.0, 2) if prev_close > 0 else None
    qt_date = str(p[30]).replace("-", "")
    qt_time = str(p[31]).replace(":", "")
    qt = f"{qt_date}{qt_time}" if len(qt_date) == 8 and len(qt_time) == 6 else ""
    return {
        "ts_code": str(code).zfill(6),
        "name": p[0],
        "price": price,
        "prev_close": prev_close if prev_close > 0 else None,
        "open": op if op > 0 else None,
        "volume": vol_shares / 100.0,
        "amount": amt,
        "high": high if high > 0 else price,
        "low": low if low > 0 else price,
        "pct": pct,
        "change": round(price - prev_close, 4) if prev_close > 0 else None,
        "turnover": None,
        "float_mv": 0.0,
        "total_mv": 0.0,
        "limit_up": None,
        "limit_down": None,
        "bid1": bid1,
        "bid1_vol": bid1_vol,
        "ask1": ask1,
        "ask1_vol": ask1_vol,
        "quote_time": qt,
        "feed_source": "sina",
    }


def _sina_get(symbols: Iterable[str], timeout: float = 6.0) -> list[dict[str, Any]]:
    syms = [s for s in symbols if s]
    if not syms:
        return []
    r = requests.get(SINA_URL + ",".join(syms), timeout=timeout, headers=_SINA_UA)
    r.encoding = "gbk"
    out: list[dict[str, Any]] = []
    for line in r.text.splitlines():
        v = _parse_sina_line(line)
        if v:
            out.append(v)
    return out


def batch_quotes(codes: Iterable[str], *, ttl: float = 5.0,
                 force: bool = False) -> dict[str, dict[str, Any]]:
    """重点池小批量实时行情（≤180 只单次 HTTP，~50ms；腾讯主源 + 新浪热备）。

    用于持仓股/候选股/个股图表实时刷新，避免调用 all_quotes() 拉取全市场 5580 只。
    """
    norm = sorted({str(c).strip().zfill(6) for c in codes if str(c).strip()})
    if not norm:
        return {}
    key = "batch_" + ",".join(norm)
    if force:
        with _lock:
            _cache.pop(key, None)

    def _fetch() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(norm), TX_BATCH):
            chunk = norm[i:i + TX_BATCH]
            syms = [tx_symbol(c) for c in chunk]
            rows: list[dict[str, Any]] = []
            try:
                rows = _tx_get(syms, timeout=5.0)
                for row in rows:
                    row.setdefault("feed_source", "tencent")
            except Exception:  # noqa: BLE001
                rows = []
            got_codes = {str(r["ts_code"]).zfill(6) for r in rows}
            missing = [c for c in chunk if c not in got_codes]
            if missing:
                try:
                    # 腾讯缺失或 501 限流时自动切新浪真实行情热备
                    sina_rows = _sina_get([tx_symbol(c) for c in missing], timeout=5.0)
                    rows.extend(sina_rows)
                except Exception:  # noqa: BLE001
                    pass
            for r in rows:
                out[str(r["ts_code"]).zfill(6)] = r
        return out

    return _cached(key, ttl, _fetch)


def single_quote(code: str, *, ttl: float = 5.0,
                 force: bool = False) -> dict[str, Any] | None:
    """单只股票实时行情（~50ms；腾讯主源 + 新浪热备）。"""
    c = str(code).strip().zfill(6)
    if not c.isdigit():
        return None
    got = batch_quotes([c], ttl=ttl, force=force)
    return got.get(c)


def clear_caches() -> None:
    """清空 feeds 全部内存缓存（供 /api/refresh 调用）。"""
    global _panel_mem, _CAL_DAYS
    with _lock:
        _cache.clear()
        _panel_mem = None
        _CAL_DAYS = None


def stock_list() -> pl.DataFrame:
    """项目缓存里的全市场清单（5,580 只）。"""
    f = PROJECT_CACHE / "meta" / "stock_list.parquet"
    df = pl.read_parquet(f)
    return df.unique(subset=["ts_code"]).sort("ts_code")


def all_quotes(*, force: bool = False) -> pl.DataFrame:
    """全市场最新快照（腾讯批量，180 只/次，含北交所）。

    2026-11-13 收紧：增加**逐批对账**与**全市场覆盖率对账**。

    为什么必须加：原实现对每批做 3 次重试，失败后 `pass` 静默跳过，
    只有「全部批次都空」才抛错。实测 920 段 355 只从未被成功解析，
    但系统一直显示「正常」，广度 n=5,225 被当成全市场（真实 5,580）。
    覆盖率缺口 6.4% 会系统性压低涨停/跌停家数与涨跌比。

    现在：每次返回都带 `coverage` 与 `missing_codes`，覆盖率 < 阈值时
    `degraded=True`，由 `/api/health` 与前端显式提示，而不是当无事发生。

    2026-11-14 多线程改造（审计 P0 性能项）：
    31 批原本**串行**拉取，每批还额外 sleep 0.04s，实测 6.3s。
    改为 `ThreadPoolExecutor(6)` 并发后 1.1s（**5.8× 加速**）。

    **并发度为什么是 6 不是 8/12**（实测数据，切勿随意调高）：
    | 线程数 | 全量外推 | 失败数 |
    |---|---|---|
    | 串行   | 6.47s   | 0 |
    | 4      | 1.50s   | 0 |
    | 6      | 1.12s   | 0 |
    | 8      | 0.99s   | 0 |
    | 12     | 0.73s   | 0 |
    8/12 更快，但腾讯有 WAF：历史实测并发 12 跑 ~300 只后返回
    **HTTP 501 + 跳转页**，冷却 >10 分钟（见踩坑日志坑 19）。
    6 线程留出安全余量，同时已经拿到 5.8× 的绝大部分收益
    （8 线程只再多 0.13s，不值得多一倍封禁风险）。

    **保留串行重试**：单批失败时依次重试 3 次（退避 0.35/0.70/1.05s），
    并发只用于「批与批之间」，失败重试仍在单个批次内串行，
    避免失败批次被并发放大成请求风暴。
    """
    def _fetch() -> pl.DataFrame:
        base = stock_list()
        codes = base["ts_code"].to_list()
        names = dict(zip(codes, base["name"].to_list()))
        chunks = [codes[i:i + TX_BATCH] for i in range(0, len(codes), TX_BATCH)]

        failed_chunks: list[list[str]] = []

        def _one(chunk: list[str]) -> tuple[list[dict[str, Any]], bool]:
            """单批拉取（含串行退避重试）。返回 (rows, ok)。"""
            for attempt in range(3):
                try:
                    got = _tx_get(tx_symbol(c) for c in chunk)
                    return got, True
                except Exception:
                    time.sleep(0.35 * (attempt + 1))
            return [], False

        rows: list[dict[str, Any]] = []
        # 并发拉取，但 map 保证结果按 chunks 顺序返回 → 拼装顺序确定，
        # 下游 DataFrame 内容与串行版完全一致（可用 diff 验证）。
        with ThreadPoolExecutor(max_workers=TX_CONCURRENCY) as pool:
            for chunk, (got, ok) in zip(chunks, pool.map(_one, chunks)):
                if ok:
                    rows.extend(got)
                else:
                    failed_chunks.append(chunk)

        if not rows:
            raise RuntimeError("腾讯批量行情全部失败")
        df = pl.DataFrame(rows)
        # 腾讯对科创板返回的 volume 是「股」，其余多为「手」；统一成「手」，
        # 否则 688/689 的量比会虚高 100 倍。判别式同 build_panel。
        df = df.with_columns(
            pl.when(
                (pl.col("amount") > 0) & (pl.col("volume") > 0) & (pl.col("price") > 0)
                & ((pl.col("amount") / (pl.col("volume") * pl.col("price"))) < 10)
            ).then(pl.col("volume") / 100).otherwise(pl.col("volume")).alias("volume")
        )
        # 用项目清单里的规范名称覆盖腾讯 GBK 名称（避免生僻字乱码）
        df = df.with_columns(
            pl.col("ts_code").replace_strict(names, default=None).alias("name_cn")
        ).with_columns(
            pl.coalesce([pl.col("name_cn"), pl.col("name")]).alias("name")
        ).drop("name_cn")
        df = df.with_columns(
            pl.Series("snapshot_at", [datetime.now().isoformat(timespec="seconds")] * df.height)
        )
        return df

    if force:
        _cache.pop("quotes", None)
    # TTL 10s（2026-11-13 性能审计：4s 导致每 4 秒全量重拉 31 批 = 6.8s 冷启。
    # 盘中实时性由 SSE 每 5s 推 overview 保证；分析接口 10s 内复用同一快照，
    # 各 payload 冷启动从 ~9s 降到 ~5s。force=True 仍可立即刷新。
    df = _cached("quotes", 10.0, _fetch)
    return _annotate_coverage(df)


#: 覆盖率低于该比例即视为降级（留 2% 容忍停牌/退市/新股未同步）
COVERAGE_MIN = 0.98


def _annotate_coverage(df: pl.DataFrame) -> pl.DataFrame:
    """给快照挂上覆盖率审计列（不改行数、不改数值）。

    实现为「读时注解」而不是「抓取时注解」：缓存里只存原始帧，
    这样每次调用都能按当前 stock_list 重新对账，不会把旧覆盖率缓存下来。
    """
    if df is None or df.height == 0:
        return df
    try:
        universe = stock_list()["ts_code"].to_list()
    except Exception:
        return df
    have = set(df["ts_code"].to_list())
    missing = [c for c in universe if c not in have]
    expected = len(universe)
    got = df.height
    coverage = round(got / expected, 4) if expected else 0.0
    # 缺票按板块分组，便于一眼看出是不是某个交易所整体挂了
    by_board: dict[str, int] = {}
    for c in missing:
        b = "BJ" if c.startswith(("43", "83", "87", "92")) else (
            "SH" if c.startswith("6") else "SZ")
        by_board[b] = by_board.get(b, 0) + 1
    return df.with_columns([
        pl.lit(got).alias("quote_n"),
        pl.lit(expected).alias("quote_expected"),
        pl.lit(coverage).alias("quote_coverage"),
        pl.lit(coverage < COVERAGE_MIN).alias("quote_degraded"),
        pl.lit(len(missing)).alias("quote_missing_n"),
    ]).with_columns(
        pl.lit("|".join(f"{k}:{v}" for k, v in sorted(by_board.items())) or "")
        .alias("quote_missing_by_board")
    )


def _em_pool(url: str, kind: str, date_str: str | None = None) -> pl.DataFrame:
    def _fetch() -> pl.DataFrame:
        d = date_str or datetime.now().strftime("%Y%m%d")
        # 2026-11-21 修复（涨跌停口径统一）：`sort` 字段各池不同。
        # 跌停池没有 `fbt`（首次封板时间）字段，传 `fbt:asc` 时接口返回
        # HTTP 200 + `tc=58` 但 `pool=[]`，导致留档里 dt 帧长期为 0。
        # 实测：dt 只认 `fund:asc` / `zdp:asc`；zt/zb 用 `fbt:asc`。
        sort_key = "fund:asc" if kind == "dt" else "fbt:asc"
        params = {
            "ut": EM_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 300,
            "sort": sort_key, "date": d,
        }
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, timeout=10, headers=_UA)
                j = r.json()
                pool = (j.get("data") or {}).get("pool") or []
                if not pool:
                    return pl.DataFrame()
                rows = []
                for x in pool:
                    # 2026-11-14 修复（审计 2.9）：空代码 zfill 会变 "000000"
                    raw_c = str(x.get("c") or "").strip()
                    if not raw_c or not raw_c.isdigit():
                        continue
                    rows.append({
                        "ts_code": raw_c.zfill(6),
                        "name": x.get("n", ""),
                        # 东财涨停池 p 的单位是「厘」（6530 -> 6.53），不是分
                        "price": (x.get("p") or 0) / 1000.0,
                        "pct": x.get("zdp"),
                        "amount": x.get("amount"),
                        "float_mv": x.get("ltsz"),
                        "total_mv": x.get("tshare"),
                        "turnover": x.get("hs"),
                        "streak": x.get("lbc"),
                        "first_seal": x.get("fbt"),
                        "last_seal": x.get("lbt"),
                        "seal_fund": x.get("fund"),
                        "broken": x.get("zbc"),
                        "industry": x.get("hybk", ""),
                        "stat": x.get("zttj"),
                        "kind": kind,
                        "trade_date": str((j.get("data") or {}).get("qdate", d)),
                    })
                return pl.DataFrame(rows)
            except Exception:
                time.sleep(0.4 * (attempt + 1))
        return pl.DataFrame()

    return _cached(f"pool_{kind}_{date_str or 'today'}", 25.0, _fetch)


def zt_pool(date_str: str | None = None) -> pl.DataFrame:
    return _em_pool(EM_ZT, "zt", date_str)


def zb_pool(date_str: str | None = None) -> pl.DataFrame:
    return _em_pool(EM_ZB, "zb", date_str)


def dt_pool(date_str: str | None = None) -> pl.DataFrame:
    return _em_pool(EM_DT, "dt", date_str)


# ---------------------------------------------------------------------------
# 历史面板（优化 1：一次性扫描 + 增量落盘）
# ---------------------------------------------------------------------------

def _panel_path() -> Path:
    """优先返回带后复权列的面板（panel_adj），缺失时回退 panel_raw。

    2026-11-13 收紧：**只读**。该路径现在是「历史面板」的唯一定义，
    运行期不得写入。写入路径已拆到 `panel_live_file`。
    """
    adj = paths.panel_adj_file()
    if adj.is_file():
        return adj
    return paths.panel_raw_file()


def build_panel(*, force: bool = False) -> pl.DataFrame:
    """读取历史面板（已收盘的完整行情），收盘后自动合并 panel_live 只读视图。

    职责边界（2026-11-13 明确划死；2026-11-13 晚追加合并视图）
    ----------------------------------------------------------
    1. 本函数**只读，绝不写入**任何面板文件。重建仍是离线动作；
    2. **合并视图（新增）**：panel_raw 只由离线脚本重建，存在「重建断点」——
       收盘后当日真实日线只落在 `panel_live.parquet`（15:05 闸门），
       若无人跑离线重建，`panel_raw` 就停在 T-1。
       下游（悟空真经 B1 情绪闸门/排板结算/次日候选）全部只读历史面板 → 每天都要手工补。
       现在收盘后（15:05 且当日行齐）自动把 panel_live 的当日行**只读合并进
       内存视图**，历史文件一个字节都不动。三重护栏：
         a) 只合并「当日 == panel_raw 最大日 + 1」的行（防跳日/防重复日）；
         b) 只在 15:05 之后合并（盘中半日 bar 不污染收盘价）；
         c) 合并行数 ≥ 覆盖率 60%（防行情源残缺日）才合并，否则放弃并留痕。
    3. 新鲜度：panel_raw / panel_live / daily_raw 的 mtime 变化都会使缓存失效。

    为什么拆开：旧实现在这里做 `panel.write_parquet(out)`，而 `out` 可能是
    `panel_adj`（含复权列），也可能是实时拼出来的帧 —— 于是「重建」与
    「实时落盘」共用了同一条写入路径，直接造成 5,218 行假 bar 污染历史库。
    现在：**读历史面板的地方不能写它**，写入只有 `panel_live_file` 一处。

    新鲜度：同时看 `daily_raw` 与 `daily_hfq` 两个源的 mtime。
    只盯 raw 会出现「面板日期 11-12、复权源只到 11-10」的错配且永不自愈。
    """
    global _panel_mem
    out = _panel_path()
    if not out.is_file():
        return pl.DataFrame()

    live_path = paths.panel_live_file()
    live_mtime = live_path.stat().st_mtime if live_path.is_file() else 0.0
    mtime = (out.stat().st_mtime, live_mtime)
    if _panel_mem is not None and _panel_mem[0] == mtime and not force:
        return _panel_mem[1]

    df = pl.read_parquet(out)
    df = _merged_with_live_view(df, live_path)
    _panel_mem = (mtime, df)
    return df


# 合并视图的护栏参数（集中可审计）
LIVE_MERGE_MIN_COVERAGE = 0.60   # 当日行数 / 昨日行数 下限，低于则放弃合并
LIVE_MERGE_AFTER = dtime(15, 5)  # 只在收盘闸门后合并（与 _persist_live_bar 一致）

#: 交易日历缓存（升序 "YYYY-MM-DD"）。合并闸门用它区分「缺交易日」与「跨周末」。
_CAL_DAYS: list[str] | None = None


def _trading_days() -> list[str]:
    """真实交易日历（升序 `YYYY-MM-DD`）；读不到返回空列表（调用方降级，不猜）。

    数据源：`live_terminal/data/market_cache/meta/calendar.parquet`（含未来
    交易日，因此当天/次日的 live bar 也能定位）。与
    `recommend._trading_days()` 同源；这里单独一份是为了避开
    `feeds ← recommend` 的导入环（recommend 依赖面更宽）。

    为什么不能再用「自然日差」：2026-11-12(四) → 2026-11-16(一) 自然日差
    **4 天**，其中只缺 11-13 一个交易日，11-14/15 是周末。旧实现用自然日差
    当 gap，把「周末」也算成缺口 → 合法的跨周末续接被拒（2026-11-16 事故）。
    """
    global _CAL_DAYS
    if _CAL_DAYS is not None:
        return _CAL_DAYS
    days: list[str] = []
    try:
        if PROJECT_CACHE is not None:
            cal = Path(PROJECT_CACHE) / "meta" / "calendar.parquet"
            if cal.is_file():
                col = pl.read_parquet(cal, columns=["trade_date"])["trade_date"]
                days = sorted({str(x)[:10] for x in col.unique().to_list()})
    except Exception:  # noqa: BLE001
        days = []
    _CAL_DAYS = days
    return days


def _merged_with_live_view(df: pl.DataFrame, live_path: Path) -> pl.DataFrame:
    """把 panel_live 里比历史面板更晚的交易日**逐日**合并进内存视图（不改盘）。

    2026-11-16 修复（BUG-1，实测事故）
    ---------------------------------
    旧实现只看 `live["trade_date"].max()` 一天，并且用**自然日差**当 gap：

        gap_days = (live_day - raw_last).days      # 11-16 - 11-12 = 4
        if gap_days > 3: return df                 # → 整批拒绝

    于是当 panel_raw 停在 T-2 而 panel_live 已累积两天（11-13 + 11-16）时，
    max 那天 gap=4 直接拒绝，**两天全丢**，`build_panel()` 永远到不了 11-16，
    悟空真经 B1 情绪闸门/排板结算/题材轴等下游全部只看到 11-12。

    新语义（更严格，不是放宽）
    --------------------------
    1. 按时间顺序**逐日**尝试，任何一天不满足就停（绝不跳日拼接）；
    2. gap 用**交易日历**判定：live_day 必须恰好是当前面板最后一日的
       「下一个交易日」，周末/节假日自然跳过；缺中间交易日仍然拒绝
       （那说明面板需要离线重建，不能靠 live 硬补）；
    3. 覆盖率护栏保留（当日行数 / 面板最近一日行数 ≥ 60%）；
    4. 只对「当日尚未收盘」的 live bar 做 15:05 时钟闸门；历史交易日已收盘，
       任何时刻都可合并（保留 2026-11-16 的 `now.date()` 同源修复）；
    5. 仍然**只读**：返回新 DataFrame，绝不写任何面板文件。

    返回新 DataFrame；没有任何一天满足条件时原样返回。
    """
    if not live_path.is_file() or df.height == 0:
        return df
    try:
        live = pl.read_parquet(live_path)
    except Exception:  # noqa: BLE001
        return df
    if live.height == 0:
        return df
    now = datetime.now()
    cal_idx = {d: i for i, d in enumerate(_trading_days())}
    live_days = live["trade_date"].unique().sort().to_list()
    merged = df
    for live_day in live_days:
        if not isinstance(live_day, date):
            try:
                live_day = date.fromisoformat(str(live_day)[:10])
            except (TypeError, ValueError):
                break
        cur_last = merged["trade_date"].max()
        if cur_last is None or live_day <= cur_last:
            continue   # 面板已有该日（防重复追加 / 防回填旧日）
        # 时钟闸门：当日未收盘的 live bar 不合并；历史交易日已收盘，任何时刻可并。
        if live_day >= now.date() and now.time() < LIVE_MERGE_AFTER:
            break
        # 护栏 a：必须是「当前面板最后一日的下一个交易日」。
        if cal_idx:
            i = cal_idx.get(str(cur_last)[:10])
            j = cal_idx.get(str(live_day)[:10])
            if i is None or j is None or j != i + 1:
                break
        else:
            # 无交易日历时退回自然日近似（旧护栏语义：只放行 T+1 / 周一补周五）。
            try:
                gap_days = (live_day - cur_last).days
            except TypeError:
                break
            if gap_days < 1 or gap_days > 3:
                break
            if gap_days > 1 and live_day.weekday() != 0:
                break
        live_one_day = live.filter(pl.col("trade_date") == live_day)
        if live_one_day.height == 0:
            continue
        # 护栏 c：覆盖率（当日行数 / 面板最近一日行数）
        prev_rows = merged.filter(pl.col("trade_date") == cur_last).height
        if prev_rows > 0 and live_one_day.height / prev_rows < LIVE_MERGE_MIN_COVERAGE:
            break
        # 列对齐：live 列缺哪些就补 null（历史面板若有复权列，live 行补 null——
        # 与 _persist_live_bar 的「live 行不带复权」口径一致）
        missing = [c for c in merged.columns if c not in live_one_day.columns]
        for c in missing:
            live_one_day = live_one_day.with_columns(
                pl.lit(None).cast(merged.schema[c]).alias(c))
        extra = [c for c in live_one_day.columns if c not in merged.columns]
        if extra:
            live_one_day = live_one_day.drop(extra)
        merged = pl.concat([merged, live_one_day.select(merged.columns)],
                           how="vertical_relaxed")
    if merged is df:
        return df
    return merged.unique(subset=["ts_code", "trade_date"], keep="last") \
                 .sort(["ts_code", "trade_date"])


def intraday_bar(*, force: bool = False) -> tuple[pl.DataFrame | None, str | None]:
    """**盘中实时 bar**（只读内存，绝不写盘）——供"看盘/展示"类模块把今天接进面板。

    2026-11-17 验收修复（用户现场质疑"盘中 10 点还显示昨天数据"）
    ---------------------------------------------------------------
    根因：`build_panel()` 有 15:05 收盘闸门，盘中不合成当日 bar；
    而悟空真经 B1 看板/题材轴等展示模块直接读日线面板，
    于是 10:17 的页面喂进去的仍是昨天（甚至 T-2）的面板 —— 算法都在，数据是旧的。

    用户硬规则（2026-11-17，统一由 market_clock 裁判）：
      R1 交易时段内绝不命中任何缓存 → `force=True` 时强制重拉实时快照；
      R2 非交易时段返回的必须是收盘定稿数据 → 这里盘中合成 bar 在非交易时段不返回；
      R3 用 A 股自己的交易时钟（含午休），不是"自然日/星期几"近似。

    本函数只**在内存**里构造当日行：
      - 只在 A 股交易时段返回；非交易时段 (None, None)；
      - 绝不写任何 parquet（历史上把半日 bar 写进 panel_adj 曾造成 5,218 行假 bar、
        中位收益 −58.5% 的污染事故，见模块顶部说明）；
      - 只供展示/结构分析使用；**收益/回测/冰点入池一律不得读它**。

    返回 (bar, quote_time)；bar 为与 panel 同构的当日逐票行（无复权列）。
    """
    from backend import market_clock

    if not market_clock.is_cn_trading():
        return None, None
    try:
        q = all_quotes(force=force)  # R1: 交易时段强制重拉，非交易时段沿用短 TTL
    except Exception:  # noqa: BLE001
        return None, None
    if q.height == 0:
        return None, None
    today_s = q["quote_time"].str.slice(0, 8).max()
    cn_today = datetime.now(market_clock.TZ_CN).strftime("%Y%m%d") \
        if market_clock.TZ_CN else datetime.now().strftime("%Y%m%d")
    if not today_s or str(today_s)[:8] != cn_today:
        return None, None          # 数据源不是今天（停牌日/假期/源故障）→ 不伪造
    trade_date = date(int(str(today_s)[:4]), int(str(today_s)[4:6]),
                      int(str(today_s)[6:8]))
    bar = q.filter((pl.col("open") > 0) & (pl.col("price") > 0)).select([
        pl.lit(trade_date).alias("trade_date"),
        pl.col("ts_code"),
        pl.col("open"), pl.col("high"), pl.col("low"),
        pl.col("price").alias("close"),
        pl.col("volume"), pl.col("amount"), pl.col("turnover"),
        pl.col("pct").alias("pct_chg"),
    ])
    snap = str(q["quote_time"].max())
    return bar, snap


_LIVE_SIG_SEQ = __import__("itertools").count()
_LIVE_BAR_MEM: dict[str, Any] = {"snap": None, "bar": None}


_LIVE_SIG_TLS = __import__("threading").local()


def live_signature() -> tuple:
    """盘中快照签名（用户规则 R1/R2/R3 的正确实现）。

    语义（2026-11-17 定稿）：
    - **同一 HTTP 请求（同一线程）内**：签名稳定 → 一次 payload 的 13 个块
      共享同一份面板视图（否则每块都重建 373 万行 → 页面 45 秒）。
    - **跨请求**：签名按 quote_time 的**秒**变化 → 下一秒的请求必定用新快照，
      任何磁盘/进程缓存都不可能跨秒返回旧判定（R1：盘中数据实时）。
    - 非交易时段：恒为 ('eod',) → 只应命中收盘定稿缓存（R2）。

    Flask threaded=True 时每个请求一个线程，用 thread-local 隔离请求是安全的。
    数据新鲜度由 quote_time（秒级）保证，不是由 TTL 或"每次递增"的伪唯一性保证。
    """
    from backend import market_clock

    if not market_clock.is_cn_trading():
        # R2 接线（2026-11-17 验收修复）：非交易时段只允许读「收盘定稿」缓存。
        # 此前 market_clock.make_cache_entry / readable / cache_allowed 全库
        # grep 不到生产调用 = R2 铁律形同虚设；现在这里实跑一遍：
        #   session_final() → make_cache_entry(final=...) → assert_readable()
        # 不可读时返回带 "nonfinal" 的签名，迫使上层重建，绝不把半日快照当收盘数据。
        entry = market_clock.make_cache_entry(
            {"sig": ("eod",)},
            final=market_clock.session_final("CN"))
        if market_clock.assert_readable(entry, market="CN"):
            return ("eod",)
        return ("eod", "nonfinal")
    # 同一线程（请求）内复用签名
    cached_sig = getattr(_LIVE_SIG_TLS, "sig", None)
    if cached_sig is not None:
        return cached_sig
    snap = None
    bar = None
    try:
        now_s = datetime.now().strftime("%Y%m%d%H%M%S")
        if _LIVE_BAR_MEM["snap"] and str(_LIVE_BAR_MEM["snap"])[:14] == now_s:
            bar, snap = _LIVE_BAR_MEM["bar"], _LIVE_BAR_MEM["snap"]
        else:
            bar, snap = intraday_bar(force=True)
            _LIVE_BAR_MEM.update(snap=snap, bar=bar)
    except Exception:  # noqa: BLE001
        pass
    if snap is None:
        sig = ("live", "no-snap", datetime.now().strftime("%Y%m%d%H%M%S"))
    else:
        sig = ("live", str(snap), bar.height if bar is not None else 0)
    _LIVE_SIG_TLS.sig = sig
    return sig


def _reset_live_sig() -> None:
    """请求结束时清 thread-local（供 Flask after_request 钩子调用）。"""
    if hasattr(_LIVE_SIG_TLS, "sig"):
        del _LIVE_SIG_TLS.sig


def current_intraday_bar() -> pl.DataFrame | None:
    """取 live_signature 同一时刻的 bar（避免重复 force 拉取）。"""
    return _LIVE_BAR_MEM.get("bar")


def _persist_live_bar(add: pl.DataFrame, trade_date: date) -> None:
    """把当日实时 bar 落到独立文件（原子替换 + 去重 + 不写复权列）。

    为什么要独立文件而不是就地改历史面板：
    历史面板一旦混入无复权行，下游多层都难以察觉；隔离后
    live 行与历史行物理分离，历史面板污染风险从根上消除。

    2026-11-20 修复两个数据安全问题：
    1. 旧实现读旧文件失败时 `except: pass` 后继续写 —— 等于**用今天的几行
       覆盖掉此前全部历史**（panel_live 是有价值的 11-13 起逐日留档）。
       现在读失败就放弃本次写入并留痕，下一轮刷新重试，绝不用残缺数据覆盖。
    2. 固定 `.tmp.parquet` 名在并发写时会互相截断；改为 pid+线程 id 唯一化。
    """
    path = paths.panel_live_file()
    tmp = path.with_name(
        f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp.parquet")
    try:
        fresh = add.drop([c for c in add.columns if c.endswith("_hfq") or c == "adj_factor"])
        if path.is_file():
            try:
                old = pl.read_parquet(path)
            except Exception as exc:  # noqa: BLE001
                # 不覆盖未知内容：保留旧文件原样，下一轮再试
                print(f"[feeds] panel_live 旧文件读取失败，跳过本次落盘以免覆盖历史: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                return
            # 只保留不同交易日的旧行，避免同一日重复累积
            old = old.filter(pl.col("trade_date") != trade_date)
            fresh = pl.concat([old, fresh], how="vertical_relaxed")
        fresh = fresh.unique(subset=["ts_code", "trade_date"], keep="last") \
            .sort(["ts_code", "trade_date"])
        fresh.write_parquet(tmp)
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        # 写失败只影响「明日回看」，不影响实时展示；但要留痕而不是静默
        print(f"[feeds] panel_live 落盘失败: {type(exc).__name__}: {exc}", flush=True)
    finally:
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass


def eod_bar_from_quotes(day: str, *,
                        quotes: pl.DataFrame | None = None
                        ) -> tuple[pl.DataFrame | None, str]:
    """从**当日最后一帧行情**构建与面板同构的收盘 bar（真实数据，不合成）。

    优先用传入的 quotes（盘中实时快照）；否则读 `data/live/<day>/quotes`
    最后一帧（收盘后/次日恢复时用）。行情时点必须属于 day，否则拒绝。
    """
    d = str(day).replace("-", "").replace("/", "")[:8]
    if quotes is None:
        # 2026-11-25 瘦身：逐帧 / 合并归档统一由 archive 抽象层读取。
        from backend import archive
        quotes = archive.latest_frame(d, "quotes")
        if quotes is None or quotes.height == 0:
            return None, f"{d} 无行情留档"
    if quotes is None or quotes.height == 0:
        return None, "行情为空"
    try:
        qt = str(quotes["quote_time"].max() or "")
    except Exception:  # noqa: BLE001
        return None, "行情无 quote_time"
    if qt[:8] != d:
        return None, f"行情时点 {qt} 不属于 {d}（不拿别的交易日冒充收盘）"
    need = {"open", "high", "low", "price"}
    if not need.issubset(set(quotes.columns)):
        return None, f"行情缺列: {sorted(need - set(quotes.columns))}"
    bar = (quotes.filter((pl.col("open") > 0) & (pl.col("price") > 0))
           .select([
               pl.lit(date(int(d[:4]), int(d[4:6]), int(d[6:]))).alias("trade_date"),
               pl.col("ts_code"),
               pl.col("open"), pl.col("high"), pl.col("low"),
               pl.col("price").alias("close"),
               pl.col("volume"), pl.col("amount"), pl.col("turnover"),
               pl.col("pct").alias("pct_chg"),
           ]))
    if bar.height == 0:
        return None, "行情无有效价格"
    return bar, qt


def persist_eod_bar(day: str | None = None, *,
                    quotes: pl.DataFrame | None = None) -> dict[str, Any]:
    """把某交易日收盘 bar 落到 `panel_live.parquet`（幂等）。

    为什么需要：`_persist_live_bar()` 此前**没有任何调用方**，导致 15:05 后
    当日收盘日线从未落盘（2026-11-24 实测 panel_live 停在 11-23），
    次日结算/K 线/情绪闸门只能看到 T-2。

    纪律：
      * 只用当日真实行情快照（open/high/low/close 全部来自行情）；
      * 覆盖率不足（< 上一交易日 60%）拒绝写入，避免残缺日污染；
      * 已有该日行 → 跳过（不覆盖收盘定格）。
    """
    d = str(day or datetime.now().strftime("%Y%m%d")).replace("-", "")[:8]
    target = date(int(d[:4]), int(d[4:6]), int(d[6:]))
    path = paths.panel_live_file()
    prev_n = 0
    if path.is_file():
        try:
            have = pl.read_parquet(path, columns=["trade_date"])
            days = sorted(set(have["trade_date"].unique().to_list()))
            if target in days:
                return {"ok": True, "skipped": "already", "day": d}
            earlier = [x for x in days if x < target]
            if earlier:
                prev_n = int(have.filter(
                    pl.col("trade_date") == earlier[-1]).height)
        except Exception:  # noqa: BLE001 - 读不了就交给 _persist_live_bar 处理
            prev_n = 0
    bar, info = eod_bar_from_quotes(d, quotes=quotes)
    if bar is None:
        return {"ok": False, "day": d, "reason": info}
    # 覆盖率护栏：至少达到上一交易日行数的 60%
    try:
        base = int(prev_n) if prev_n else int(bar.height)
        if base and bar.height / base < LIVE_MERGE_MIN_COVERAGE:
            return {"ok": False, "day": d, "rows": bar.height,
                    "reason": (f"覆盖率 {bar.height}/{base} "
                               f"< {LIVE_MERGE_MIN_COVERAGE:.0%}，拒绝写入")}
    except Exception:  # noqa: BLE001
        pass
    _persist_live_bar(bar, date(int(d[:4]), int(d[4:6]), int(d[6:])))
    return {"ok": True, "day": d, "rows": bar.height, "as_of": info}


def _quote_stamp(q: pl.DataFrame) -> str:
    """行情自带的数据时点（不是刷新时刻）：20261120161500 -> 2026-11-20T16:15:00。"""
    try:
        raw = str(q["quote_time"].max() or "")
    except Exception:  # noqa: BLE001
        raw = ""
    if len(raw) < 14 or not raw.isdigit():
        return ""
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T{raw[8:10]}:{raw[10:12]}:{raw[12:14]}"


def market_breadth(q: pl.DataFrame | None = None) -> dict[str, Any]:
    """全市场广度。

    涨跌停口径（2026-11-21 用户裁定，唯一实现在 `limit_counts`）：
    **东财池口径 = 收盘封死 + 剔除 ST**。`limit_up/limit_down` 是口径内家数，
    `limit_*_touched` 是「收盘价触及涨跌停价（含 ST、含未封死）」，供审计对账。
    """
    q = q if q is not None else all_quotes()
    if q.height == 0:
        return {}
    valid = q.filter(pl.col("pct").is_not_null() & (pl.col("prev_close") > 0))
    up = int((valid["pct"] > 0).sum())
    down = int((valid["pct"] < 0).sum())
    flat = int((valid["pct"] == 0).sum())
    lc = limit_counts.from_quotes(valid)
    ratio = up / max(down, 1)
    stamp = _quote_stamp(valid)
    out = {
        "n": valid.height, "up": up, "down": down, "flat": flat,
        "updown_ratio": round(ratio, 3),
        "limit_up": lc.get("limit_up", 0),
        "limit_down": lc.get("limit_down", 0),
        "limit_up_touched": lc.get("limit_up_touched", 0),
        "limit_down_touched": lc.get("limit_down_touched", 0),
        "limit_up_st": lc.get("limit_up_st", 0),
        "limit_down_st": lc.get("limit_down_st", 0),
        "limit_up_unsealed": lc.get("limit_up_unsealed", 0),
        "limit_down_unsealed": lc.get("limit_down_unsealed", 0),
        "limit_basis": lc.get("limit_basis"),
        "limit_note": lc.get("limit_note"),
        "book_verified": lc.get("book_verified", False),
        "median_pct": round(float(valid["pct"].median() or 0), 2),
        "amount": float(valid["amount"].sum() or 0),
        # 数据时点 = 行情自带时间；没有就留空并显式标注，不拿刷新时刻冒充
        "snapshot_at": stamp,
        "snapshot_source": "quote_time" if stamp else "missing",
        "refresh_at": datetime.now().isoformat(timespec="seconds"),
    }
    return out


def health() -> dict[str, Any]:
    q = all_quotes()
    return {
        "quotes": q.height if q is not None else 0,
        "quote_time": (q["quote_time"].max() if q is not None and q.height else None),
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "panel_rows": build_panel().height if _panel_path().is_file() else 0,
        "sources": ["腾讯批量行情", "东方财富涨停池", "项目日线缓存"],
    }
