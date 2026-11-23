import json, sys
from pathlib import Path
ROOT = Path(r'E:\workspace\量化\live_terminal')
sys.path.insert(0, str(ROOT))
from backend import costs

print('DEFAULT describe:', json.dumps(costs.DEFAULT.describe(), ensure_ascii=False, default=str))
print()
d = json.loads((ROOT/'data'/'paper'/'sim_state.json').read_text(encoding='utf-8'))
for t in d['trades']:
    g = t['gross_return']; n = t.get('notional')
    exact = costs.DEFAULT.round_trip_rate_for_return(n, g) if n else costs.DEFAULT.round_trip_rate(None)
    print(f"{t['name']:<8} gross={g*100:+.2f}% cost_old={t['cost_rate']*100:.3f}% "
          f"exact_rate={exact*100:.3f}% exact_net={(g-exact)*100:+.2f}% stored_r_net={t['r_net']*100:+.2f}%")
