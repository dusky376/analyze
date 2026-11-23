import json, sys
from pathlib import Path
ROOT = Path(r'E:\workspace\量化\live_terminal')
sys.path.insert(0, str(ROOT))
from backend import live_sim, fill_verify, costs

prod = json.loads((ROOT/'data'/'paper'/'sim_state.json').read_text(encoding='utf-8'))

# 华东重机 11-16 未成交原因
print('=== 华东重机 002685 11-16 生产委托 ===')
o = prod['orders'].get('20261116|002685|B1')
if o:
    print('status:', o.get('status'))
    print('fill_reason:', o.get('fill_reason'))
    print('queue_sim:', json.dumps(o.get('queue_sim'), ensure_ascii=False))
    print('entry_frame_ts:', o.get('entry_frame_ts'))
    print('broken_at_entry:', o.get('broken_at_entry'))

# 用当前代码重算 11-16 的 华东重机 fill_verify
frames = live_sim.read_frames('20261116')
entry_ts = None
for fr in frames:
    rows,_ = live_sim._pool_rows(fr)
    for r in rows:
        if str(r.get('ts_code') or '').zfill(6)=='002685':
            entry_ts = fr.get('ts'); break
    if entry_ts: break
print('\n重算 fill_verify entry_ts=', entry_ts)
print(json.dumps(fill_verify.simulate_fill('20261116','002685',str(entry_ts),frames,entry_price=6.09), ensure_ascii=False))

# 凯撒文化 11-16 未成交原因（当前代码选中但 queued_unfilled）
print('\n=== 凯撒文化 002425 11-16 生产委托 ===')
o = prod['orders'].get('20261116|002425|B1')
if o:
    print('status:', o.get('status'))
    print('fill_reason:', o.get('fill_reason'))
    print('queue_sim:', json.dumps(o.get('queue_sim'), ensure_ascii=False))

# 11-18 真视通后续最高连板（验证 5 板）
print('\n=== 真视通/盛剑科技 后续日线 ===')
import polars as pl
raw = pl.read_parquet(ROOT/'data'/'panel_raw.parquet')
live = pl.read_parquet(ROOT/'data'/'panel_live.parquet')
panel = pl.concat([raw,live],how='vertical').unique(subset=['trade_date','ts_code'],keep='last')
for code in ['002771','603324','002685','603030','002579','002242']:
    df = panel.filter(pl.col('ts_code')==code).sort('trade_date')
    print(f'\n{code}:')
    for r in df.filter(pl.col('trade_date')>=pl.lit('2026-11-18').cast(pl.Date)).iter_rows(named=True):
        print(f"  {r['trade_date']} close={r['close']} pct={r['pct_chg']:+.2f}%")
