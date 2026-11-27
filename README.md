# 悟空真经 · 二板排板终端

> 单策略实盘验证终端（**E5:B1 悟空真经·二板排板**）：集情绪闸门、冰点降仓、龟兔选股、买点质量分级、Q037 排板撮合、两路径自适应卖点、因果信息素反馈与三页签交互式看板于一体。

## 一、策略口径（唯一）

| 项目 | 规则 |
|---|---|
| 市场冰点与普跌细分 | 四维分位分级（`down_share` / `limit_down_n` / `dt_ratio` / `broken_hi_n`）+ **自适应普跌细分引擎**（外盘冲击型 / 情绪退潮核按钮型 / 抱团虹吸分化型 / 缩量失血阴跌型）：不写死静态阈值，全部按近 250 日滚动分位秩与次日修复率 + 实盘因果结算盈亏动态演化仓位系数（`[0.15, 0.75]`），结合外盘日韩美股/A50 情绪风偏 |
| 个股情绪与高标梯队 | 昨日高标（≥2板）今日均涨 ≤ -3%、跌停占比 ≥ 40%、或昨日涨停溢价 ≤ -1.5% 时拦截开仓；新增 **≥3板高标梯队动态脉冲**（不对称：高标核按钮跌停按近60日实证动态控仓 `×0.70~0.85`，高标晋级仅在近60日二板正传导胜率 ≥50% 时给同题材小幅加分，避免盲目追高），并通过 `sizing` 统一前后端生效仓位口径 |
| 选股与四层题材识别 | **题材内有一字二板（兔子）→ 买同题材其他二板（乌龟）；无组合则当日不开仓**；`theme_match.py` 升级为四层自适应识别（YAML v4 13轴 → 高精度语义词根 → `themes.parquet` 同票共现自学习归轴 → 因果历史个股记忆 + 新概念前缀聚类），融合美股映射、国内 7×24 快讯与主线龙头热度反哺，剔除雷区与烂板，每题材前 2 只、单日上限 5 只 |
| 买点与仓位 | 二板封板瞬间以涨停价挂限价单排队；按滚动可用净值 × 10% × 综合风偏系数 × 严格因果信息素系数（`[0.30, 1.25]`）分配，总仓上限 80%；附买点质量分级（A/B/C/REJECT）与临封扫板观察雷达 |
| 成交判定 | 按东财 Q037 + 逐轮封单消耗核验（`fill_verify.py`）：开板期优先按 1 分钟真实成交额逐轮扣减队列；全天未开板期同步核对封板期 1 分钟累计真实成交额（解决“边吃边补单”误判），吃穿前方封单队列才判成交，成交价 = 涨停价 |
| 卖点（两路径自适应） | ① **买入日收盘仍炸板（未回封）** → 次日 `10:00` 前卖出（含竞价低开抢跑）；② **买入日收盘封住** → 首个断板日收盘前卖出（含次日不及预期低开反抽无力离场、09:30–09:35 洗盘滤波、冲高分批锁利建议与 `14:40` 尾盘弱势防核按钮；**不因兔子断板而强杀强势换手乌龟**）。未到时间边界（10:00 / 14:55）不提前兜底强平 |
| 反馈自进化与踏空反思 | `evolution.py` 三向闭环：① 已操作单按真实已结算盈亏与未成交次日遗憾（侦察蚁）因果更新信息素（`ρ=10%` 挥发、核按钮 `2×` 重罚）；② **漏网强势股反事实反思**：扫描全市场未命中但走出大行情的二板，归因漏网原因并经正负样本对撞检验候选因子，同时把主线龙头热度因果反哺至催化分与仓位微调；③ **B2 纯换手二板影子挑战者**：针对无同题材一字兔的纯换手二板，按「关联产业链有兔 / 高标同题材带动 / 一字首板带动 / 独立换手」独立跑影子结算与信息素学习，达标毕业门禁（样本≥10、胜率≥55%、净均≥+3%、核按钮≤10%、τ≥1.10）才解锁 0.3× 卫星试错仓 |
| 成本口径 | **万 0.84 免五**（佣金 0.0084%/边·无最低5元·印花 0.05%·过户 0.001%·滑点 15bp/边，往返 `0.3688%`） |
| 安全边界 | 纯本地模拟账本，**永不向任何券商/账户发送交易指令** |

## 二、目录结构（数据与代码分离）

> 代码位于 `live_terminal/`，运行期数据统一由 `backend/paths.py` 解析至 `<项目根>/data/`（支持 `WUKONG_DATA_HOME` 环境变量整体迁移）。详见 [架构说明-数据代码分离与瘦身-20261125.md](架构说明-数据代码分离与瘦身-20261125.md)。

