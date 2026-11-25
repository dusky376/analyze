# live_terminal · 悟空真经执行端

本目录为单策略执行端源码（数据与代码分离，数据位于上一级 `data/`）。项目总说明见 [README.md](../README.md)。

## 模块边界

- **`backend/live_sim.py`**：E5:B1 策略本体（冰点降仓 → 情绪闸门 → 龟兔选股 → 因果信息素分仓 → 逐帧撮合 → 两路径自适应卖点结算）。
- **`backend/sell_rules.py`**：两路径自适应卖点引擎（板块幅度/冰点动态阈值、竞价低开抢跑、VWAP/量能确认急跌、尾盘防核按钮）。
- **`backend/buy_signals.py`**：买点质量分级（A/B/C/REJECT）与同题材临封扫板观察雷达（只读）。
- **`backend/evolution.py`**：严格因果蚁群信息素奖惩引擎 + 侦察蚁执行遗憾反馈。
- **`backend/queue_match.py` / `fill_verify.py`**：Q037 规则与逐轮封单消耗成交核验。
- **`backend/guitu_roles.py` / `guitu_cases.py`**：龟兔角色标注与案例库（只作展示与研究）。
- **`web/`**：Apple 风格三页签单页终端，仅允许 `index.html`、`app.js`、`style.css`、`favicon.svg` 四个文件。

## 常用命令

```bat
python app.py                           :: 启动服务（8765）
python -m backend.guitu_backtest        :: 重建历史回放 data/guitu_sim.json
python -m backend.live_sim              :: 手动同步一次实时账本
python -m backend.compact_archive --all :: 压缩历史留档
pytest -q                               :: 运行全量测试
```

## 数据写入点

| 文件 | 写入者 | 说明 |
|---|---|---|
| `data/paper/sim_state.json` | `live_sim.sync()` | B1 委托 / 持仓 / 结算账本 |
| `data/paper/live_sim.json` | `live_sim.sync()` | 前端账本快照 |
| `data/guitu_sim.json` | `guitu_backtest.build()` | 二板排板历史回放 |
| `data/panel_live.parquet` | `feeds.persist_eod_bar()` | 收盘日线增量落盘 |
| `data/live/<day>/` | `recorder` / `minute_flow` | 涨停池 / 行情 / 1分钟分时留档 |

`data/panel_raw.parquet` **只读**；运行期不得覆盖。
