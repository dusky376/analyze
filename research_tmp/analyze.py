"""只读研究脚本：亏损复盘 + 同期未买入二板 + 漏网原因分析。
不修改任何生产数据，所有 sync 写到 tempfile 隔离账本。
"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path

ROOT = Path(r'E:\workspace\量化\live_terminal')
sys.path.insert(0, str(ROOT))

import polars as pl
import numpy as np

from backend import live_sim, feature_engine, costs, guitu_roles, theme_pool

DAYS = ['20261116', '20261117', '20261118', '20261120']
LEDGER = ROOT / 'data' / 'paper' / 'sim_state.json'

# ---------------- 隔离账本 ----------------
_tmp = Path(tempfile.mkdtemp(prefix='wukong_research_'))
live_sim.STATE_FILE = _tmp / 'sim_state.json'
live_sim.SNAPSHOT = _tmp / 'live_sim.json'
print(f'[isolated ledger] {_tmp}', file=sys.stderr)

# ---------------- 读生产账本（只读）----------------
prod = json.loads(LEDGER.read_text(encoding='utf-8'))

# 真实日线合并视图
import polars as pl
raw = pl.read_parquet(ROOT/'data'/'panel_raw.parquet')
live = pl.read_parquet(ROOT/'data'/'panel_live.parquet')
panel = pl.concat([raw, live], how='vertical').unique(subset=['trade_date','ts_code'], keep='last')
panel = panel.with_columns(pl.col('trade_date').cast(pl.Utf8).alias('d'))
# 交易日历（面板里所有日期排序）
trade_days = sorted(set(panel['d'].to_list()))
print(f'[panel] rows={panel.height} days={trade_days[0]}..{trade_days[-1]} n_days={len(trade_days)}', file=sys.stderr)

def norm_day(d):
    d = str(d).replace('-','')
    return d

def dash(d):
    d = norm_day(d)
    return f'{d[:4]}-{d[4:6]}-{d[6:]}'

# 每只票的日线序列
def daily_for(code):
    df = panel.filter(pl.col('ts_code')==code).sort('d')
    if df.height==0: return []
    rows = []
    closes = df['close'].to_numpy().astype(float)
    dates = df['d'].to_list()
    zt, streak = feature_engine.zt_flags(code, closes)
    lim = feature_engine.limit_pct(code)
    for i in range(len(dates)):
        prev = closes[i-1] if i>0 else float('nan')
        pct = (closes[i]/prev-1.0) if prev==prev and prev>0 else float('nan')
        rows.append({
            'date': dates[i], 'close': float(closes[i]),
            'open': float(df['open'][i]), 'high': float(df['high'][i]), 'low': float(df['low'][i]),
            'pct_chg': float(pct), 'zt': bool(zt[i]), 'streak': int(streak[i]),
            'is_dt': bool(pct <= -(lim-0.003)) if pct==pct else False,
        })
    return rows

# =====================================================
# PART A: 生产账本已结算 trades
# =====================================================
print('\n' + '='*100)
print('PART A: 生产账本已结算 trades')
print('='*100)
trades = prod['trades']
print(f'n_trades = {len(trades)}')
# 收集委托单建单时的题材/兔子锚点证据（从冻结账本 orders 里取，不重新推断）
orders = prod['orders']

def rabbit_context(order):
    """从委托单里提取兔子锚点/闸门等上下文；老单很多字段缺。"""
    o = order or {}
    return {
        'guitu_theme': o.get('guitu_theme'),
        'guitu_rabbits': o.get('guitu_rabbits'),
        'role': (o.get('guitu') or {}).get('role'),
        'role_cn': (o.get('guitu') or {}).get('role_cn'),
        'entry_frame_ts': o.get('entry_frame_ts'),
        'broken_at_entry': o.get('broken_at_entry'),
        'broken_max': o.get('broken_max'),
    }

A_rows = []
for t in trades:
    oid = t['id']
    ctx = rabbit_context(orders.get(oid))
    A_rows.append({
        'id': oid,
        'entry_date': t.get('entry_date'),
        'code': t.get('ts_code'),
        'name': t.get('name'),
        'role': (t.get('guitu') or {}).get('role_cn'),
        'industry': t.get('industry'),
        'theme': ctx['guitu_theme'],
        'rabbits': ctx['guitu_rabbits'],
        'entry_price': t.get('entry_price'),
        'exit_date': t.get('exit_date'),
        'exit_price': t.get('exit_price'),
        'gross_return': t.get('gross_return'),
        'r_net': t.get('r_net'),
        'net_return': t.get('net_return'),
        'exit_blocked': t.get('exit_blocked'),
        'exit_blocked_days': t.get('exit_blocked_days'),
        'exit_blocked_dates': t.get('exit_blocked_dates'),
        'exit_candidate_date': t.get('exit_candidate_date'),
        'exit_basis': t.get('exit_price_basis'),
        'notional': t.get('notional'),
        'qty': t.get('qty'),
        'cost_rate': t.get('cost_rate'),
        'broken_max': (t.get('guitu') or {}).get('evidence',{}).get('broken'),
        'first_seal': (t.get('guitu') or {}).get('evidence',{}).get('first_seal'),
        'last_seal': (t.get('guitu') or {}).get('evidence',{}).get('last_seal'),
        'turnover': (t.get('guitu') or {}).get('evidence',{}).get('turnover'),
        'seal_fund_yi': (t.get('guitu') or {}).get('evidence',{}).get('seal_fund_yi'),
        'ctx': ctx,
    })

# print table
hdr = f"{'买入日':<9}{'代码':<8}{'名称':<10}{'角色':<10}{'行业/题材':<14}{'买价':>7}{'卖日':<12}{'卖价':>7}{'毛收益':>9}{'净收益(r_net)':>13}{'顺延':>5}"
print(hdr)
for r in A_rows:
    print(f"{r['entry_date']:<9}{r['code']:<8}{r['name']:<10}{str(r['role']):<10}{str(r['industry']):<14}"
          f"{r['entry_price']:>7}{str(r['exit_date']):<12}{r['exit_price']:>7}"
          f"{r['gross_return']*100:>8.2f}%{r['r_net']*100:>12.2f}%{str(r['exit_blocked_days']):>5}")

print('\n--- 亏损笔明细（含顺延/证据）---')
for r in A_rows:
    if r['gross_return'] < 0:
        print(f"\n[{r['id']}] {r['name']} 毛 {r['gross_return']*100:+.2f}% 净 {r['r_net']*100:+.2f}%")
        print(f"  证据: broken={r['broken_max']} first_seal={r['first_seal']} last_seal={r['last_seal']} "
              f"turnover={r['turnover']} seal_fund_yi={r['seal_fund_yi']}")
        print(f"  委托上下文: {r['ctx']}")
        print(f"  卖出: {r['exit_basis']}  blocked_days={r['exit_blocked_days']} dates={r['exit_blocked_dates']} candidate={r['exit_candidate_date']}")
        # 计算断板日当天的实际跌幅 vs 顺延后
        d = daily_for(r['code'])
        by_date = {x['date']:x for x in d}
        if r['exit_candidate_date'] in by_date:
            cd = by_date[r['exit_candidate_date']]
            print(f"  断板日 {r['exit_candidate_date']}: close={cd['close']} pct_chg={cd['pct_chg']*100:+.2f}% is_dt={cd['is_dt']}")
        if r['exit_date'] and dash(r['exit_date']) in by_date:
            xd = by_date[dash(r['exit_date'])]
            print(f"  实际卖出日 {r['exit_date']}: close={xd['close']} pct_chg={xd['pct_chg']*100:+.2f}%")

# 计算若按断板日收盘卖出的“理论毛收益”（对比顺延损失）
print('\n--- 断板日 vs 顺延卖出 对比（毛收益）---')
for r in A_rows:
    if r['gross_return'] >= 0: continue
    d = daily_for(r['code'])
    by_date = {x['date']:x for x in d}
    cand = r['exit_candidate_date']
    if cand and cand in by_date and r['entry_price']:
        px = by_date[cand]['close']
        theoretical = px/r['entry_price']-1
        print(f"{r['name']:<8} entry={r['entry_price']} 断板日{cand}收{px} 理论毛{theoretical*100:+.2f}% vs 实盘毛{r['gross_return']*100:+.2f}% 顺延差{(r['gross_return']-theoretical)*100:+.2f}pp")

# =====================================================
# PART B: 同期二板全景（4 个留档日）
# =====================================================
print('\n'+'='*100)
print('PART B: 同期二板全景（collect_all_zt / collect_guitu_targets）')
print('='*100)

# 闸门指标（对 4 天）
print('\n--- 情绪闸门（对 4 天）---')
gate_results = {}
for day in DAYS:
    g = live_sim.emotion_gate(day)
    gate_results[day] = g
    print(day, json.dumps(g, ensure_ascii=False))

# 对每一天：读帧 -> all_zt -> 二板 -> 选中目标
all_data = {}  # day -> dict
for day in DAYS:
    frames = live_sim.read_frames(day)
    allzt = live_sim.collect_all_zt(frames)
    two = {c:g for c,g in allzt.items() if g.get('streak')==2}
    picked, notes = live_sim.collect_guitu_targets(frames, day)
    picked_set = set(picked.keys())
    # 闸门是否拦截
    gate = gate_results[day]
    gate_blocked = bool(gate.get('available') and gate.get('blocked'))
    all_data[day] = {
        'frames': frames, 'allzt': allzt, 'two': two, 'picked': picked,
        'picked_set': picked_set, 'notes': notes, 'gate_blocked': gate_blocked,
        'gate': gate,
    }
    print(f"\n[{day}] frames={len(frames)} n_zt={len(allzt)} n_two={len(two)} n_picked={len(picked)} gate_blocked={gate_blocked}")
    print('  notes:', ' | '.join(notes))
    print(f"  {'代码':<8}{'名称':<10}{'末帧在池':<8}{'broken_max':<10}{'first_seal':<10}{'turnover':<9}{'seal_fund':<12}{'角色':<10}{'选中':<6}")
    for c,g in sorted(two.items()):
        last = g['last']
        sf = last.get('seal_fund')
        role = guitu_roles.classify_role({
            'broken': g.get('broken_max'), 'first_seal': g.get('first_seal'),
            'last_seal': last.get('last_seal'), 'turnover': g.get('turnover'),
            'seal_fund': sf,
        })
        print(f"  {c:<8}{g['name']:<10}{str(g.get('in_last_frame')):<8}{str(g.get('broken_max')):<10}{str(g.get('first_seal')):<10}{str(round(g.get('turnover') or 0,2)):<9}{str(sf):<12}{role.get('role','?'):<10}{'✓' if c in picked_set else '✗':<6}")

# ---------------- 后续表现 ----------------
def forward_perf(code, entry_day):
    """从真实日线算：次日收益、是否晋级涨停、之后最高连板、断板前最大涨幅、后续最高价涨幅。"""
    d = daily_for(code)
    idx = None
    for i,x in enumerate(d):
        if norm_day(x['date'])==entry_day:
            idx=i; break
    if idx is None or idx+1>=len(d):
        return None
    entry_close = d[idx]['close']
    nxt = d[idx+1]
    next_ret = nxt['close']/entry_close-1.0
    # 从 entry 日之后（含次日）观察
    after = d[idx+1:]
    # 晋级涨停：次日是否 zt
    next_zt = bool(nxt['zt'])
    # 之后最高连板
    max_streak = max((x['streak'] for x in after), default=0)
    # 断板前最大涨幅：从 entry_close 到之后最高 close 的涨幅（直到首个断板日收盘）
    first_break_idx = None
    for k,x in enumerate(after):
        if not x['zt']:
            first_break_idx=k; break
    if first_break_idx is not None:
        window = after[:first_break_idx+1]
    else:
        window = after
    max_close = max((x['high'] for x in window), default=entry_close)
    max_gain = max_close/entry_close-1.0
    # 全局最高涨幅（含之后所有日）
    max_close_all = max((x['high'] for x in after), default=entry_close)
    max_gain_all = max_close_all/entry_close-1.0
    # 断板日
    brk = after[first_break_idx] if first_break_idx is not None else None
    return {
        'next_ret': next_ret,
        'next_zt': next_zt,
        'max_streak_after': max_streak,
        'max_gain_to_break': max_gain,
        'max_gain_all': max_gain_all,
        'break_date': brk['date'] if brk else None,
        'break_ret_from_entry': (brk['close']/entry_close-1.0) if brk else None,
        'n_after': len(after),
    }

# 列表 B：每个二板 + 选中 + 后续表现
print('\n--- B 表：全部二板 + 选中 + 后续表现 ---')
B_rows = []
for day in DAYS:
    dd = all_data[day]
    for c,g in sorted(dd['two'].items()):
        pf = forward_perf(c, day)
        picked = c in dd['picked_set']
        B_rows.append({'day':day,'code':c,'name':g['name'],'picked':picked,'g':g,'pf':pf,
                       'gate_blocked':dd['gate_blocked'],'notes':dd['notes']})

print(f"{'日':<9}{'代码':<8}{'名称':<10}{'选中':<5}{'次收益':>8}{'晋级':<5}{'最高连板':>8}{'到断最大涨':>10}{'全局最大涨':>10}{'断板日':<12}")
for r in B_rows:
    pf = r['pf'] or {}
    print(f"{r['day']:<9}{r['code']:<8}{r['name']:<10}{('✓' if r['picked'] else '✗'):<5}"
          f"{(pf.get('next_ret') or 0)*100:>7.2f}%{str(pf.get('next_zt')):<5}{pf.get('max_streak_after',0):>8}"
          f"{(pf.get('max_gain_to_break') or 0)*100:>9.2f}%{(pf.get('max_gain_all') or 0)*100:>9.2f}%"
          f"{str(pf.get('break_date')):<12}")

# ---------------- 未选中原因分析 ----------------
print('\n--- C：未选中原因 ---')
theme_tags_cache = {}
for day in DAYS:
    theme_tags_cache[day] = live_sim._theme_tags(day)

def why_not_selected(day, code, g, dd):
    reasons = []
    tags = theme_tags_cache.get(day) or {}
    my_tags = tags.get(code) or []
    allzt = dd['allzt']
    # 闸门
    if dd['gate_blocked']:
        reasons.append('情绪闸门拦截')
    # ST
    if feature_engine.is_st(g['name']):
        reasons.append('ST过滤')
    # 末帧在池
    if not g.get('in_last_frame'):
        reasons.append('末帧不在池(炸板)')
    # 找题材内兔子
    rabbit_found = []
    for t in my_tags:
        for c2,g2 in allzt.items():
            if c2==code: continue
            if t in (tags.get(c2) or []):
                if live_sim.is_rabbit(g2):
                    rabbit_found.append((t,c2,g2['name']))
    if not my_tags:
        reasons.append('无题材标签')
    elif not rabbit_found:
        reasons.append('同题材无一字兔(兔子形态:broken=0,first_seal<=93000,turnover<5,streak=2)')
    else:
        reasons.append(f'有兔子但未被选({rabbit_found})')
    return reasons, my_tags, rabbit_found

C_rows = []
for r in B_rows:
    if r['picked']: continue
    reasons, tags, rabbits = why_not_selected(r['day'], r['code'], r['g'], all_data[r['day']])
    C_rows.append({**r, 'reasons':reasons, 'tags':tags, 'rabbits':rabbits})

for r in C_rows:
    pf = r['pf'] or {}
    print(f"\n[{r['day']} {r['code']} {r['name']}]")
    print(f"  why: {' / '.join(r['reasons'])}")
    print(f"  tags={r['tags']} rabbits={[(t,c,n) for t,c,n in r['rabbits']]}")
    print(f"  evidence: broken={r['g'].get('broken_max')} first_seal={r['g'].get('first_seal')} "
          f"turnover={r['g'].get('turnover')} in_last={r['g'].get('in_last_frame')} "
          f"seal_fund={r['g']['last'].get('seal_fund')} n_frames={r['g'].get('n_frames')}")
    print(f"  后续: next_ret={(pf.get('next_ret') or 0)*100:+.2f}% next_zt={pf.get('next_zt')} "
          f"max_streak={pf.get('max_streak_after')} max_gain_to_break={(pf.get('max_gain_to_break') or 0)*100:+.2f}% "
          f"max_gain_all={(pf.get('max_gain_all') or 0)*100:+.2f}%")

# ---------------- 找漏网强票 ----------------
print('\n--- D：漏网强票（未选中 且 后续表现强，如 next_zt 或 max_gain>=15%）---')
strong = []
for r in C_rows:
    pf = r['pf'] or {}
    if pf.get('next_zt') or (pf.get('max_gain_all') or 0)>=0.15 or (pf.get('next_ret') or 0)>=0.05:
        strong.append(r)
for r in strong:
    pf = r['pf']
    print(f"[{r['day']} {r['code']} {r['name']}] next_ret={pf['next_ret']*100:+.2f}% "
          f"next_zt={pf['next_zt']} max_streak={pf['max_streak_after']} "
          f"max_gain_to_break={pf['max_gain_to_break']*100:+.2f}% max_gain_all={pf['max_gain_all']*100:+.2f}% "
          f"why={' / '.join(r['reasons'])}")

# ---------------- 导出结构化结果 ----------------
out = {
    'trade_days': trade_days,
    'A_rows': [{k:v for k,v in r.items() if k!='ctx'} for r in A_rows],
    'gate_results': gate_results,
    'DAYS': DAYS,
    'B_rows': [{
        'day':r['day'],'code':r['code'],'name':r['name'],'picked':r['picked'],
        'gate_blocked':r['gate_blocked'],
        'broken_max':r['g'].get('broken_max'),'first_seal':r['g'].get('first_seal'),
        'last_seal':r['g']['last'].get('last_seal'),
        'turnover':r['g'].get('turnover'),'seal_fund':r['g']['last'].get('seal_fund'),
        'in_last_frame':r['g'].get('in_last_frame'),'n_frames':r['g'].get('n_frames'),
        'industry':r['g'].get('industry'),
        'pf':r['pf'],
    } for r in B_rows],
    'C_rows': [{
        'day':r['day'],'code':r['code'],'name':r['name'],'reasons':r['reasons'],
        'tags':r['tags'],'rabbits':r['rabbits'],'pf':r['pf'],
    } for r in C_rows],
    'notes_by_day': {d:all_data[d]['notes'] for d in DAYS},
    'strong': [f"{r['day']}|{r['code']}|{r['name']}" for r in strong],
}
outpath = Path(r'E:\workspace\量化\research_tmp\analysis.json')
outpath.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
print(f'\n[saved] {outpath}')
