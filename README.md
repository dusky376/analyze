# 悟空真经 · 二板排板终端

> 从原「量化 / live_terminal」全策略决策台提炼出的**单策略**版本，只保留悟空真经（E5:B1）：
> 选股、排板撮合、断板结算、龟兔案例与历史回放。其余策略、LLM、威科夫研究仓、情绪周期、
> 冰点二板、题材雷达等已删除（删除前全量备份见 `E:\workspace\_archive\`）。

## 一、策略口径（唯一）

| 项目 | 规则 |
|---|---|
| 选股 | **题材内有一字二板（兔子）→ 买同题材其他二板（乌龟）；无组合则不开仓**。一字兔子按 Q037 买不进 |
| 买点 | **二板封板瞬间以涨停价挂限价单排队**，不默认排到就成交 |
| 成交判定 | 按东财 Q037：不开板不成交；开板且卖一有单才成交，成交价=涨停价 |
| 卖点 | **首个未涨停日（断板）当日收盘卖出**；断板后不持有 |
| 成本 | 0.26% / 0.36% / 0.20% 三口径并列，不擅自统一（用户裁定⑥） |
| 边界 | 只写本地模拟账本，**永不向任何券商/账户发送交易指令** |

## 二、目录

```
量化/
├─ start.bat / stop.bat          启动 / 停止（ASCII，CRLF）
├─ 踩坑与复利.md                 项目级记录
└─ live_terminal/
   ├─ app.py                     最小 Flask 入口（6 个接口）
   ├─ backend/                   悟空真经算法 + 最小运行底座
   │   ├─ live_sim.py            B1 实时模拟账本（核心状态机）
   │   ├─ guitu_roles.py         兔子/乌龟/盘子偏大 角色代理
   │   ├─ guitu_cases.py         龟兔案例库 / 命题检验
   │   ├─ guitu_backtest.py      二板排板历史回放
   │   ├─ queue_match.py         Q037 排板撮合
   │   ├─ fill_verify.py         逐帧排板成交核验
   │   ├─ auction_signal.py      集合竞价 → 悟空二板决策输入
   │   ├─ feeds.py / recorder.py 真实行情 / 涨停池采集与留档
   │   ├─ feature_engine.py / costs.py / dates.py / paths.py
   │   ├─ leader_ladder.py / leader_casebook.py  案例库支撑
   │   └─ theme_pool.py / limit_counts.py / trading_rules.py
   ├─ web/                       单页前端（index.html + app.js + style.css）
   ├─ configs/                   题材轴 / 概念映射
   ├─ data/                      真实落盘：panel / 涨停池留档 / 账本 / 题材
   └─ tests/                     悟空真经契约与算法测试
```

## 三、启动

```bat
cd /d E:\workspace\量化
start.bat
```

打开 <http://127.0.0.1:8765>。停止：`stop.bat`。

依赖：Python 3.11+，`flask polars pandas pyarrow requests numpy`。
可用 `WUKONG_PYTHON` 指定解释器。

## 四、接口

> 共 10 个路由；前端只用其中 5 个（health / live-sim / guitu / guitu-cases / refresh）。

| 接口 | 内容 |
|---|---|
| `GET /` | 单页终端（`index.html`，4 区块：今日/账本/回放/龟兔） |
| `GET /favicon.svg` | 图标 |
| `GET /api/version` | 进程 build / 磁盘 build / stale |
| `GET /api/health` | 面板、留档、采集器、交易时钟状态 |
| `GET /api/live-sim` | 今日二板委托、持仓、结算、成本三口径 |
| `GET /api/guitu` | 历史回放 + 今日实时排板队列 |
| `GET /api/guitu-cases` | 龟兔案例库 / 今日快照 / 命题检验（只读） |
| `GET /api/recorder` | 采集器状态 |
| `POST /api/refresh` | 清缓存并重新同步 |
| `GET /api/stream` | SSE 每 30s 推送今日开仓简报（前端可降级为轮询） |

**实际选股口径**（以 `live_sim.collect_guitu_targets` 为准）：情绪闸门通过后，
在题材内找「一字二板兔子」，买同题材其他二板（乌龟）；无组合则当日不开仓，
不硬凑、不退回「全部二板」旧口径。完整算法与流程图见
[`悟空真经-全流程图与算法逻辑核对.md`](悟空真经-全流程图与算法逻辑核对.md)。

## 五、数据真实性

- 只读真实落盘：`data/panel_raw.parquet`（真实日线）+ `data/live/<day>/pools/pools.jsonl`（东财涨停池原样留档）。
- 不合成、不插值、不补数；日线未覆盖时状态是 `pending_daily`，绝不拿盘中价冒充收盘价。
- 停牌静默跨日，到右端仍连板记 `censored`，不算盈也不亏。
- 今日无委托就显示「今日不开仓」，不回退展示历史日的单。

## 六、风险声明

本项目只做数据整理与规则化模拟，**不构成投资建议**。A 股 T+1、涨跌停、流动性风险会放大回测与实盘的差距；
历史表现不代表未来收益。任何买入决策与盈亏由使用者自行承担。

## 七、仓库管理

- 本目录已是**独立 git 仓库**：`E:\workspace\量化\.git`，origin =
  `https://github.com/dusky376/analyze.git`（分支 `main`）。
- 已本地提交：`dc4d3b7 feat: 悟空真经 E5:B1 单策略终端（提炼精简版）`。
- 运行期数据（parquet / jsonl / 账本 / 缓存）由 `.gitignore` 排除，只入库源码、文档、配置与测试。
- ⚠️ 2026-11-23 推送未成功：本环境到 `github.com:443` 的 TCP 连接被重置（无代理、非 git 配置问题）。
  网络恢复后执行：`cd /d E:\workspace\量化 && git push -u origin main`。

## 八、精简记录

- 2026-11-23：只保留悟空真经前端/后端/算法 + 最小运行底座；删除 E1–E4、威科夫、LLM、情绪周期、
  冰点二板、题材雷达、决策总线、周度复盘等模块与页面。删除前全量备份：
  `E:\workspace\_archive\量化-精简前-20261123-185555.zip`。
