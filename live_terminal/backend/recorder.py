"""实时数据留档（recorder）。

目标
----
把盘中真正「看过」的数据落盘，供次日和历史比对：

    data/live/YYYYMMDD/
        meta.json                当日留档摘要（帧数/首末时间/竞价是否采到）
        auction/*.parquet        集合竞价 09:15–09:25，每 15 秒一帧（全市场）
        quotes/*.parquet         连续竞价，每 5 分钟一帧（全市场）
        breadth.jsonl            涨跌广度 / 涨跌停，每 30 秒一条
        pools/*.jsonl            涨停 / 炸板 / 跌停池，每 60 秒一条

设计取舍
--------
* 竞价是稀缺数据（一天只有 10 分钟），必须全市场保存；15 秒一帧约 40 帧/日。
* 盘中全市场 5 分钟一帧，兼顾「可回溯」与磁盘占用（约 6–8 MB/日）。
* 所有写入都是「临时文件 + 原子替换」，进程被杀不会留下半个 parquet。
* 只读 feeds 的缓存结果，不额外压垮行情源。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from backend import feeds

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "data" / "live"
RETENTION_DAYS = 60

AUCTION_START, AUCTION_END = dtime(9, 15), dtime(9, 26)
AUCTION_EVERY = 15.0
QUOTE_EVERY = 300.0
BREADTH_EVERY = 30.0
POOL_EVERY = 60.0
#: 题材池（同花顺三池）盘中实时刷新间隔。
#: 2026-11-24 用户裁定：「这是股票，不是游戏」—— 题材池必须盘中抓，
#: 否则「题材内一字板 + 换手二板」的选股链路在盘中永远拿不到标签
#: （实测 11-24 停更在 11-20，当天有 11 只一字二板却 0 委托）。
THEME_EVERY = 60.0
THEME_START = dtime(9, 25)      # 竞价定格后再抓，避免把未定格的竞价标签当决策依据
THEME_END = dtime(15, 5)        # 与日线收盘闸门一致；此后抓到的记为「收盘定格」
THEME_TICK = 5.0                # 独立线程的检查粒度（不阻塞主 tick）
TICK = 3.0

QUOTE_COLS = [
    "ts_code", "name", "price", "prev_close", "open", "high", "low",
    "pct", "volume", "amount", "turnover", "limit_up", "limit_down",
    "quote_time", "snapshot_at",
]

_lock = threading.RLock()
_thread: threading.Thread | None = None
_theme_thread: threading.Thread | None = None
_stop = threading.Event()
_state: dict[str, Any] = {
    "last_error": None,
    "last_tick": None,
    "last_auction": 0.0,
    "last_quote": 0.0,
    "last_breadth": 0.0,
    "last_pool": 0.0,
    "last_theme": 0.0,
    "last_theme_error": None,
    "day": None,
}
_pruned_for: str | None = None


# ---------------------------------------------------------------------------
# 落盘工具
# ---------------------------------------------------------------------------

def _day_dir(day: date | None = None) -> Path:
    d = (day or datetime.now().date()).strftime("%Y%m%d")
    p = LIVE / d
    p.mkdir(parents=True, exist_ok=True)
    return p


def _atomic_parquet(df: pl.DataFrame, path: Path) -> None:
    if df is None or df.height == 0:
        return
    tmp = path.with_name(path.stem + ".tmp.parquet")
    df.write_parquet(tmp, compression="zstd")
    tmp.replace(path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _read_meta(folder: Path) -> dict[str, Any]:
    p = folder / "meta.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(folder: Path, meta: dict[str, Any]) -> None:
    tmp = folder / "meta.tmp.json"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(folder / "meta.json")


def _bump(folder: Path, key: str, **extra: Any) -> None:
    with _lock:
        meta = _read_meta(folder)
        meta.setdefault("date", folder.name)
        meta[key] = int(meta.get(key, 0)) + 1
        meta["last_at"] = datetime.now().isoformat(timespec="seconds")
        meta.setdefault("first_at", meta["last_at"])
        meta.update(extra)
        _write_meta(folder, meta)
        _state["last_" + key.replace("_frames", "").replace("_points", "")] = time.time()


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------

def _quotes_frame() -> pl.DataFrame:
    q = feeds.all_quotes()
    if q is None or q.height == 0:
        return pl.DataFrame()
    cols = [c for c in QUOTE_COLS if c in q.columns]
    out = q.select(cols)
    if "snapshot_at" not in out.columns:
        out = out.with_columns(
            pl.Series("snapshot_at", [datetime.now().isoformat(timespec="seconds")] * out.height)
        )
    return out


def _snap_auction(folder: Path) -> None:
    df = _quotes_frame()
    if df.height == 0:
        return
    ts = datetime.now().strftime("%H%M%S")
    _atomic_parquet(df, folder / "auction" / f"auction_{ts}.parquet")
    _bump(folder, "auction_frames",
          last_quote_time=str(df["quote_time"].max() or ""))
    _state["last_auction"] = time.time()


def _snap_quotes(folder: Path) -> None:
    df = _quotes_frame()
    if df.height == 0:
        return
    ts = datetime.now().strftime("%H%M%S")
    _atomic_parquet(df, folder / "quotes" / f"quotes_{ts}.parquet")
    _bump(folder, "quote_frames",
          last_quote_time=str(df["quote_time"].max() or ""))
    _state["last_quote"] = time.time()


def _snap_breadth(folder: Path) -> None:
    b = feeds.market_breadth()
    if not b:
        return
    _append_jsonl(folder / "breadth.jsonl", {"ts": datetime.now().isoformat(timespec="seconds"), **b})
    _bump(folder, "breadth_points")
    _state["last_breadth"] = time.time()


def _snap_pools(folder: Path) -> None:
    """留档涨停/炸板/跌停池。

    2026-11-13：新增 `all` 全量字段。此前只留 `top=head(30)` 且按 streak 降序，
    二板（streak==2）恰在最易被截掉的位置 —— 用截断帧做「二板排板监控」会
    系统性漏信号。`top` 保留原样供前端展示，`all` 供策略账本读全量。
    同时加 `ok`/`err`/`empty_note`，避免「接口失败」被当成「今天没有涨停」。
    """
    payload: dict[str, Any] = {"ts": datetime.now().isoformat(timespec="seconds")}
    for kind, fn in (("zt", feeds.zt_pool), ("zb", feeds.zb_pool), ("dt", feeds.dt_pool)):
        err: str | None = None
        try:
            df = fn()
        except Exception as exc:  # noqa: BLE001
            df, err = None, f"{type(exc).__name__}: {exc}"
        if df is None or df.height == 0:
            payload[kind] = {
                "n": 0, "top": [], "all": [],
                "ok": err is None, "err": err,
                "empty_note": ("接口返回空或请求失败，feeds 层当前不区分二者；"
                               "不得把本帧解读为「今日无涨停」"),
            }
            continue
        cols = [c for c in ("ts_code", "name", "price", "pct", "streak", "industry",
                            "turnover", "seal_fund", "broken", "first_seal",
                            "last_seal", "trade_date") if c in df.columns]
        full = df.select(cols).to_dicts()
        rows = (df.select(cols).sort("streak", descending=True).head(30).to_dicts()
                if "streak" in df.columns else full[:30])
        payload[kind] = {"n": df.height, "top": rows, "all": full,
                         "ok": True, "err": None, "all_complete": True}
    _append_jsonl(folder / "pools" / "pools.jsonl", payload)
    _bump(folder, "pool_points")
    _state["last_pool"] = time.time()


def _snap_themes(now: datetime) -> dict[str, Any]:
    """盘中实时抓同花顺题材池（涨停/炸板/连板三池）并存量更新。

    为什么放这里（2026-11-24）：
      `data/live/<day>/pools` 只留涨停池的行业字段，**没有题材标签**；
      二板选股链路 `live_sim._theme_tags()` 读的是 `data/themes/themes.parquet`。
      该文件此前只靠人工跑，11-21 之后断更 → 11-24 有 11 只一字二板、
      情绪也不差，却因为「当日无题材池标签」全天 0 委托。
      现在跟随 recorder 每 60 秒刷新一次，盘中标签与行情同频。

    留痕：manifest 里写 `intraday=True` + `snapshot_at`，区分「盘中快照」
    与「收盘定格」；收盘后最后一次抓取会把该日数据定格（upsert 幂等覆盖）。
    """
    from backend import theme_pool

    day = now.strftime("%Y%m%d")
    intraday = now.time() < THEME_END
    res = theme_pool.collect(
        day, save=True,
        meta_extra={"intraday": intraday,
                    "snapshot_at": now.isoformat(timespec="seconds")},
    )
    folder = _day_dir(now.date())
    _prepare(folder)
    _bump(folder, "theme_points",
          last_theme_at=now.isoformat(timespec="seconds"),
          last_theme_rows=int(res.get("n_rows") or 0),
          last_theme_intraday=bool(intraday))
    return res


def _theme_due(now: datetime) -> bool:
    """是否该抓题材池（交易日 + 交易时段 + 间隔到）。"""
    if not _is_trading_day(now):
        return False
    t = now.time()
    if t < THEME_START or t > THEME_END:
        return False
    if dtime(11, 32) <= t < dtime(12, 58):
        return False
    return time.time() - float(_state.get("last_theme") or 0.0) >= THEME_EVERY


def _theme_loop() -> None:
    """题材池专用线程：网络慢/超时也不拖住 3 秒主采集循环。"""
    while not _stop.is_set():
        try:
            now = datetime.now()
            if _theme_due(now):
                _snap_themes(now)
                _state["last_theme_error"] = None
        except Exception as exc:  # noqa: BLE001 - 单次失败不影响主循环
            _state["last_theme_error"] = f"{type(exc).__name__}: {exc}"
        _stop.wait(THEME_TICK)


def _prune() -> None:
    """只保留最近 RETENTION_DAYS 个自然日目录。"""
    global _pruned_for
    today = datetime.now().date().strftime("%Y%m%d")
    if _pruned_for == today or not LIVE.is_dir():
        return
    _pruned_for = today
    cutoff = datetime.now().date() - timedelta(days=RETENTION_DAYS)
    for p in LIVE.iterdir():
        if not p.is_dir() or len(p.name) != 8 or not p.name.isdigit():
            continue
        try:
            d = date(int(p.name[:4]), int(p.name[4:6]), int(p.name[6:8]))
        except ValueError:
            continue
        if d < cutoff:
            for f in sorted(p.rglob("*"), reverse=True):
                try:
                    f.unlink() if f.is_file() else f.rmdir()
                except OSError:
                    pass
            try:
                p.rmdir()
            except OSError:
                pass


def _prepare(folder: Path) -> None:
    for sub in ("auction", "quotes", "pools"):
        (folder / sub).mkdir(parents=True, exist_ok=True)


def _is_trading_day(now: datetime) -> bool:
    """是否 A 股交易日（含节假日判定）。

    2026-11-20 修复（借鉴 TSP 的「交易日探针」）：旧实现只看 `weekday() >= 5`，
    节假日（如国庆/春节）会被当成交易日白跑一整天采集——拍空帧、记空 meta，
    既浪费请求又污染 `/api/recorder` 的「今天采了多少」判断。
    真实节假日以 `market_clock` 的交易日历（calendar.parquet，含调休）为准；
    日历不可用时**降级回 weekday 判断**（宁可多采一天，也不漏采）。
    """
    try:
        from backend import market_clock
        return bool(market_clock.is_trading_day("CN", now=now))
    except Exception:  # noqa: BLE001 - 日历不可用绝不阻塞采集
        return now.weekday() < 5


def _tick() -> None:
    now = datetime.now()
    _state["last_tick"] = now.isoformat(timespec="seconds")
    if not _is_trading_day(now):
        return  # 周末 / 法定节假日不采
    t = now.time()
    if t < dtime(9, 15) or t > dtime(15, 15):
        return  # 非交易时段（收盘后再给 15 分钟让行情定格）
    if dtime(11, 32) <= t < dtime(12, 58):
        return  # 午间休市不重复留档

    folder = _day_dir(now.date())
    _prepare(folder)
    _prune()

    in_auction = AUCTION_START <= t < AUCTION_END
    if in_auction and time.time() - _state["last_auction"] >= AUCTION_EVERY:
        _snap_auction(folder)
    elif not in_auction and time.time() - _state["last_quote"] >= QUOTE_EVERY:
        _snap_quotes(folder)

    if time.time() - _state["last_breadth"] >= BREADTH_EVERY:
        _snap_breadth(folder)
    if time.time() - _state["last_pool"] >= POOL_EVERY:
        _snap_pools(folder)
    # 收盘定格（15:05 后抓最后一帧行情写 panel_live）：每日一次。
    # 2026-11-25 修复：此前没有任何调用方，panel_live 停在 11-23，
    # 次日 K 线/结算/情绪闸门都看不到 T-1 收盘。
    if t >= dtime(15, 5) and _state.get("eod_day") != now.strftime("%Y%m%d"):
        try:
            from backend import feeds
            res = feeds.persist_eod_bar(now.strftime("%Y%m%d"))
            _state["eod_result"] = res
            if res.get("ok"):
                _state["eod_day"] = now.strftime("%Y%m%d")
        except Exception as exc:  # noqa: BLE001 - 定格失败不阻塞采集
            _state["last_error"] = f"eod: {type(exc).__name__}: {exc}"


def _loop() -> None:
    while not _stop.is_set():
        try:
            _tick()
            _state["last_error"] = None
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
        _stop.wait(TICK)


# ---------------------------------------------------------------------------
# 对外
# ---------------------------------------------------------------------------

def start() -> dict[str, Any]:
    global _thread, _theme_thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return status()
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="live-recorder", daemon=True)
        _thread.start()
        _theme_thread = threading.Thread(target=_theme_loop, name="theme-recorder",
                                         daemon=True)
        _theme_thread.start()
    return status()


def stop() -> None:
    _stop.set()


def status() -> dict[str, Any]:
    today = datetime.now().date()
    folder = LIVE / today.strftime("%Y%m%d")
    meta = _read_meta(folder)
    days = sorted([p.name for p in LIVE.iterdir() if p.is_dir() and p.name.isdigit()]) \
        if LIVE.is_dir() else []
    return {
        "running": bool(_thread and _thread.is_alive()),
        "root": str(LIVE),
        "retention_days": RETENTION_DAYS,
        "today": {
            "date": today.strftime("%Y%m%d"),
            "auction_frames": int(meta.get("auction_frames", 0)),
            "quote_frames": int(meta.get("quote_frames", 0)),
            "breadth_points": int(meta.get("breadth_points", 0)),
            "pool_points": int(meta.get("pool_points", 0)),
            "theme_points": int(meta.get("theme_points", 0)),
            "last_theme_at": meta.get("last_theme_at"),
            "last_theme_rows": meta.get("last_theme_rows"),
            "last_theme_intraday": meta.get("last_theme_intraday"),
            "first_at": meta.get("first_at"),
            "last_at": meta.get("last_at"),
            "last_quote_time": meta.get("last_quote_time"),
        },
        "days": days[-10:],
        "day_count": len(days),
        "auction_window": "09:15–09:25（含 09:25 定格）",
        "tick_sec": TICK,
        "auction_every_sec": AUCTION_EVERY,
        "quote_every_sec": QUOTE_EVERY,
        "theme_every_sec": THEME_EVERY,
        "last_tick": _state.get("last_tick"),
        "last_error": _state.get("last_error"),
        "last_theme_error": _state.get("last_theme_error"),
        "note": (
            "竞价每 15 秒、盘中每 5 分钟保存全市场快照；"
            "广度 30 秒、情绪池 60 秒、题材池（同花顺三池）60 秒。保留 60 天。"
        ),
    }
