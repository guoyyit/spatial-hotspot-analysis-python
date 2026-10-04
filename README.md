# 空间事件热点识别与聚类分析服务

本服务接收带地理坐标与时间戳的事件流，提供基于空间邻域权重与显著性检验的
热点识别、参数化密度聚类与热点区域提取。全部计算与存储在本机完成
（SQLite + 内存索引），不依赖任何外部服务。

## 运行与测试

```bash
uv sync                 # 安装依赖
uv run pytest           # 运行全部自动化测试
uv run uvicorn main:app # 启动服务（默认 SQLite 文件 data/events.db）
```

数据库路径可用环境变量 `SHP_DB_PATH` 覆盖。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/events` | 批量写入事件（幂等去重） |
| GET | `/stats/summary` | 总量、时间范围、活跃网格数、版本号 |
| GET | `/hotspots` | 热点显著性分析（可配窗口/α/带宽/修正方式） |
| POST | `/analysis/rebuild` | 从持久化事件全量重建分析状态 |
| POST | `/clusters/compute` | 按参数计算密度聚类 |
| GET | `/clusters` | 查询最近一次聚类结果（含失效标记） |
| GET | `/health` | 健康检查 |

## 事件写入语义

- 事件字段：`event_id`（字符串，全局唯一）、`lat`/`lon`（度）、
  `ts`（Unix 秒，浮点）、`attrs`（任意 JSON）。
- **去重**：以 `event_id` 为幂等键，重复到达（包括同一批次内的重复）
  只计一次；响应中分别返回 `accepted` 与 `duplicates` 数量。
- **乱序与迟到**：事件允许任意顺序到达。统计只依赖事件集合本身，
  与写入顺序无关（测试 `test_out_of_order_write_matches_ordered_write`
  验证两种顺序产生完全相同的网格状态）。
- **原子性**：每个写入批次是一个 SQLite 事务，崩溃后要么整批生效、
  要么整批不存在，不会留下半更新状态。

## 统计口径

### 空间网格

事件按 `cell_size_deg`（默认 0.01°）划入规则网格，统计在网格计数上进行。
网格是稀疏结构，只有非空单元占内存。

### Getis-Ord Gi* 热点统计量

对每个非空单元 *i* 计算 Gi* z 分数（Getis & Ord, 1992）：

```
z_i = (Σ_j w_ij·x_j − x̄·Σ_j w_ij) / (S·√((n·Σ_j w_ij² − (Σ_j w_ij)²)/(n−1)))
```

- 权重 `w_ij` 为**二值空间邻域权重**：两单元中心切比雪夫距离
  ≤ `bandwidth_deg` 时为 1（含自身 j=i），否则为 0。
- `x̄`、`S` 为全部非空单元计数的均值与总体标准差；`n` 为非空单元数。
- 所有单元计数相等（S=0）或只有一个单元时，z 定义为 0（无空间分异）。
- p 值为标准正态双侧检验：`p = erfc(|z|/√2)`。

### 多重比较修正

`correction` 参数控制对同一结果集内全部单元的 p 值修正：

| 取值 | 方法 | 控制目标 |
| --- | --- | --- |
| `none` | 不修正，p_adjusted = p | — |
| `bonferroni` | p_adjusted = min(1, p·m) | 族系错误率 FWER |
| `fdr_bh`（默认） | Benjamini–Hochberg 步进法 | 错误发现率 FDR |

结果中每个单元都带有 `correction` 字段，**明确标注本次结论所用的修正
方式**；`significant` 与 `label`（`hotspot`/`coldspot`/`not_significant`）
均由**修正后**的 `p_adjusted` 与 `alpha` 判定（z>0 为热点，z<0 为冷点）。

### 可配置参数

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `alpha` | 0.05 | 显著性水平（作用于修正后 p 值） |
| `bandwidth_deg` | 0.05 | 空间邻域带宽（度） |
| `correction` | `fdr_bh` | 多重比较修正方式 |
| `cell_size_deg` | 0.01 | 网格单元边长（度），服务级配置 |
| `bucket_seconds` | 3600 | 时间桶粒度（秒），服务级配置 |

## 时间窗口语义

- 窗口为**半开区间 `[start, end)`**：`start` 含、`end` 不含。
- `/hotspots` 的窗口查询在**时间桶粒度**上聚合：凡是与 `[start, end)`
  有交叠的时间桶整体计入。即窗口边界按桶对齐取并集，属于有意的近似，
  使历史窗口可以常驻有界内存。不传窗口时返回全量（增量维护的）视图。
- `/clusters/compute` 的窗口按事件时间戳**精确过滤**（`ts >= start AND
  ts < end`），直接读取持久化存储。
- **迟到事件的追溯更新**：迟到事件按其自身 `ts` 落入对应历史时间桶，
  之后的任何窗口查询自动包含它——历史窗口的结论会被追溯性更新
  （测试 `test_window_query_and_late_event_retroactive_update`）。
  已返回给客户端的历史响应不会被改写，版本号（`version`）随每次
  写入单调递增，调用方可据此判断结论是否已变化。

## 增量、全量与快照一致性

- 写入路径同时更新持久化存储与内存增量状态（全局网格 + 时间桶索引），
  二者在同一临界区内推进。
- `POST /analysis/rebuild` 从 SQLite 全量重算：先在写锁内钉住一个 WAL
  读快照，锁外完成扫描，重建期间并发写入的事件进入增量日志并在换装前
  回放，最后**原子换装**。增量推进的结果与从零全量重算**逐单元一致**
  （测试 `test_incremental_equals_full_rebuild`、
  `test_rebuild_concurrent_with_writes_stays_consistent`）。
- 每次查询先获取一个写时拷贝的**一致快照**（含版本号），计算在锁外
  进行；并发写入下读到的永远是某一个完整版本，不会读到中间态。

## 聚类与失效语义

- 聚类算法为 DBSCAN（欧氏距离，单位为度），参数 `eps_deg`（邻域半径）
  与 `min_samples`（核心点最小邻居数，含自身）。
- **确定性**：点按 `event_id` 排序后输入，邻居按固定顺序扩展，聚类 id
  按（规模降序、质心坐标）重编号——同一参数与同一数据下逐次结果完全
  一致（测试 `test_cluster_ids_stable_across_identical_runs`）。
- **失效标记**：聚类结果记录计算时使用的参数。`GET /clusters` 若以不同
  参数查询，返回的最近结果带 `stale: true`，表示该结果对应旧参数，
  需重新 `POST /clusters/compute`。
- 单次聚类最多读取 `cluster_max_points`（默认 100 000）个点，
  超出部分按 `event_id` 顺序截断，保证内存有界。

## 崩溃恢复

- 事件存储为 SQLite（WAL 日志），已提交批次崩溃后完好；未提交批次
  整体回滚，不留半更新状态。
- 内存分析状态是持久化事件的**派生物**：重启时从已提交事件全量重建，
  已提交的统计结果立即可查询（测试 `test_restart_recovers_committed_state`、
  `test_results_queryable_after_restart`）。
- 重建过程通过 `rebuild_in_progress` 元数据标记；若崩溃留下未完成的
  重建标记，启动时自动清除——重建在内存中构建、原子换装，磁盘上从不
  存在半成品分析状态。

## 有界资源

- 内存中只保存**聚合量**（网格计数、时间桶计数），原始事件全部落盘。
- 时间桶索引是 LRU 有界缓存（`max_buckets_in_memory`，默认 256）：
  高频访问区域与近期窗口常驻，冷桶被逐出后可从 SQLite 重建；
  内存占用不随事件总量增长（测试 `test_bucket_cache_is_bounded_but_global_stats_exact`）。
- 网格为稀疏结构，内存随**活跃区域数**而非事件数增长。

## 项目结构

```
app/
  config.py   服务配置（统计与资源参数）
  models.py   API 请求/响应模型
  grid.py     稀疏空间网格 + LRU 时间桶索引
  stats.py    Gi* 统计量与多重比较修正
  store.py    SQLite 事件存储（去重、WAL、读快照）
  cluster.py  确定性 DBSCAN
  engine.py   增量状态、快照隔离、全量重建、崩溃恢复
  main.py     FastAPI 路由
tests/        乱序/重复/迟到/一致性/并发/恢复等自动化测试
```

