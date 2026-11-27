"""项目根 conftest：DSH 沙箱适配（2026-11-13）。

问题：pytest 默认 basetemp 在系统 Temp，沙箱不可写 → tmp_path fixture
全部 PermissionError；workspace 内目录又被挂了「禁止删除子目录」的
权限项，pytest 拿到已存在的 basetemp 会先 rmtree 再重建 → 必炸。

对策（实证有效）：
1. basetemp 指向**每次运行都不存在**的时间戳路径——rm_rf 对不存在的
   路径是空操作，之后 pytest 自己 mkdir（创建是允许的）；
2. pytest_ignore_collect 跳过 data/ 与遗留 pytest-cache-files-*（沙箱
   锁定，根级收集一扫描就 PermissionError）。

注意：tests/conftest.py 保持只有 sys.path 职责；沙箱适配只在根级做。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def pytest_ignore_collect(collection_path, config):
    try:
        p = Path(collection_path)
        name = p.name
        if "data" in p.parts or name.startswith("pytest-cache-files-"):
            return True
    except Exception:
        pass
    return None


def pytest_configure(config):
    """DSH 沙箱适配（2026-11-13，最终方案）。

    根因：pytest 的 basetemp / numbered dir 一律 `mkdir(mode=0o700)`，
    Windows 上等价于 `os.chmod(dir, 0o700)`；DSH 文件沙箱把这次 chmod
    判为危险操作，目录创建后立即对所有人拒绝访问（listdir/rmdir 全 5）。
    实证：`os.makedirs(p, mode=0o700)` 复现锁死；`os.makedirs(p)` 正常。

    对策：monkeypatch `pathlib.Path.mkdir` 的 mode（仅在本进程内），
    Windows 下 mode 本无 POSIX 语义，去掉它不影响任何真实权限。
    """
    opt = config.option
    if not getattr(opt, "basetemp", None):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        from backend import paths
        basetemp = paths.data_dir() / "_pt" / f"run-{stamp}"
        opt.basetemp = str(basetemp)
        try:
            basetemp.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    if sys.platform == "win32":
        from pathlib import Path as _P

        _orig_mkdir = _P.mkdir

        def _sandbox_mkdir(self, mode=0o777, parents=False, exist_ok=False):
            return _orig_mkdir(self, parents=parents, exist_ok=exist_ok)

        _P.mkdir = _sandbox_mkdir
