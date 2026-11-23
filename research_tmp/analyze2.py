"""补充分析：当前代码 sync 结果、题材池覆盖、兔子自排除、未成交强票。"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path
ROOT = Path(r'E:\workspace\量化\live_terminal')
sys.path.insert(0, str(ROOT))
import polars as pl
from backend import live_sim, guitu_roles, feature_engine, costs, theme_pool

DAYS = ['20261116','20261117','20261118','20261120']
_tmp = Path(tempfile.mkdtemp(prefix='wukong_research2_'))
live_sim.STATE_FILE = _tmp/'sim_state.json'
live_sim.SNAPSHOT = _tmp/'live_sim.json'
print('[isolated]', _tmp, file=sys.stderr)

# 1) 题材池覆盖
print('\n=== 题材池日期覆盖 ===')
df = pl.read_parquet(ROOT/'data'/'themes'/'themes.parquet')
dates = sorted(set(df['date'].to_list()))
print('themes dates count:', len(dates), 'first:', dates[0], 'last:', dates[-1])
for d in ['2026-11-13','2026-11-16','2026-11-17','2026-11-18','2026-11-20']:
    sub = df.filter(pl.col('date')==d)
    print(d, 'rows=', sub.height, 'zt rows=', sub.filter(pl.col('pool')=='zt').height)

# 2) 当前代码 sync 结果（隔离账本）
print('\n=== 当前代码 sync(day) 结果（隔离账本）===')
sync_results = {}
for day in DAYS:
    out = live_sim.sync(day)
    sync_results[day] = out
    print(f"\n[{day}] frames={out['frames']} n_today={out['n_today']} filled={out['n_filled_today']} unfilled={out['n_unfilled_today']} unknown={out['n_unknown_today']}")
    print('  notes:', ' | '.join(out['notes']))
    for o in out['today_orders']:
        print(f"  {o['ts_code']} {o['name']:<8} status={o['status']:<16} role={(o.get('guitu') or {}).get('role'):<10} "
              f"theme={o.get('guitu_theme')} rabbits={o.get('guitu_rabbits')} broken_at_entry={o.get('broken_at_entry')} "
              f"broken_max={o.get('broken_max')} fill_conf={o.get('fill_confidence')} fill_reason={str(o.get('fill_reason'))[:60]}")

# 3) 当前账本完整 Orders/Positions/Trades
state = json.loads(live_sim.STATE_FILE.read_text(encoding='utf-8'))
print('\n=== 隔离账本最终状态 ===')
print('n orders', len(state['orders']), 'n positions', len(state['positions']), 'n trades', len(state['trades']))
for t in state['trades']:
    print(' trade', t['id'], t['name'], 'gross', t.get('gross_return'), 'net', t.get('net_return'))
for oid,p in state['positions'].items():
    if p.get('status')!='closed':
        print(' holding', oid, p.get('name'), p.get('status'))

# 4) 对每一天列出兔子/乌龟（用当前代码 is_rabbit）
print('\n=== 兔子判定明细（当日全部涨停股中 streak==2 且形态满足）===')
for day in DAYS:
    frames = live_sim.read_frames(day)
    allzt = live_sim.collect_all_zt(frames)
    rabbits = [(c,g['name'],g.get('broken_max'),g.get('first_seal'),g.get('turnover')) for c,g in allzt.items() if live_sim.is_rabbit(g)]
    oneword_all = [(c,g['name'],g.get('streak'),g.get('broken_max'),g.get('first_seal'),g.get('turnover')) for c,g in allzt.items() if live_sim.is_oneword(g)]
    print(f"\n[{day}] 兔子(streak2+一字)={len(rabbits)}: {[ (c,n) for c,n,_,_,_ in rabbits]}")
    print(f"  一字形态(不限板位)={len(oneword_all)}: {[(c,n,'S'+str(s)) for c,n,s,_,_,_ in oneword_all]}")

# 5) 11-18 算力题材组详情（谁被当兔子、谁被当乌龟）
print('\n=== 11-18 算力题材组 ===')
day='20261118'
frames = live_sim.read_frames(day)
allzt = live_sim.collect_all_zt(frames)
tags = live_sim._theme_tags(day)
two_map, _ = live_sim.collect_two_board(frames)
for c, g in sorted(allzt.items()):
    tt = tags.get(c) or []
    if ('算力' in tt) and g.get('streak')==2:
        print(f"  {c} {g['name']:<8} rabbit={live_sim.is_rabbit(g)} streak={g.get('streak')} "
              f"broken={g.get('broken_max')} first_seal={g.get('first_seal')} turnover={round(g.get('turnover') or 0,2)} "
              f"in_last={g.get('in_last_frame')} tags={tt}")

# 6) 11-16 算力题材组详情（华东重机为何无兔）
print('\n=== 11-16 算力题材组 ===')
day='20261116'
frames = live_sim.read_frames(day)
allzt = live_sim.collect_all_zt(frames)
tags = live_sim._theme_tags(day)
for c, g in sorted(allzt.items()):
    tt = tags.get(c) or []
    if ('算力' in tt) and g.get('streak')==2:
        print(f"  {c} {g['name']:<8} rabbit={live_sim.is_rabbit(g)} streak={g.get('streak')} "
              f"broken={g.get('broken_max')} first_seal={g.get('first_seal')} turnover={round(g.get('turnover') or 0,2)} "
              f"in_last={g.get('in_last_frame')} tags={tt}")
print('  11-16 全部一字形态（不限题材/板位）:')
for c,g in sorted(allzt.items()):
    if live_sim.is_oneword(g):
        print(f"    {c} {g['name']:<8} streak={g.get('streak')} broken={g.get('broken_max')} first_seal={g.get('first_seal')} turnover={round(g.get('turnover') or 0,2)} tags={tags.get(c)}")

# 7) 检查 fill_verify 对未成交票的判定
print('\n=== fill_verify 对 11-18 未成交强票的判定 ===')
from backend import fill_verify
day='20261118'
frames = live_sim.read_frames(day)
for code, name in [('002771','真视通'),('600410','华胜天成'),('301391','卡莱特')]:
    # 找首帧
    entry_ts=None
    for fr in frames:
        rows,_ = live_sim._pool_rows(fr)
        for r in rows:
            if str(r.get('ts_code') or '').zfill(6)==code:
                entry_ts=fr.get('ts'); break
        if entry_ts: break
    sim = fill_verify.simulate_fill(day, code, str(entry_ts), frames, entry_price=None)
    print(f"  {code} {name} entry_ts={entry_ts}")
    print('   ', json.dumps(sim, ensure_ascii=False)[:600])

# 8) 六笔亏损.background: 11-13/11-16 的委托是老口径（全二板），验证 guitu_theme 缺失
prod = json.loads((ROOT/'data'/'paper'/'sim_state.json').read_text(encoding='utf-8'))
print('\n=== 生产账本 orders 的 guitu_theme 字段（验证老口径）===')
for oid,o in prod['orders'].items():
    print(f"  {oid} theme={o.get('guitu_theme')} rabbits={o.get('guitu_rabbits')} role={(o.get('guitu') or {}).get('role')} broken_at_entry={o.get('broken_at_entry')}")
