# -*- coding: utf-8 -*-
"""修复账本里"二元判定成交 vs 精确模拟未排到"的矛盾单。

用户裁定：矛盾单以精确模拟为准。
- order.status: filled → queued_unfilled
- 删除对应 position（未成交不该建仓）
- 备份原账本后原子写回
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data" / "paper" / "sim_state.json"


def main() -> None:
    if not STATE.is_file():
        print("无账本")
        return
    data = json.loads(STATE.read_text(encoding="utf-8"))
    fixed = []
    for oid, o in list((data.get("orders") or {}).items()):
        if o.get("status") != "filled":
            continue
        qs = o.get("queue_sim") or {}
        if qs.get("status") != "queued_unfilled":
            continue
        # 矛盾：已标成交，但精确模拟说没排到
        detail = str(qs.get("detail") or "")
        o["status"] = "queued_unfilled"
        o["fill_price"] = None
        o["fill_frame_ts"] = None
        o["fill_round"] = None
        o["fill_confidence"] = "none"
        o["fill_reason"] = f"[矛盾修正] 精确模拟为准：{detail[:120]}"
        o["corrected_at"] = datetime.now().isoformat(timespec="seconds")
        data.get("positions", {}).pop(oid, None)
        fixed.append((oid, o.get("name")))

    print(f"修正 {len(fixed)} 张矛盾单：")
    for oid, name in fixed:
        print(f"  {oid} {name}")

    if fixed:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        shutil.copy2(STATE, STATE.with_name(f"sim_state.backup-contradiction-{ts}.json"))
        tmp = STATE.with_name("sim_state.tmp.json")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE)
        print("已写回（备份已留）")
    else:
        print("无需修正")


if __name__ == "__main__":
    main()
