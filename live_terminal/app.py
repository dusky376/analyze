"""悟空真经 · 二板排板终端（E5:B1）· Flask 入口。

启动：
    python app.py
然后打开 http://127.0.0.1:8765

本终端只保留悟空真经一条策略：
    选股  情绪闸门通过后，题材内有一字二板（兔子）→ 买同题材其他二板（乌龟）；
         无组合则当日不开仓（不硬凑、不退回「全部二板」旧口径）
    买点  二板封板瞬间以涨停价挂限价单排队（不默认成交）
    卖点  首个未涨停日（断板）当日收盘卖出
    角色  兔子=一字（买不进）、乌龟=换手（可能成交），只作标注

接口：
    GET /                 前端页面（单页）
    GET /api/version      进程 build / 磁盘 build / stale
    GET /api/health       数据与采集健康
    GET /api/live-sim     B1 实时模拟账本（今日委托 / 持仓 / 结算 / 三口径成本）
    GET /api/guitu        二板排板历史回放（主口径 + 对照 + 今日实时排板队列）
    GET /api/guitu-cases  龟兔案例库 / 今日快照 / 命题检验（只读）
    POST /api/refresh     清缓存并重新同步

本系统只写本地模拟账本，永不向任何券商/账户发送交易指令。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import polars as pl
from flask import Flask, Response, jsonify, request, send_from_directory

from backend import (buy_signals, charts, data_health, evolution,
                     exit_alternatives, feeds, guitu_backtest, guitu_cases,
                     ice_samples, leader_ladder, live_sim, market_clock,
                     minute_flow, news_feed, overseas, position_sizing,
                     process_guard, queue_match, recorder)
from backend import paths
from backend.json_safe import sanitize

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
# 数据与代码分离：数据根由 backend.paths 统一解析（支持 WUKONG_DATA_HOME）。
DATA = paths.data_dir()

source_build = process_guard.source_build
BUILD = process_guard.PROCESS_START_BUILD
STARTED = time.time()

app = Flask(__name__, static_folder=str(WEB), static_url_path="")
app.json.ensure_ascii = False
app.config["JSON_SORT_KEYS"] = False


# ---------------------------------------------------------------------------
# 缓存 / 错误边界
# ---------------------------------------------------------------------------
_lock = threading.RLock()
_cache: dict[str, tuple[float, Any]] = {}


def cached_copy(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    """TTL 缓存 + 深拷贝：调用方原地改写结果不会污染缓存。"""
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return copy.deepcopy(hit[1])
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return copy.deepcopy(val)


def clear_cache() -> None:
    """清空所有层级的缓存（修复旧版 _PANEL_MEM 大小写错误导致刷新失效的问题）。"""
    with _lock:
        _cache.clear()
    try:
        from backend import ice_gate
        ice_gate._METRICS_CACHE = None
    except Exception:  # noqa: BLE001
        pass
    try:
        live_sim._GATE_CACHE.clear()
    except Exception:  # noqa: BLE001
        pass
    try:
        feeds.clear_caches()
    except Exception:  # noqa: BLE001
        pass
    try:
        minute_flow.clear_caches()
    except Exception:  # noqa: BLE001
        pass
    try:
        overseas.clear_cache()
        news_feed.clear_cache()
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend import fill_verify
        if hasattr(fill_verify, "_AMT_CACHE"):
            fill_verify._AMT_CACHE.clear()
    except Exception:  # noqa: BLE001
        pass


def json_ok(payload: Any, ttl: int = 0) -> Response:
    resp = jsonify(sanitize(payload))
    if ttl:
        resp.headers["Cache-Control"] = f"private, max-age={ttl}"
    return resp


def api_guard(fn: Callable[[], Any]) -> Response:
    try:
        return json_ok(fn())
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }), 500


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def _serve_index() -> Response:
    try:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        ver = int(BUILD)
        html = html.replace("__APP_VER__", str(ver))
        resp = Response(html, mimetype="text/html; charset=utf-8")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
    except OSError:
        return Response("web/index.html 缺失", status=500, mimetype="text/plain; charset=utf-8")


@app.get("/")
def index() -> Response:
    return _serve_index()


@app.get("/favicon.svg")
def favicon() -> Response:
    return send_from_directory(WEB, "favicon.svg")


@app.get("/api/version")
def api_version() -> Response:
    disk = source_build()          # 每次请求现算，只用于报告磁盘现状
    return jsonify({
        "name": "悟空真经 · 二板排板终端",
        "strategy": "E5:B1",
        "build": BUILD,
        "disk_build": disk,
        "stale": disk > BUILD + 5,
        "started_at": datetime.fromtimestamp(STARTED).isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "uptime_secs": round(time.time() - STARTED, 1),
    })


@app.get("/api/health")
def api_health() -> Response:
    def _run() -> dict[str, Any]:
        panel = DATA / "panel_raw.parquet"
        panel_live = paths.panel_live_file()
        ph = feeds.panel_health()
        last = ph.get("effective_last_trade_date") or ph.get("raw_last_trade_date")
        rows = int(ph.get("effective_rows") or 0)
        newest_mtime = max(
            [p.stat().st_mtime for p in (panel, panel_live) if p.is_file()],
            default=None,
        )
        live_days = sorted(p.name for p in (DATA / "live").iterdir()
                           if p.is_dir() and p.name.isdigit()) if (DATA / "live").is_dir() else []
        rec = recorder.status()
        return {
            "ok": True,
            "day": datetime.now().strftime("%Y%m%d"),
            "market": market_clock.trading_state("CN"),
            "panel": {
                "file": str(panel),
                "exists": panel.is_file(),
                "last_trade_date": last,
                "effective_last_trade_date": ph.get("effective_last_trade_date"),
                "raw_last_trade_date": ph.get("raw_last_trade_date"),
                "live_last_trade_date": ph.get("live_last_trade_date"),
                "expected_last_trade_date": ph.get("expected_last_trade_date"),
                "gap_days": ph.get("gap_days") or [],
                "continuity_broken": bool(ph.get("continuity_broken")),
                "stale_panel": bool(ph.get("stale_panel")),
                "panel_ok": bool(ph.get("panel_ok")),
                "message": ph.get("message") or "",
                "rows": rows,
                "mtime": (datetime.fromtimestamp(newest_mtime)
                          .isoformat(timespec="seconds") if newest_mtime else None),
            },
            "live_archive": {"days": len(live_days), "latest": live_days[-1] if live_days else None},
            "archive_check": data_health.check(),
            "recorder": rec,
            "process": process_guard.status(),
            # 2026-11-25 数据/代码分离：暴露实际数据根，便于确认数据是否被搬走/指错。
            "paths": paths.describe(),
            "data_policy": "只读真实落盘：涨停池留档 + 真实日线；不合成、不插值、不补数。",
        }
    return api_guard(lambda: cached_copy("health", 15.0, _run))


# ---------------------------------------------------------------------------
# 悟空真经三个数据出口
# ---------------------------------------------------------------------------
def _scan_today_pre_seal(day: str) -> list[dict[str, Any]]:
    """只读执行临封扫板雷达：优先扫描「昨日首板池 → 今日一进二临封（+7.5%~涨停前五档薄卖一）」标的（绝不造数）。"""
    try:
        frames = live_sim.read_frames(day)
        if not frames:
            return []
        hits_1to2 = live_sim.scan_today_first_to_second_sweep(day, frames, use_live_l1=True)
        if hits_1to2:
            return hits_1to2[:15]
        allzt = live_sim.collect_all_zt(frames)
        rabbits = {c for c, g in allzt.items() if live_sim.is_rabbit(g)}
        if not rabbits:
            return []
        tags = live_sim._theme_tags(day, codes=list(allzt.keys()))
        if not tags:
            return []
        from backend import theme_match as _tm
        items = [{"code": c, "name": "", "tags": tlist, "industry": None, "streak": 2}
                 for c, tlist in tags.items()]
        grouped = _tm.group_by_theme(items)
        theme_rabbits: dict[str, list[str]] = {}
        theme_members: dict[str, list[str]] = {}
        for g in grouped.get("groups") or []:
            if g.get("level") not in ("axis", "tag"):
                continue
            th = str(g.get("theme") or "")
            for c in g.get("codes") or []:
                theme_members.setdefault(th, []).append(c)
                if c in rabbits:
                    theme_rabbits.setdefault(th, []).append(c)
        if not theme_rabbits:
            return []
        from backend import archive
        qdf = archive.latest_frame(day, "quotes")
        if qdf is None or qdf.height == 0:
            return []
        quotes_map = {
            str(r.get("ts_code") or "").zfill(6): r
            for r in qdf.iter_rows(named=True)
            if r.get("ts_code")
        }
        return buy_signals.scan_pre_seal_candidates(
            theme_rabbits, theme_members, quotes_map,
            already_sealed=set(allzt.keys()),
        )[:15]
    except Exception:  # noqa: BLE001
        return []


def _live_sim_enriched() -> dict[str, Any]:
    """执行 live_sim.sync() 并无损附加买点质量评分、临封雷达与新鲜度元数据（不改账本）。"""
    d = live_sim.sync()
    if isinstance(d, dict):
        # 合并外盘、国内快讯、高标同题材传导与主线龙头热度（与 live_sim.sync 唯一口径一致）
        ovs = d.get("overseas") or {}
        cat_scores: dict[str, float] = {}
        for th, ci in (ovs.get("catalyst_by_theme") or {}).items():
            cat_scores[th] = cat_scores.get(th, 0.0) + float(ci.get("catalyst_score") or 0.0)
        hb_pulse = ((d.get("emotion") or {}).get("high_board_pulse") or {})
        for th in (hb_pulse.get("lead_themes") or []):
            cat_scores[th] = cat_scores.get(th, 0.0) + 0.5
        stock_alerts: dict[str, dict[str, Any]] = {}
        try:
            ns = news_feed.snapshot(day=str(d.get("day") or ""))
            if ns.get("available"):
                stock_alerts = ns.get("stock_alerts") or {}
                for th, ni in (ns.get("catalyst_by_theme") or {}).items():
                    cat_scores[th] = cat_scores.get(th, 0.0) + max(-2.0, min(2.0, float(ni.get("score") or 0.0)))
        except Exception:  # noqa: BLE001
            pass

        # 为今日委托只读附加买点质量分级（A/B/C/REJECT + 撤单预警 + 外盘/快讯催化）
        orders = d.get("today_orders") or []
        for o in orders:
            if not isinstance(o, dict):
                continue
            rabbits = o.get("guitu_rabbits") or []
            qsim = o.get("queue_sim") or {}
            # 仅对未成交且未开板的排队单比较封单失血；已成交单队列归零是吃穿成交，绝非失血撤单
            is_unfilled_intact = (o.get("status") != "filled" and not (qsim.get("rounds") or []))
            cur_seal = qsim.get("queue_left_yi") if is_unfilled_intact else None
            theme = str(o.get("guitu_theme") or o.get("industry") or "")
            code6 = str(o.get("ts_code") or "").zfill(6)
            o["buy_quality"] = buy_signals.evaluate_turtle_quality(
                o,
                rabbit_count=len(rabbits) if isinstance(rabbits, list) else 1,
                peak_seal_yi=qsim.get("seal_fund_at_entry_yi"),
                current_seal_yi=cur_seal,
                catalyst_score=cat_scores.get(theme),
                news_alert=stock_alerts.get(code6),
                rabbit_seal_yi=o.get("rabbit_max_seal_yi"),
            )
        d["pre_seal_radar"] = _scan_today_pre_seal(str(d.get("day") or ""))
        mkt = market_clock.trading_state("CN")
        d["freshness"] = {
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "generated_at": d.get("generated_at"),
            "market_trading": bool(mkt.get("trading")),
            "market_reason": mkt.get("reason") or "",
            "price_source": "腾讯/新浪实时行情 · 日韩美股外盘 · 7×24政策快讯",
            "cache_ttl_sec": 15 if mkt.get("trading") else 60,
        }
    return d


def _live_sim_ttl() -> float:
    try:
        return 15.0 if market_clock.is_trading("CN") else 60.0
    except Exception:  # noqa: BLE001
        return 30.0


@app.get("/api/live-sim")
def api_live_sim() -> Response:
    """B1 实时模拟账本：二板涨停瞬间排板 → 按 11-25 两路径卖点规则卖出。"""
    return api_guard(lambda: cached_copy("live_sim", _live_sim_ttl(), _live_sim_enriched))


@app.get("/api/evolution")
def api_evolution() -> Response:
    """蚁群信息素路径权重 + 非对称奖惩反馈 + 影子账本对照（只读，不改生产账本）。"""
    return api_guard(lambda: cached_copy("evolution", 60.0, evolution.snapshot))


@app.get("/api/macro-intel")
def api_macro_intel() -> Response:
    """外盘指数风偏（日经/韩国KOSPI/美股/A50）、美股板块映射与国内7×24政策快讯。"""
    def _run() -> dict[str, Any]:
        ovs = overseas.snapshot()
        ns = news_feed.snapshot()
        # 给美股板块映射也挂上当日真实 A 股活跃对应股（来自 theme_pool 连板/涨停/炸板三池）
        tmap = ns.get("theme_stock_map") or news_feed.build_theme_stock_map()
        if isinstance(ovs, dict) and ovs.get("catalysts"):
            ovs = copy.deepcopy(ovs)
            for c in ovs["catalysts"]:
                c["mapped_stocks"] = tmap.get(c.get("theme"), [])[:5]
        # 交叉核对当前持仓股是否命中雷区/利好快讯（统一过滤已作废/已平仓/已冲销脏单）
        watch_alerts: list[dict[str, Any]] = []
        try:
            sf = getattr(live_sim, "STATE_FILE", paths.data_dir() / "paper" / "sim_state.json")
            if Path(sf).is_file():
                st = json.loads(Path(sf).read_text(encoding="utf-8"))
                raw_orders = st.get("orders") or {}
                invalid_oids = {
                    str(k) for k, v in raw_orders.items()
                    if isinstance(v, dict) and position_sizing.is_invalid_record(v)
                } if isinstance(raw_orders, dict) else set()
                raw_voided = st.get("voided_positions") or {}
                voided_ids = set(raw_voided.keys()) if isinstance(raw_voided, dict) else set()
                raw_pos = st.get("positions") or {}
                pos_list = list(raw_pos.values()) if isinstance(raw_pos, dict) else list(raw_pos)
                holding_map = {
                    str(p.get("ts_code") or "").zfill(6): str(p.get("name") or "")
                    for p in pos_list
                    if isinstance(p, dict)
                    and p.get("ts_code")
                    and p.get("status") not in ("closed", "voided")
                    and str(p.get("id") or "") not in voided_ids
                    and not position_sizing.is_invalid_record(p, invalid_oids)
                }
                for code, alert in (ns.get("stock_alerts") or {}).items():
                    if code in holding_map:
                        watch_alerts.append({
                            "ts_code": code,
                            "name": holding_map[code],
                            "scope": "当前持仓",
                            **alert,
                        })
        except Exception:  # noqa: BLE001
            pass
        from backend import lhb as _lhb
        try:
            lhb_info = _lhb.causal_snapshot(datetime.now().strftime("%Y-%m-%d"), lookback_days=5, allow_fetch=True)
            lhb_latest = lhb_info.get("latest_day") or ""
            lhb_profs = lhb_info.get("stock_profiles") or {}
            lhb_focus = _lhb.compute_lhb_focus(lhb_profs, lhb_latest) if (lhb_profs and lhb_latest) else {"top_themes": [], "top_stocks": []}
            # 用实时行情（不阻塞）计算昨日上榜游资今日溢价（验证游资赚钱效应）
            pct_map: dict[str, float] = {}
            try:
                qdf = feeds.all_quotes(force=False)
                if qdf is not None and qdf.height:
                    need = set(lhb_profs.keys())
                    for r in qdf.filter(pl.col("ts_code").is_in(list(need))).select(["ts_code", "pct"]).iter_rows(named=True):
                        if r.get("pct") is not None:
                            pct_map[str(r["ts_code"]).zfill(6)] = float(r["pct"])
            except Exception:  # noqa: BLE001
                pct_map = {}
            youzi_prem = _lhb.compute_youzi_premium(lhb_profs, pct_map)
        except Exception:  # noqa: BLE001
            lhb_info = {"available": False, "latest_day": None, "market_sentiment": {}, "stock_profiles": {}}
            lhb_focus = {"top_themes": [], "top_stocks": []}
            youzi_prem = {"available": False}
        return {
            "available": bool(ovs.get("available") or ns.get("available") or lhb_info.get("available")),
            "overseas": ovs,
            "news": ns,
            "lhb": {
                "available": bool(lhb_info.get("available")),
                "latest_day": lhb_info.get("latest_day"),
                "market_sentiment": lhb_info.get("market_sentiment") or {},
                "youzi_premium": youzi_prem,
                "focus": lhb_focus,
                "n_profiles": len(lhb_info.get("stock_profiles") or {}),
            },
            "watch_alerts": watch_alerts,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
    return api_guard(lambda: cached_copy("macro_intel", 90.0, _run))


@app.get("/api/guitu")
def api_guitu() -> Response:
    """二板排板历史回放 + 今日实时排板队列。"""
    def _run() -> dict[str, Any]:
        book = queue_match.live_queue_book()
        return {
            "sim": guitu_backtest.cached_result(),
            "today_orders": book.get("orders", []),
            "queue_book": book,
            "wukong_cases": {
                "status": guitu_cases._thesis()["status"],
                "groups": len(guitu_cases.PAIR_GROUPS),
                "bull_periods": len(leader_ladder.CASE_PERIODS),
                "detail": "/api/guitu-cases",
            },
        }
    return api_guard(lambda: cached_copy("guitu", 60.0, _run))


@app.get("/api/guitu-cases")
def api_guitu_cases() -> Response:
    """龟兔案例库、今日实时快照与命题检验（只读，不产生买卖信号）。"""
    return api_guard(lambda: cached_copy("guitu_cases", 300.0, guitu_cases.payload))


@app.get("/api/recorder")
def api_recorder() -> Response:
    return api_guard(recorder.status)


@app.get("/api/exit-alternatives")
def api_exit_alternatives() -> Response:
    """卖法对照（只读研究）：现行卖法 vs 固定止盈止损模拟 + 当时环境快照。

    不改任何账本、不产生买卖点；仅展示"若当时用另一卖法"的描述性对照。
    """
    return api_guard(lambda: cached_copy("exit_alternatives", 300.0,
                                         exit_alternatives.analyze))


@app.get("/api/chart")
def api_chart() -> Response:
    """个股 K 线 + 分时（盘中实时）：?code=002931&day=20261124&kdays=40。

    全部真实落盘；盘中 K 线最后一根为内存实时 bar、分末点为实时价，
    前端每轮轮询刷新。不写盘、不下单。
    """
    code = str(request.args.get("code") or "").strip()
    if not code or not code.isdigit():
        return jsonify({"ok": False, "error": "code 必须是 6 位数字"}), 400
    day = request.args.get("day") or None
    try:
        kdays = int(request.args.get("kdays") or 40)
    except (TypeError, ValueError):
        kdays = 40
    kdays = max(5, min(250, kdays))
    return api_guard(lambda: charts.stock_chart(code, day=day, kdays=kdays))


@app.get("/api/ice-samples")
def api_ice_samples() -> Response:
    """冰点样本库（用户裁定 B：命中即新增样本）+ 漂移监控。

    只读：返回 anchor（人工标注）/ auto（算法命中）统计与最近样本。
    """
    def _run() -> dict[str, Any]:
        st = ice_samples.load()
        dr = ice_samples.drift(st)
        autos = st.get("auto_samples") or {}
        recent = [autos[k] for k in sorted(autos)][-30:]
        by_type: dict[str, int] = {}
        for v in autos.values():
            t = (v or {}).get("ice_type") or "unknown"
            by_type[t] = by_type.get(t, 0) + 1
        by_level: dict[str, int] = {}
        for v in autos.values():
            lv = (v or {}).get("severity_level") or "unknown"
            by_level[lv] = by_level.get(lv, 0) + 1
        return {
            "available": True,
            "version": st.get("version"),
            "updated_at": st.get("updated_at"),
            "auto_n": len(autos),
            "history_n": len(st.get("history") or {}),
            "anchor_n": dr["anchor_n"],
            "by_ice_type": by_type,
            "by_level": by_level,
            "drift": dr,
            "drift_history": (st.get("drift_history") or [])[-10:],
            "recent": recent,
            "note": ("anchor=人工标注、auto=算法命中；两者重合率即漂移指标。"
                     "auto 样本参与分位校准（用户裁定 B），可按 source 回溯剔除。"),
        }
    return api_guard(lambda: cached_copy("ice_samples", 120.0, _run))


@app.post("/api/refresh")
def api_refresh() -> Response:
    clear_cache()
    try:
        val = _live_sim_enriched()
        with _lock:
            _cache["live_sim"] = (time.time(), val)
    except Exception:  # noqa: BLE001
        pass
    return json_ok({"ok": True, "refreshed_at": datetime.now().isoformat(timespec="seconds")})


@app.get("/api/stream")
def api_stream() -> Response:
    """交易日实时 SSE 推送：交易时段每 15 秒推送最新状态心跳，非交易时段每 45 秒。"""
    def _gen():
        while True:
            trading = False
            try:
                trading = bool(market_clock.is_trading("CN"))
                d = cached_copy("live_sim", _live_sim_ttl(), _live_sim_enriched)
                brief = {
                    "build": int(BUILD),
                    "day": d.get("day"),
                    "n_today": d.get("n_today"),
                    "n_filled_today": d.get("n_filled_today"),
                    "n_holding": d.get("n_holding"),
                    "no_orders": d.get("no_orders"),
                    "frames": d.get("frames"),
                    "trading": trading,
                    "generated_at": d.get("generated_at"),
                    "server_time": datetime.now().isoformat(timespec="seconds"),
                }
                yield f"data: {json.dumps(brief, ensure_ascii=False)}\n\n"
            except Exception as exc:  # noqa: BLE001
                yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
            time.sleep(15 if trading else 45)

    return Response(_gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# 启动：实时留档线程 + 后台快照预热线程（pytest 下禁用）
# ---------------------------------------------------------------------------
def _under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in os.environ.get("_", "")


def _warm_loop() -> None:
    """后台预热 live_sim 内存快照，让前端请求直接命中内存（<10ms 响应）。"""
    while True:
        if process_guard.is_stale_process():
            time.sleep(30.0)
            continue
        try:
            val = _live_sim_enriched()
            with _lock:
                _cache["live_sim"] = (time.time(), val)
        except Exception:  # noqa: BLE001
            pass
        try:
            interval = 20.0 if market_clock.is_trading("CN") else 60.0
        except Exception:  # noqa: BLE001
            interval = 45.0
        time.sleep(interval)


def _start_recorder() -> None:
    if _under_pytest() or os.environ.get("WUKONG_NO_RECORDER") == "1":
        return
    try:
        recorder.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[wukong] recorder 未启动: {type(exc).__name__}: {exc}", flush=True)
    try:
        t = threading.Thread(target=_warm_loop, name="wukong-warm", daemon=True)
        t.start()
    except Exception:  # noqa: BLE001
        pass


_start_recorder()


if __name__ == "__main__":
    port = int(os.environ.get("WUKONG_PORT", "8765"))
    print(f"[wukong] http://127.0.0.1:{port}  build={BUILD:.0f}", flush=True)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
