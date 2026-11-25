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
| 卖点 | **两条路径（2026-11-25 裁定）**：① 买入日收盘仍炸板（未回封）→ 次日 10:00 前卖出；② 买入日收盘封住 → 首个未涨停日（断板）收盘前卖出。两条路径的盘中触发卖点均为「快速下跌 ≥2% / 冲高回落 ≥3%」信号，**具体参数仍在研究中，未做样本外验证**；未触发则按各路径的时间边界兜底。历史回放口径仍是「断板当日收盘」，与实盘账本不是同一卖法 |
| 成本 | **万 0.84 免五**（佣金 0.0084%/边、无最低 5 元、印花 0.05%、过户 0.001%、滑点 15bp/边），单一口径（用户 2026-11-23 裁定）；历史已结算记录不重算 |
| 边界 | 只写本地模拟账本，**永不向任何券商/账户发送交易指令** |

## 二、目录

> **2026-11-25 已做数据与代码分离**：代码在 `live_terminal/`（约 2.2 MB），
> 数据在项目根的 `data/`（约 119 MB），可整体搬走。详见
> [`架构说明-数据代码分离与瘦身-20261125.md`](架构说明-数据代码分离与瘦身-20261125.md)。

```
量化/
├─ start.bat / stop.bat          启动 / 停止（ASCII，CRLF）
├─ 踩坑与复利.md                 项目级记录
├─ data/                         【纯数据，可整体搬走】面板 / 留档 / 账本 / 题材
└─ live_terminal/                【纯代码，约 2.2 MB】
   ├─ app.py                     Flask 入口（10 个路由，前端用 5 个）
   ├─ backend/                   悟空真经算法 + 最小运行底座
   │   ├─ live_sim.py            B1 策略本体（闸门→选股→挂单→成交→结算）
   │   ├─ paths.py               数据路径唯一事实源（支持 WUKONG_DATA_HOME）
   │   ├─ archive.py             留档读写抽象（明文/gzip/合并归档透明）
   │   ├─ compact_archive.py     历史留档压缩工具（cli）
   │   ├─ queue_match.py         Q037 排板撮合 + 今日队列观察表
   │   ├─ fill_verify.py         逐轮封单消耗成交核验
   │   ├─ minute_flow.py         腾讯分钟成交额（封单消耗测算）
   │   ├─ intraday_signal.py     分时首封/开板次数交叉核对
   │   ├─ guitu_roles.py         兔子/乌龟/盘子偏大 角色标注（不过滤）
   │   ├─ guitu_backtest.py      二板排板历史回放
   │   ├─ guitu_cases.py         龟兔案例/命题检验（后端只读，前端已隐藏）
   │   ├─ feeds.py / recorder.py 真实行情 / 涨停池采集与留档
   │   ├─ feature_engine.py / costs.py / dates.py
   │   ├─ leader_ladder.py / leader_casebook.py  案例证据链支撑
   │   └─ theme_pool.py / limit_counts.py / trading_rules.py / market_clock.py
   ├─ web/                       单页前端 3 区块（今日/账本/回放）+ favicon
   ├─ configs/                   题材轴（theme_axis.yaml）—— 配置跟代码走
   └─ tests/                     悟空真经契约与算法测试（424 passed）
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
| `GET /api/health` | 面板、留档、采集器、交易时钟状态 + `archive_check` 数据完整性自检（缺留档显式报警） |
| `GET /api/live-sim` | 今日二板委托、持仓、结算、万0.84免五单一成本 |
| `GET /api/guitu` | 历史回放 + 今日实时排板队列 |
| `GET /api/guitu-cases` | 龟兔案例库 / 今日快照 / 命题检验（只读） |
| `GET /api/recorder` | 采集器状态 |
| `GET /api/exit-alternatives` | 卖法对照（只读研究）：现行卖法 vs 固定止盈止损模拟 + 当时情绪/高标/环境快照 |
| `POST /api/refresh` | 清缓存并重新同步 |
| `GET /api/stream` | SSE 每 30s 推送今日开仓简报（前端可降级为轮询） |

**实际选股口径**（以 `live_sim.collect_guitu_targets` 为准）：情绪闸门通过后，
在题材内找「一字二板兔子」，买同题材其他二板（乌龟）；无组合则当日不开仓，
不硬凑、不退回「全部二板」旧口径。完整算法与流程图见
[`悟空真经-流程图与算法核对.md`](悟空真经-流程图与算法核对.md)、
[`亏损复盘与未买入二板因子研究.md`](亏损复盘与未买入二板因子研究.md)、
[`卖点时机实证对比研究.md`](卖点时机实证对比研究.md)、
[`外部项目与论文复用评估.md`](外部项目与论文复用评估.md)。

## 五、数据真实性

- 只读真实落盘：`data/panel_raw.parquet`（真实日线）+ `data/live/<day>/pools/`（东财涨停池原样留档）。
- 历史日的留档可能是压缩形态（`pools.jsonl.gz` / `quotes/frames.parquet`），
  由 `backend/archive.py` 透明读取，数据内容一字未改。
- 不合成、不插值、不补数；日线未覆盖时状态是 `pending_daily`，绝不拿盘中价冒充收盘价。
- 停牌静默跨日，到右端仍连板记 `censored`，不算盈也不亏。
- 今日无委托就显示「今日不开仓」，不回退展示历史日的单。

## 五之一、数据位置可配置

数据目录默认在 `<项目根>/data`，可用环境变量整体搬走（代码零改动）：

```powershell
$env:WUKONG_DATA_HOME = "D:\quant-data"
```

`GET /api/health` 的 `paths` 字段会显示实际解析到的数据根与 `separated` 标记。
历史留档可用一条命令压缩（详见架构说明文档）：

```powershell
cd live_terminal
python -m backend.compact_archive --all
```

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
- 2026-11-25：**数据与代码分离 + 瘦身**（204.28 MB → 123.51 MB，−39.5%）。
  - 数据从 `live_terminal/data` 迁到项目根 `data/`，代码 2.21 MB / 数据 118.76 MB。
  - `backend/paths.py` 成为唯一路径事实源，17 处硬编码路径全部改掉，
    支持 `WUKONG_DATA_HOME` 整体搬移数据。
  - 新增 `backend/archive.py`（留档读写抽象）+ `backend/compact_archive.py`（压缩工具）：
    历史 `pools.jsonl` → gzip（8%）、`quotes`/`auction` 逐帧 → 单日合并（36%/19%），
    6 个交易日实测 91.85 MB → 24.66 MB。
  - 删除 8 个已完成的一次性迁移脚本、pytest 临时目录与全部 `__pycache__`。
  - 修复真实架构缺陷：`theme_pool` 原用 `data_dir().parent / "configs"`，
    数据一搬走就找不到题材轴 → 改为 `paths.configs_dir()`。
  - 测试 405 → **424 passed, 1 skipped**（新增 19 个路径/归档契约测试）。
  - 详见 [`架构说明-数据代码分离与瘦身-20261125.md`](架构说明-数据代码分离与瘦身-20261125.md)。
