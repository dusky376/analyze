# live_terminal · 悟空真经执行端

本目录是精简后的唯一应用。项目总说明见上一级 `README.md`。

## 模块边界

- **`backend/live_sim.py` 就是 E5:B1 悟空真经的实现本体**：情绪闸门 → 选股（**题材内有一字兔 → 买同题材其他二板，无组合则不开仓**）→ 排板委托 → 逐帧成交 → 断板结算 →
  持仓跨日，全部在这里。它不是「通用模拟账本」，不要改成只接收外部委托。
- `queue_match` 只回答「排板能不能成交」；`guitu_roles` 只做兔子/乌龟标注，**不参与过滤**；
  `guitu_cases` 只做案例登记与命题检验，**不产生买卖信号**。
- 前端 `web/` 只展示接口数据，不自行拼策略结论。

## 常用命令

```bat
python app.py                     :: 启动（8765）
python -m backend.guitu_backtest  :: 重建二板排板历史回放 data/guitu_sim.json
python -m backend.live_sim        :: 手动同步一次实时账本
pytest tests                      :: 测试
```

## 数据写入点

| 文件 | 写入者 | 说明 |
|---|---|---|
| `data/paper/sim_state.json` | `live_sim.sync()` | B1 委托 / 持仓 / 结算账本 |
| `data/paper/live_sim.json`   | `live_sim.sync()` | 前端快照 |
| `data/guitu_sim.json`        | `guitu_backtest.build()` | 历史回放 |
| `data/live/<day>/`           | `recorder` | 涨停池 / 行情留档（只追加） |

`data/panel_raw.parquet` **只读**；运行期不得写入，重建必须走离线流程。
