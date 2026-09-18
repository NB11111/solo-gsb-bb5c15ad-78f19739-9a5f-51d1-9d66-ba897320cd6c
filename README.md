# 仪器校准漂移补偿 API

基于 **Python + FastAPI + SQLite** 的仪器校准漂移补偿服务。

客户端在某一时点提交**参考值**与同一时点的**至少 3 次读数**，服务：

1. 以整组读数的**中位数**为基准，将与中位数之差的绝对值**超过请求容差**的读数标记为异常；
2. 剔除异常读数后，以 **校准偏差 = 参考值 − 其余读数中位数** 作为该时点的偏差；
3. 查询时在**相邻两个观测时点之间对偏差做线性插值**，将待校正读数加上偏差后返回，并给出所用区间与区间端点的异常明细；
4. 每次新增观测后**重算全部相邻区间**，观测与原始读数始终保留（不覆盖、不删除）；
5. 查询时点落在观测覆盖范围（最早 ~ 最晚观测时点）之外时**拒绝查询**（HTTP 400）。

---

## 一条命令初始化并启动

```bash
docker compose up -d --build
```

该命令自动构建镜像、初始化 SQLite 数据库（首次启动自动建表）、并以后台方式启动服务。

- **API 地址与端口**：`http://localhost:8000`
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc 文档：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

停止服务：`docker compose down`（数据保留在命名卷 `calibration-data` 中）。

> 主机端口可在 `docker-compose.yml` 的 `ports` 中修改（默认 `8000:8000`）。

### 不使用 Docker 的本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
CALIBRATION_DB=./data/calibration.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 数据模型（SQLite）

| 表 | 内容 |
|---|---|
| `observations` | 时点（唯一）、参考值、容差、原始读数中位数 `median_raw`、校准偏差 `bias` |
| `readings` | 每次读数及其 `is_anomaly` 异常标记（**原始数据完整保留**） |
| `intervals` | 相邻观测时点区间：端点观测 ID、时点与偏差（新增观测后整体重算） |

数据库文件位于容器内 `/data/calibration.db`，通过 Docker 命名卷 `calibration-data` 持久化。

---

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/observations` | 提交一条校准观测（至少 3 次读数），自动判异常、算偏差、重算区间 |
| `GET` | `/api/v1/observations` | 列出全部观测（含每次读数与异常标记） |
| `GET` | `/api/v1/observations/{id}` | 查询单条观测 |
| `GET` | `/api/v1/intervals` | 列出当前全部相邻插值区间 |
| `GET` | `/api/v1/calibrate?timestamp=&reading=` | 查询时点的偏差并校正读数；范围外返回 400 |
| `GET` | `/health` | 健康检查 |

### 1. 提交观测

```bash
curl -X POST http://localhost:8000/api/v1/observations \
  -H 'Content-Type: application/json' \
  -d '{
        "timestamp": 0,
        "reference_value": 12.0,
        "tolerance": 5.0,
        "readings": [9.0, 10.0, 11.0, 50.0]
      }'
```

请求字段：

- `timestamp`：观测时点，Unix 时间戳（秒，允许浮点），同一时点唯一，重复提交返回 `409`；
- `reference_value`：参考（真值）；
- `tolerance`：异常容差，必须 `> 0`，读数满足 `|读数 − 整组中位数| > tolerance` 判为异常；
- `readings`：同一时点至少 3 次读数。

响应示例（组中位数为 `10.5`，`50.0` 偏离 `39.5 > 5` 判为异常；剔除后中位数为 `10`，偏差 `12 − 10 = 2`）：

```json
{
  "id": 1,
  "timestamp": 0.0,
  "reference_value": 12.0,
  "tolerance": 5.0,
  "median_raw": 10.5,
  "bias": 2.0,
  "readings": [
    {"value": 9.0,  "is_anomaly": false},
    {"value": 10.0, "is_anomaly": false},
    {"value": 11.0, "is_anomaly": false},
    {"value": 50.0, "is_anomaly": true}
  ]
}
```

### 2. 再提交一个后续时点

```bash
curl -X POST http://localhost:8000/api/v1/observations \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": 10, "reference_value": 24.0, "tolerance": 5.0, "readings": [19.0, 20.0, 21.0]}'
# bias = 24 - 20 = 4
```

### 3. 线性插值 + 漂移校正

```bash
curl "http://localhost:8000/api/v1/calibrate?timestamp=5&reading=15"
```

`t = 5` 位于相邻时点 `0`（偏差 2）与 `10`（偏差 4）之间，插值偏差为 `3.0`，
校正结果 `15 + 3 = 18.0`。响应包含所用区间端点与异常明细：

```json
{
  "query_timestamp": 5.0,
  "input_reading": 15.0,
  "bias": 3.0,
  "corrected_value": 18.0,
  "interval": {
    "method": "linear",
    "previous_observation_id": 1,
    "next_observation_id": 2,
    "start_timestamp": 0.0,
    "end_timestamp": 10.0,
    "start_bias": 2.0,
    "end_bias": 4.0
  },
  "anomalies": [ {"id": 1, "...": "区间左端点观测（含全部读数与标记）"},
                 {"id": 2, "...": "区间右端点观测（含全部读数与标记）"} ]
}
```

说明：

- `reading` 可省略：此时只返回插值 `bias`，`input_reading` / `corrected_value` 为 `null`；
- `timestamp` 恰好等于某条观测时点时，`interval.method` 为 `"exact"`，直接采用该点偏差，`anomalies` 仅含该观测；
- 其余情况 `method` 为 `"linear"`，偏差按时间比例线性插值。

### 4. 范围外拒绝

```bash
curl -i "http://localhost:8000/api/v1/calibrate?timestamp=99"
# HTTP 400
# {"detail": "查询时点 99.0 超出观测覆盖范围 [0.0, 10.0]，范围外查询被拒绝",
#  "coverage": {"start_timestamp": 0.0, "end_timestamp": 10.0}}
```

不做外推（extrapolation）：只接受 `[最早观测时点, 最晚观测时点]` 内的查询。

### 5. 区间列表

```bash
curl http://localhost:8000/api/v1/intervals
```

返回按时间排序的相邻观测区间；任意时点新增观测后，区间集合自动重算（如在两点之间插入一条观测，原来的一个区间变为两个）。

---

## 计算规则

- 整组中位数：对客户端提交的全部读数取中位数 `median_raw`；
- 异常判定：`|reading − median_raw| > tolerance`（严格大于；等于容差不判异常）；
- 有效读数中位数：剔除异常读数后重新取中位数；
- 校准偏差：`bias = reference_value − median(有效读数)`；
- 若某次提交的全部读数都被判为异常（偶数个读数且中位数不是任何实际读数时理论上可能），返回 `422`，不落库；
- 区间插值：对查询时点 `t ∈ [t₁, t₂]`，
  `bias(t) = bias₁ + (t − t₁) / (t₂ − t₁) × (bias₂ − bias₁)`；
- 校正：`corrected_value = reading + bias(t)`。

## 测试

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```
