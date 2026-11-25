# -*- coding: utf-8 -*-
"""回填 11-25 卖出 trades 的环境字段（break_is_limit_down / exit_env）。

这批是「盘中信号卖出」路径产生、但写入时尚未带环境字段的记录。
按买入日归类补：broke_and_stayed → 断板性质未封住（False）。
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data" / "paper" / "sim_state.json"


def main() -> None:
    data = json.loads(STATE.read_text(encoding="utf-8"))
    n = 0
    # trades 与 positions 两份都补
    for coll in (data.get("trades") or [],
                 list((data.get("positions") or {}).values())):
        for t in coll:
            if str(t.get("exit_date", "")).replace("-", "") != "20261125":
                continue
            if t.get("break_is_limit_down") is None:
                t["break_is_limit_down"] = False   # 买入日收盘仍炸板
                n += 1
            if not t.get("exit_env"):
                t["exit_env"] = {
                    "rule": "broke_and_stayed_intraday_sell",
                    "signal_time": t.get("exit_signal_time"),
                    "signal_kind": t.get("exit_source"),
                }
    print(f"回填 {n} 条记录")
    if n:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        shutil.copy2(STATE, STATE.with_name(f"sim_state.backup-env-{ts}.json"))
        tmp = STATE.with_name("sim_state.tmp.json")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE)
        print("已写回（备份留痕）")


if __name__ == "__main__":
    main()
