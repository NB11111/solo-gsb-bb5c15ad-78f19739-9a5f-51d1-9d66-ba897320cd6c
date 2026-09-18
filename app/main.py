import os
import sqlite3
from contextlib import asynccontextmanager
from statistics import median
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from .schemas import (
    CalibrateResponse,
    IntervalDetail,
    IntervalOut,
    ObservationCreate,
    ObservationOut,
    ReadingOut,
)

DB_PATH = os.environ.get("CALIBRATION_DB", "/data/calibration.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL UNIQUE,
    reference_value REAL    NOT NULL,
    tolerance       REAL    NOT NULL,
    median_raw      REAL    NOT NULL,
    bias            REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    value          REAL    NOT NULL,
    is_anomaly     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS intervals (
    start_observation_id INTEGER PRIMARY KEY
                                 REFERENCES observations(id) ON DELETE CASCADE,
    end_observation_id   INTEGER NOT NULL
                                 REFERENCES observations(id) ON DELETE CASCADE,
    start_timestamp      REAL    NOT NULL,
    end_timestamp        REAL    NOT NULL,
    start_bias           REAL    NOT NULL,
    end_bias             REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observations_ts ON observations(timestamp);
CREATE INDEX IF NOT EXISTS idx_readings_obs ON readings(observation_id);
"""


def connect() -> sqlite3.Connection:
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db() -> sqlite3.Connection:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="仪器校准漂移补偿 API",
    version="1.0.0",
    description=(
        "提交参考值与同点多次读数，服务剔除异常后计算校准偏差，"
        "在相邻观测时点间对偏差做线性插值，并对待校正读数进行漂移补偿。"
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 数据装配辅助
# ---------------------------------------------------------------------------


def load_observation(conn: sqlite3.Connection, obs_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()


def observation_to_out(row: sqlite3.Row, conn: sqlite3.Connection) -> ObservationOut:
    reading_rows = conn.execute(
        "SELECT value, is_anomaly FROM readings WHERE observation_id = ? ORDER BY id",
        (row["id"],),
    ).fetchall()
    return ObservationOut(
        id=row["id"],
        timestamp=row["timestamp"],
        reference_value=row["reference_value"],
        tolerance=row["tolerance"],
        median_raw=row["median_raw"],
        bias=row["bias"],
        readings=[
            ReadingOut(value=r["value"], is_anomaly=bool(r["is_anomaly"]))
            for r in reading_rows
        ],
    )


def recompute_intervals(conn: sqlite3.Connection) -> None:
    """按观测时点重建全部相邻区间。新增观测后调用，原始读数/观测不受影响。"""
    conn.execute("DELETE FROM intervals")
    points = conn.execute(
        "SELECT id, timestamp, bias FROM observations ORDER BY timestamp"
    ).fetchall()
    for a, b in zip(points, points[1:]):
        conn.execute(
            """
            INSERT INTO intervals (
                start_observation_id, end_observation_id,
                start_timestamp, end_timestamp, start_bias, end_bias
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (a["id"], b["id"], a["timestamp"], b["timestamp"], a["bias"], b["bias"]),
        )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@app.get("/health", tags=["运维"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/v1/observations",
    response_model=ObservationOut,
    status_code=status.HTTP_201_CREATED,
    tags=["观测"],
)
def create_observation(
    payload: ObservationCreate, conn: sqlite3.Connection = Depends(get_db)
) -> ObservationOut:
    duplicate = conn.execute(
        "SELECT 1 FROM observations WHERE timestamp = ?", (payload.timestamp,)
    ).fetchone()
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"时点 {payload.timestamp} 已存在观测，原始数据保留不变",
        )

    raw_median = float(median(payload.readings))
    anomalies = [
        abs(v - raw_median) > payload.tolerance for v in payload.readings
    ]
    kept = [v for v, bad in zip(payload.readings, anomalies) if not bad]
    if not kept:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="所有读数均偏离组中位数超过容差，无法计算校准偏差",
        )

    bias = payload.reference_value - float(median(kept))

    cur = conn.execute(
        """
        INSERT INTO observations
            (timestamp, reference_value, tolerance, median_raw, bias)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            payload.timestamp,
            payload.reference_value,
            payload.tolerance,
            raw_median,
            bias,
        ),
    )
    obs_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO readings (observation_id, value, is_anomaly) VALUES (?, ?, ?)",
        [(obs_id, v, 1 if bad else 0) for v, bad in zip(payload.readings, anomalies)],
    )

    # 新增观测后重算相邻区间；观测与原始读数始终保留
    recompute_intervals(conn)

    row = load_observation(conn, obs_id)
    return observation_to_out(row, conn)


@app.get("/api/v1/observations", response_model=List[ObservationOut], tags=["观测"])
def list_observations(conn: sqlite3.Connection = Depends(get_db)) -> List[ObservationOut]:
    rows = conn.execute("SELECT * FROM observations ORDER BY timestamp").fetchall()
    return [observation_to_out(r, conn) for r in rows]


@app.get("/api/v1/observations/{obs_id}", response_model=ObservationOut, tags=["观测"])
def get_observation(obs_id: int, conn: sqlite3.Connection = Depends(get_db)) -> ObservationOut:
    row = load_observation(conn, obs_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="观测不存在")
    return observation_to_out(row, conn)


@app.get("/api/v1/intervals", response_model=List[IntervalOut], tags=["区间"])
def list_intervals(conn: sqlite3.Connection = Depends(get_db)) -> List[IntervalOut]:
    rows = conn.execute(
        "SELECT * FROM intervals ORDER BY start_timestamp"
    ).fetchall()
    return [
        IntervalOut(
            start_observation_id=r["start_observation_id"],
            end_observation_id=r["end_observation_id"],
            start_timestamp=r["start_timestamp"],
            end_timestamp=r["end_timestamp"],
            start_bias=r["start_bias"],
            end_bias=r["end_bias"],
        )
        for r in rows
    ]


@app.get(
    "/api/v1/calibrate",
    response_model=CalibrateResponse,
    tags=["漂移补偿"],
    responses={400: {"description": "查询时点超出观测覆盖范围"}},
)
def calibrate(
    timestamp: float = Query(..., description="待校正读数的时点，Unix 时间戳（秒）"),
    reading: Optional[float] = Query(None, description="待校正的仪器读数（可选）"),
    conn: sqlite3.Connection = Depends(get_db),
) -> CalibrateResponse:
    bounds = conn.execute(
        "SELECT MIN(timestamp) AS t0, MAX(timestamp) AS t1 FROM observations"
    ).fetchone()
    if bounds["t0"] is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="尚无任何观测，无法进行漂移补偿",
        )
    t0, t1 = bounds["t0"], bounds["t1"]
    if timestamp < t0 or timestamp > t1:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": (
                    f"查询时点 {timestamp} 超出观测覆盖范围 [{t0}, {t1}]，"
                    "范围外查询被拒绝"
                ),
                "coverage": {"start_timestamp": t0, "end_timestamp": t1},
            },
        )
    if reading is not None and not (isinstance(reading, (int, float)) and reading == reading):
        raise HTTPException(status_code=422, detail="reading 必须为有限数值")

    exact = conn.execute(
        "SELECT * FROM observations WHERE timestamp = ?", (timestamp,)
    ).fetchone()
    if exact is not None:
        bias = exact["bias"]
        interval = IntervalDetail(
            method="exact",
            previous_observation_id=exact["id"],
            next_observation_id=exact["id"],
            start_timestamp=exact["timestamp"],
            end_timestamp=exact["timestamp"],
            start_bias=bias,
            end_bias=bias,
        )
        anomalies = [observation_to_out(exact, conn)]
    else:
        prev = conn.execute(
            "SELECT * FROM observations WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1",
            (timestamp,),
        ).fetchone()
        nxt = conn.execute(
            "SELECT * FROM observations WHERE timestamp > ? ORDER BY timestamp ASC LIMIT 1",
            (timestamp,),
        ).fetchone()

        frac = (timestamp - prev["timestamp"]) / (nxt["timestamp"] - prev["timestamp"])
        bias = prev["bias"] + frac * (nxt["bias"] - prev["bias"])

        interval = IntervalDetail(
            method="linear",
            previous_observation_id=prev["id"],
            next_observation_id=nxt["id"],
            start_timestamp=prev["timestamp"],
            end_timestamp=nxt["timestamp"],
            start_bias=prev["bias"],
            end_bias=nxt["bias"],
        )
        anomalies = [
            observation_to_out(prev, conn),
            observation_to_out(nxt, conn),
        ]

    return CalibrateResponse(
        query_timestamp=timestamp,
        input_reading=reading,
        bias=bias,
        corrected_value=None if reading is None else reading + bias,
        interval=interval,
        anomalies=anomalies,
    )