```text
量化/
├─ start.bat / stop.bat          一键启动 / 停止（端口 8765）
├─ README.md                     项目总说明
├─ 踩坑与复利.md                 实盘/开发复盘记录
├─ data/                         【纯数据，可整体迁移】面板 / 留档 / 账本 / 题材
└─ live_terminal/                【纯代码】
   ├─ app.py                     Flask 入口（14 个路由 + SSE 推送 + 后台预热）
   ├─ backend/
   │   ├─ live_sim.py            B1 策略本体（闸门→选股→因果分仓→撮合→自适应结算）
   │   ├─ sell_rules.py          两路径自适应卖点引擎（板块幅度/VWAP/冰点动态阈值）
   │   ├─ buy_signals.py         买点质量分级（A/B/C/REJECT）与临封扫板观察雷达
   │   ├─ evolution.py           蚁群信息素非对称奖惩进化与侦察蚁遗憾引擎
   │   ├─ fill_verify.py         逐轮封单消耗成交核验
   │   ├─ queue_match.py         Q037 排板撮合模型
   │   ├─ ice_gate.py / ice_samples.py  市场冰点四维分级与漂移监控
   │   ├─ overseas.py / news_feed.py    外盘日韩美股A50情绪风偏、美股龙头映射与7×24政策快讯/雷区引擎
   │   ├─ theme_pool.py / theme_match.py 同花顺题材池抓取与多标签板块归并
   │   ├─ feeds.py / minute_flow.py / recorder.py  多源行情（腾讯+新浪热备/东财分时）与采集留档
   │   ├─ charts.py              个股日K线（含实时bar+买卖点标记）与1分钟分时
   │   ├─ guitu_roles.py / guitu_backtest.py / guitu_cases.py / leader_ladder.py
   │   ├─ exit_alternatives.py   卖法对照（现行卖法 vs 固定止盈止损）
   │   ├─ paths.py / archive.py / compact_archive.py / data_health.py
   │   └─ costs.py / feature_engine.py / dates.py / limit_counts.py / trading_rules.py / market_clock.py / intraday_signal.py / json_safe.py
   ├─ web/                       Apple 风格暗色玻璃三页签终端（index.html / app.js / style.css / favicon.svg）
   ├─ configs/                   题材轴配置（theme_axis.yaml）
   └─ tests/                     全量契约与回归测试（439 passed）
```

## 三、启动与常用命令

```bat
cd /d E:\workspace\量化
start.bat
```

打开 http://127.0.0.1:8765 。停止：`stop.bat`。

```powershell
cd live_terminal
python app.py                     # 启动服务（8765）
python -m backend.guitu_backtest  # 重建历史回放 data/guitu_sim.json
python -m backend.live_sim        # 手动同步实时模拟账本
python -m backend.compact_archive --all  # 压缩历史留档
pytest -q                         # 运行全量测试
```

依赖：Python 3.11+，`flask polars pandas pyarrow requests numpy`（可用 `WUKONG_PYTHON` 指定解释器）。

## 四、API 接口清单

| 接口 | 说明 |
|---|---|
| `GET /` | 三页签单页终端（今日作战台 / 持仓与账本 / 进化与回放） |
| `GET /favicon.svg` | 终端图标 |
| `GET /api/version` | 进程 build / 磁盘 build / stale |
| `GET /api/health` | 合并日线面板（raw+live）、留档自检、题材池与采集器健康（15s 短缓存） |
| `GET /api/live-sim` | 实时模拟账本、账户资产、冰点/情绪/外盘风偏、今日委托质量评分与临封雷达 |
| `GET /api/macro-intel` | 全球日韩美股/A50 指数、美股板块映射与国内 7×24 政策快讯/持仓雷区 |
| `GET /api/evolution` | 蚁群信息素路径权重、侦察蚁遗憾反馈日志与严格因果影子对照 |
| `GET /api/chart?code=XXXXXX` | 单股快查日K线（含当日实时bar与买卖点标记）+ 1分钟分时 |
| `GET /api/guitu` | 二板排板历史回放 + 今日实时排板队列 |
| `GET /api/guitu-cases` | 龟兔案例库与命题检验（只读） |
| `GET /api/exit-alternatives` | 卖法对照（现行 vs 固定止损 -7% / 止盈 +15% + 环境分组） |
| `GET /api/ice-samples` | 冰点样本库与漂移监控 |
| `GET /api/recorder` | 后台实时采集器状态 |
| `POST /api/refresh` | 清空全层级缓存并重新同步 |
| `GET /api/stream` | SSE 实时状态心跳推送 |

## 五、数据真实性与风控声明

- 只读真实落盘与公开行情（`panel_raw.parquet` + `panel_live.parquet` + `data/live/<day>/`），**绝不合成、不插值、不补数**。
- 今日无龟兔组合则如实显示「今日不开仓」，绝不回退展示历史日委托或旧配对。
- 本项目仅作量化研究与规则化模拟，**不构成投资建议**，不向任何外部券商发单。

## 六、近期迭代与优化记录

- **2026-11-25（升级与全面审计优化）**：
  - 修复生产崩溃：修正 `evolution.py` 与 `charts.py` 将 `sim_state.json` 字典形态 `orders`/`positions` 当列表遍历导致的报错。
  - 打通策略闭环：`live_sim.py` 正式接入 `sell_rules.decide_adaptive`、盘中兜底时间门控（A<10:00 / B<14:55 不提前兜底强平）、今日涨停已落盘保护、`evolution.causal_multipliers_before` 严格因果仓位反馈与滚动净值分配，以及 `buy_signals` 临封雷达。
  - 性能优化：`emotion_gate` 改为 Polars 35 交易日窗口裁剪（耗时 ~4s → ~0.15s，提升 >20×）；`_payload()` 合并重复全市场 `quotes` 读取为单次按需过滤；`_live_price` 改用单股快查；`/api/health` 合并 `panel_live.parquet` 日期并加 15s 短缓存；统一板块涨跌幅逻辑到 `feature_engine.limit_pct`；清理冗余 `.bak` 备份文件。
  - 前端重构：全量对齐 3 页签所有接口字段契约，并升级为 Apple 风格暗色玻璃与弹簧微动效界面（滑动胶囊、滚动渐显、卡片跟随高光、KPI 计数动画）。
  - 历史回放 `data/guitu_sim.json` 更新至 `2026-11-25`（5,097 样本）；测试增至 **439 passed, 1 skipped**。
- **2026-11-25（早期）**：数据与代码分离 + 历史留档压缩瘦身（详见 [架构说明-数据代码分离与瘦身-20261125.md](架构说明-数据代码分离与瘦身-20261125.md)）。
- **2026-11-23**：提炼单策略精简版，移除非悟空策略模块。
