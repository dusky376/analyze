# -*- coding: utf-8 -*-
"""修复历史账本：按万 0.84 免五单一口径补写 net_return（不删 r_net 留档）。

用户 2026-11-23 裁定：以万0.84单一口径为准。
历史 trades 的 net_return 全为 null，导致 by_cost 聚合回退到旧 r_net(0.402%)。
本脚本只**补字段**，不删原 r_net / cost_rate，保证可追溯。

用法：python tools/fix_net_return.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import costs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data" / "paper" / "sim_state.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not STATE.is_file():
        print(f"账本不存在：{STATE}")
        return
    data = json.loads(STATE.read_text(encoding="utf-8"))
    rate = costs.DEFAULT.round_trip_rate()
    print(f"万0.84免五单一口径 cost_rate = {rate:.6f} ({rate:.4%})")

    rows = []
    rows.extend(data.get("trades") or [])
    rows.extend((data.get("positions") or {}).values())
    n_fixed = 0
    for r in rows:
        g = r.get("gross_return")
        if not isinstance(g, (int, float)) or isinstance(g, bool):
            continue
        new_net = round(float(g) - rate, 6)
        old = r.get("net_return")
        if old is None or abs(float(old) - new_net) > 1e-9:
            # 保留旧口径留档，避免历史不可追溯
            if r.get("r_net") is not None and "r_net_note" not in r:
                r["r_net_note"] = "历史口径 0.402%（万2.5+最低5元），仅留档；净收益以 net_return 为准"
            r["net_return"] = new_net
            r["net_basis"] = "万0.84免五单一口径（用户 2026-11-23 裁定）"
            n_fixed += 1
            print(f"  {r.get('ts_code')} {r.get('name')} gross={g:+.6f} "
                  f"net_return={new_net:+.6f} (旧 r_net={r.get('r_net')})")

    print(f"\n需修正 {n_fixed} 条")
    if args.dry_run:
        print("（dry-run，未写盘）")
        return
    if n_fixed:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        shutil.copy2(STATE, STATE.with_name(f"sim_state.backup-{ts}.json"))
        tmp = STATE.with_name("sim_state.tmp.json")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE)
        print(f"已写回（备份 sim_state.backup-{ts}.json）")
    else:
        print("无需改动")


if __name__ == "__main__":
    main()
