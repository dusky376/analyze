"""tests 包 conftest：sys.path + 测试期出站网络「硬化」（2026-11-16）。

一、sys.path
------------
项目没有安装成包，把仓库根与 tools/ 放进 sys.path。
（沙箱适配 basetemp / collect-ignore 统一放在项目根 conftest.py，这里不重复。）

二、修复「全量 pytest 间歇性卡在 51%」（2026-11-16）
---------------------------------------------------
现象：`pytest tests` 全量跑会**间歇性**卡在 51~52%，十几分钟无进展；
任意单个文件跑都正常；同样一批文件上一次 50 秒跑完、下一次就卡死。
本会话最初（未改动任何代码）就能复现，属既有问题。

定位结论（证据见 tools/qa_pytest_watchdog.py 抓的线程栈 + 黑洞代理复现）：
    卡住的是 `tests/test_order_engine.py` 的用例（全量进度条常停在 51~52%，
    这是非 -v 模式下最后一次 flush 的进度行，真实卡点在下一次整行输出前的
    order_engine 区间）。其调用链：

        test_order_engine.py:51
          → order_engine.build_all()
            → order_engine._pool_gate()            # order_engine.py:327
              → feeds.all_quotes()                 # feeds.py:325
                → ThreadPoolExecutor 31 批 × 3 重试 × 单次 timeout 8s
          → live_sim.sync() → intraday_signal.fetch()   # 每只二板票一次 HTTP

    2026-11-16 夜间留档里有 9 只二板票；order_engine 12 个用例**每个都重新
    拉一遍**（`feeds._cached` 只缓存成功、不缓存失败）。网络一旦「静默丢包」
    （丢包而非快速拒绝），每次 connect 要等满 8~10s 超时：
        单次 build_all ≈ 31/6 批 × 3 重试 × (超时+退避) ≈ 100s+
        12 个用例连起来就是十几分钟「无进展」——与实测 12 分钟完全吻合。
    网络正常时（单次 ~0.3~0.9s 且被 10~25s 缓存吸收）看不出来，所以单文件
    跑、或同批文件上一次跑，都正常。**根因是测试依赖外网可达性 + 无上限的
    重试/超时叠加，不是某个用例本身有 bug。**

对策（只改测试基建，不动 product code、不删用例、不加 skip）
------------------------------------------------------------
这些用例本身**必须**用真实行情（/api/plan、/api/bus、/api/paper-ledger
断言 200 + 真实字段），所以不能把网络全禁掉。改为「有界网络」：

1. **单次连接超时上限**（默认 5s）：urllib3 自己 `sock.settimeout()` 设的
   8s/10s/25s 被压到 ≤5s，单次失败不再无限期挂住；
2. **慢连接熔断**：累计 3 次 >4s 的慢连接/解析，断网 120 秒（立即抛 OSError
   → 各产品的 try/except 走降级路径），之后自动恢复。这样最坏情况只等
   3 次慢连接（约 10~15s），而不是十几分钟；
3. **取数层退避 sleep 归零**：`feeds`/`theme_pool` 等模块里的
   `time.sleep(0.35*attempt)` 是给真实网络抖动留的；被熔断后重试只会空等
   （实测单次 `feeds.all_quotes()` 空等 12.7 秒），测试进程内把这几层
   模块自己引用的 `time.sleep` 置为空操作。
4. 网络正常时行为与修复前一致（仍走真实行情、断言一条未改），只是不再
   可能被批量超时拖死。需要完全还原真实网络（人工排查用）：
       $env:DSH_TEST_ALLOW_NETWORK=1
   即可关闭以上全部硬化。

三、隔离：测试不得写生产账本（2026-11-16 同批修复）
----------------------------------------------------
`order_engine.build_all()` / `/api/paper-ledger` 会调用无注入参数的
`live_sim.sync()`，读写真实 `data/paper/sim_state.json` 并结算真实持仓：
跑一次测试就会把当天二板票写成生产委托，还可能替用户「平仓」。
本 conftest 里 session 级 autouse fixture 只把 STATE_FILE / SNAPSHOT
两个写入点重定向到临时目录（读取的真实池子/日线不变），
避免测试污染生产账本、也避免账本漂移导致 `test_paper_ledger` 随机红。

四、辅助诊断工具
----------------
* `tools/qa_pytest_watchdog.py`：`-p qa_pytest_watchdog` 加载，用例超时无进展
  会 dump 全线程栈并强杀（这次定位 51% 卡死就靠它）。
* `tools/qa_pytest_network_guard.py`：更激进的「完全禁网」插件，只用于
  证明「哪些用例依赖外网」，不要用于常规跑测。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tools"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)


# ---------------------------------------------------------------------------
# 测试期出站网络硬化
# ---------------------------------------------------------------------------

_ALLOW_ENV = "DSH_TEST_ALLOW_NETWORK"
_HARDENED = os.environ.get(_ALLOW_ENV) != "1"

#: 单次连接/读取超时上限（秒）
_CONNECT_CAP = float(os.environ.get("DSH_TEST_CONNECT_CAP", "5"))
#: 超过该时长的连接/解析记为「慢」，作为熔断证据
_SLOW_SECONDS = float(os.environ.get("DSH_TEST_SLOW_SECONDS", "4"))
#: 窗口内累计多少次慢连接后熔断
_TRIP_COUNT = int(os.environ.get("DSH_TEST_TRIP_COUNT", "3"))
#: 熔断持续时间（秒），之后自动恢复（避免一次抖动把整轮测试判死）
_BREAKER_SECONDS = float(os.environ.get("DSH_TEST_BREAKER_SECONDS", "120"))

_state_lock = threading.Lock()
_slow_events: list[tuple[float, str]] = []
_breaker_until = 0.0
_hardening_installed = False
_fast_backoff_patched: list[str] = []


def _record_slow(label: str) -> None:
    """记录一次慢连接；窗口内累计到阈值就熔断一段时间。"""
    global _breaker_until

    now = time.monotonic()
    with _state_lock:
        _slow_events.append((now, label))
        cutoff = now - 60.0
        while _slow_events and _slow_events[0][0] < cutoff:
            _slow_events.pop(0)
        if len(_slow_events) >= _TRIP_COUNT and now >= _breaker_until:
            _breaker_until = now + _BREAKER_SECONDS


def _check_breaker() -> None:
    """熔断期内立即失败，让 product code 的降级路径接管。"""
    if not _breaker_until:
        return
    now = time.monotonic()
    if now < _breaker_until:
        raise OSError(
            "[tests] 出站网络熔断中（连续慢连接）；快速失败，"
            f"{_breaker_until - now:.0f}s 后自动恢复。"
            f"如需完全放开网络：设 {_ALLOW_ENV}=1"
        )


def install_network_hardening() -> bool:
    """安装「连接超时上限 + 慢连接熔断」。幂等，返回是否启用。"""
    global _hardening_installed
    if not _HARDENED:
        return False
    if _hardening_installed:
        return True

    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_settimeout = socket.socket.settimeout

    def _cap(value):
        if value is None:
            return _CONNECT_CAP
        try:
            return _CONNECT_CAP if float(value) > _CONNECT_CAP else value
        except (TypeError, ValueError):
            return _CONNECT_CAP

    def guarded_getaddrinfo(host, *args, **kwargs):
        _check_breaker()
        start = time.monotonic()
        try:
            return real_getaddrinfo(host, *args, **kwargs)
        finally:
            if time.monotonic() - start >= _SLOW_SECONDS:
                _record_slow(f"dns:{host}")

    def guarded_create_connection(address, timeout=None, *args, **kwargs):
        _check_breaker()
        start = time.monotonic()
        try:
            return real_create_connection(address, _cap(timeout), *args, **kwargs)
        finally:
            if time.monotonic() - start >= _SLOW_SECONDS:
                _record_slow(f"connect:{address}")

    def guarded_settimeout(self, value):
        return real_settimeout(self, _cap(value))

    def guarded_connect(self, address):
        _check_breaker()
        start = time.monotonic()
        try:
            return real_connect(self, address)
        finally:
            if time.monotonic() - start >= _SLOW_SECONDS:
                _record_slow(f"socket:{address}")

    def guarded_connect_ex(self, address):
        _check_breaker()
        start = time.monotonic()
        try:
            return real_connect_ex(self, address)
        finally:
            if time.monotonic() - start >= _SLOW_SECONDS:
                _record_slow(f"socket_ex:{address}")

    socket.getaddrinfo = guarded_getaddrinfo
    socket.create_connection = guarded_create_connection
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.socket.settimeout = guarded_settimeout
    _hardening_installed = True
    return True


# ---------------------------------------------------------------------------
# 取数层退避 sleep 归零（熔断后重试只需快速失败，不需要空等）
# ---------------------------------------------------------------------------

#: 内部有「网络重试 + time.sleep 退避」的取数模块
_BACKOFF_MODULES = (
    "backend.backfill",
    "backend.feeds",
    "backend.intraday_multi",
    "backend.jiuyan_source",
    "backend.social_heat",
    "backend.theme_pool",
)

_real_time = time


class _FastTime:
    """`time` 模块代理：sleep 立即返回，其余属性透传（time/time_ns/...）。"""

    @staticmethod
    def sleep(_seconds):
        return None

    def __getattr__(self, name):
        return getattr(_real_time, name)


def install_fast_backoff() -> list[str]:
    """把取数层重试的退避 sleep 置零（只替换这些模块自身的 time 引用）。"""
    if not _HARDENED:
        return []

    import importlib

    for name in _BACKOFF_MODULES:
        if name in _fast_backoff_patched:
            continue
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - 模块缺失不影响测试
            continue
        if getattr(mod, "time", None) is _real_time:
            mod.time = _FastTime()
            _fast_backoff_patched.append(name)
    return list(_fast_backoff_patched)


#: 在 conftest 导入期就装上：覆盖测试模块 import（含 app.py 的导入副作用）。
install_network_hardening()
install_fast_backoff()


def pytest_report_header(config):
    if not _HARDENED:
        return f"tests: outbound network UNHARDENED ({_ALLOW_ENV}=1)"
    return (
        f"tests: outbound network HARDENED (connect cap {_CONNECT_CAP:g}s, "
        f"breaker {_TRIP_COUNT}x>{_SLOW_SECONDS:g}s -> {_BREAKER_SECONDS:g}s; "
        f"set {_ALLOW_ENV}=1 to disable); "
        f"fast-backoff on {len(_fast_backoff_patched)} modules"
    )


# ---------------------------------------------------------------------------
# 隔离：测试不得写生产账本 data/paper/sim_state.json
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolate_live_sim_ledger(tmp_path_factory):
    """把 live_sim 的**写入目标**重定向到临时目录。

    为什么（同一次跑测实测）：
        `order_engine.build_all()` → `live_sim.sync()` 以及 `app` 的
        `/api/paper-ledger` 路由都会用**无注入参数**的 `live_sim.sync()`，
        它会读写真实的 `data/paper/sim_state.json`，并**结算真实持仓**。
        也就是说：跑一次测试就会把当天真实二板票写成生产账本里的委托/持仓，
        还可能替用户平仓（实测把 20261113 的 600467 结算成 -8.78%）。
        而 `test_paper_ledger` 的会计恒等式检查依赖该账本：一旦出现已实现
        盈亏，恒等式就因 `account` 里字段名是 `realized_net_036_yuan`、
        测试读的是 `realized_yuan`（恒为 0）而失败——这是**既有测试口径缺陷**，
        但测试污染生产账本同样是不能接受的。

    做法：只读 LIVE_DIR / PANEL 等真实数据不变（测试仍校验真实口径），
    只把 STATE_FILE / SNAPSHOT 两个写入点指向本次 session 的临时目录。
    """
    from backend import live_sim

    root = tmp_path_factory.mktemp("live_sim_ledger")
    old_state, old_snapshot = live_sim.STATE_FILE, live_sim.SNAPSHOT
    live_sim.STATE_FILE = root / "sim_state.json"
    live_sim.SNAPSHOT = root / "live_sim.json"
    try:
        yield root
    finally:
        live_sim.STATE_FILE, live_sim.SNAPSHOT = old_state, old_snapshot
