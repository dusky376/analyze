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

from backend import (charts, data_health, exit_alternatives, feeds, guitu_backtest,
                     guitu_cases, ice_samples, leader_ladder, live_sim, market_clock,
                     queue_match, recorder)
from backend.json_safe import sanitize

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"


def source_build() -> float:
    """磁盘上源码的最新 mtime（秒）——进程启动时冻结，避免陈旧进程自称最新。"""
    newest = 0.0
    p = ROOT / "app.py"
    if p.is_file():
        newest = max(newest, p.stat().st_mtime)
    for sub in ("backend", "web"):
        d = ROOT / sub
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".js", ".css", ".html"):
                newest = max(newest, f.stat().st_mtime)
    return newest


BUILD = source_build()
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
    """清空所有层级的缓存。

    2026-11-23 审计：旧实现只清 `_cache`，`ice_gate._METRICS_CACHE`、
    `live_sim._GATE_CACHE`、`feeds` 的 panel 缓存、`fill_verify._AMT_CACHE`
    都残留 → `/api/refresh` 并不能真正"刷新"，会继续用旧的全市场扫描结果。
    """
    with _lock:
        _cache.clear()
    # 各模块的进程级缓存（尽力清，缺属性则跳过，不阻塞刷新）
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
        from backend import feeds
        for name in ("_PANEL_MEM", "_CAL_DAYS", "_PANEL_CACHE"):
            if hasattr(feeds, name):
                setattr(feeds, name, None)
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
        return Response(html, mimetype="text/html; charset=utf-8")
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
        last = None
        rows = None
        if panel.is_file():
            try:
                lf = pl.scan_parquet(panel).select(
                    pl.col("trade_date").max().alias("last"),
                    pl.len().alias("n"),
                )
                got = lf.collect().to_dicts()[0]
                last = str(got.get("last"))
                rows = int(got.get("n") or 0)
            except Exception as exc:  # noqa: BLE001
                last = f"读取失败: {type(exc).__name__}"
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
                "rows": rows,
                "mtime": (datetime.fromtimestamp(panel.stat().st_mtime)
                          .isoformat(timespec="seconds") if panel.is_file() else None),
            },
            "live_archive": {"days": len(live_days), "latest": live_days[-1] if live_days else None},
            "archive_check": data_health.check(),
            "recorder": rec,
            "data_policy": "只读真实落盘：涨停池留档 + 真实日线；不合成、不插值、不补数。",
        }
    return api_guard(_run)


# ---------------------------------------------------------------------------
# 悟空真经三个数据出口
# ---------------------------------------------------------------------------
@app.get("/api/live-sim")
def api_live_sim() -> Response:
    """B1 实时模拟账本：二板涨停瞬间排板 → 断板当日收盘卖。"""
    return api_guard(lambda: cached_copy("live_sim", 60.0, live_sim.sync))


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
        live_sim.sync()
    except Exception:  # noqa: BLE001
        pass
    return json_ok({"ok": True, "refreshed_at": datetime.now().isoformat(timespec="seconds")})


@app.get("/api/stream")
def api_stream() -> Response:
    """轻量 SSE：每 30 秒推送一次今日开仓状态，前端可降级为轮询。"""
    def _gen():
        while True:
            try:
                d = cached_copy("live_sim_sse", 25.0, live_sim.sync)
                brief = {
                    "day": d.get("day"),
                    "n_today": d.get("n_today"),
                    "n_holding": d.get("n_holding"),
                    "no_orders": d.get("no_orders"),
                    "generated_at": d.get("generated_at"),
                }
                yield f"data: {json.dumps(brief, ensure_ascii=False)}\n\n"
            except Exception as exc:  # noqa: BLE001
                yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
            time.sleep(30)

    return Response(_gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# 启动：实时留档线程（pytest 下禁用）
# ---------------------------------------------------------------------------
def _under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in os.environ.get("_", "")


def _start_recorder() -> None:
    if _under_pytest() or os.environ.get("WUKONG_NO_RECORDER") == "1":
        return
    try:
        recorder.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[wukong] recorder 未启动: {type(exc).__name__}: {exc}", flush=True)


_start_recorder()


if __name__ == "__main__":
    port = int(os.environ.get("WUKONG_PORT", "8765"))
    print(f"[wukong] http://127.0.0.1:{port}  build={BUILD:.0f}", flush=True)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
