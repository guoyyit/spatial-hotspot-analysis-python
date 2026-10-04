# 空间事件热点识别与聚类分析服务

带地理坐标与时间戳的事件通过 HTTP 写入本机 SQLite，服务基于空间邻域权重与
显著性检验识别热点，并支持参数化密度聚类。支持乱序/重复/迟到事件、增量与全量
结果一致、快照一致读、崩溃恢复与有界内存占用。

## 运行

```bash
uv sync
# 可选环境变量
export SPATIAL_DB_PATH=./spatial_hotspot.db      # 数据库路径
export SPATIAL_ORIGIN_LON=116.40                 # 投影原点经度
export SPATIAL_ORIGIN_LAT=39.90                  # 投影原点纬度
export SPATIAL_BUCKET_WIDTH_S=3600               # 时间桶宽度(秒)
uv run uvicorn main:app --host 127.0.0.1 --port 8000
# 交互式文档: http://127.0.0.1:8000/docs
```

事件输入与存储全部在本机完成，不依赖外部服务。

## 接口

### `POST /events` 写入事件（支持批处理、乱序、重复、迟到）

```json
{
  "events": [
    {"event_id": "e1", "lon": 116.404, "lat": 39.915, "ts": 1700000000.0}
  ]
}
```

返回 `{"accepted": 1, "duplicates": 0, "version": 1}`。重复 `event_id` 只计一次。

### `GET /hotspots` Getis-Ord Gi* 热点识别

查询参数：`start`、`end`（事件时间窗口，半开 `[start,end)`）、`alpha`（默认 0.05）、
`bandwidth_m`（默认 1000）、`grid_size_m`（默认 250）、
`correction`（`fdr_bh` 默认 / `bonferroni` / `none`）、
`kernel`（`binary` 默认 / `gaussian`）、`refresh`（`true` 强制重算）。

每个网格单元返回计数、`z_score`、`p_value_raw`、`p_value_adj`、`significant`、
`kind`（`hotspot`/`coldspot`/`nonsignificant`）；顶层 `status` 标注结果生命周期。

### `GET /clusters` DBSCAN 密度聚类与热点区域提取

查询参数：`start`、`end`、`eps_m`（默认 500）、`min_samples`（默认 5）、`refresh`。
返回每个簇的 `size`、质心与外接矩形，以及 `noise_count`。

### 管理与状态

- `POST /admin/rebuild?grid_size_m=250`：全量重建派生汇总并校验与增量结果一致。
- `POST /admin/invalidate`：清空热点/聚类结果缓存。
- `GET /status`：当前 version、事件总数、事件时间范围、数据库路径。

## 结果生命周期（`status`）

- `current`：当前参数下、基于最新已提交版本的结果；
- `stale_data`：参数仍为当前参数，但之后有新事件提交（结果仍可查询，携带其 version）；
- `stale_params`：之后使用过另一组参数，该旧参数结果标记为失效。

## 测试

```bash
uv run pytest
```

覆盖：乱序/重复/迟到去重、半开窗口边界、增量与全量一致性、Bonferroni 与 BH-FDR
修正、Gi* 热点检出、DBSCAN 确定性与共点多重性、并发读写单快照自洽、崩溃/重启恢复、
半写入回滚、半重建清理、有界窗口扫描，以及 HTTP 端到端与参数校验。

统计口径、公式与参数语义见 [`docs/SEMANTICS.md`](docs/SEMANTICS.md)。
