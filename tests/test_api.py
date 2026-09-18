import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["CALIBRATION_DB"] = _tmp.name

from app.main import app, get_db  # noqa: E402


@pytest.fixture()
def client():
    # 每个测试使用独立的临时数据库
    db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_file.close()
    os.environ["CALIBRATION_DB"] = db_file.name

    from app import main as main_module

    main_module.DB_PATH = db_file.name
    main_module.init_db()

    with TestClient(app) as c:
        yield c

    os.unlink(db_file.name)


def _post(client, ts, ref, tolerance, readings):
    return client.post(
        "/api/v1/observations",
        json={
            "timestamp": ts,
            "reference_value": ref,
            "tolerance": tolerance,
            "readings": readings,
        },
    )


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_observation_marks_anomaly_and_bias(client):
    # 中位数为 10.0；读数 50 偏离 40 > 容差 5 -> 异常
    r = _post(client, ts=100.0, ref=12.0, tolerance=5.0, readings=[9.0, 10.0, 11.0, 50.0])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["median_raw"] == 10.5  # (10+11)/2
    flags = [rd["is_anomaly"] for rd in body["readings"]]
    assert flags == [False, False, False, True]
    # 保留读数中位数 = 10 -> bias = 12 - 10 = 2
    assert body["bias"] == pytest.approx(2.0)


def test_requires_at_least_three_readings(client):
    r = _post(client, ts=1.0, ref=1.0, tolerance=0.1, readings=[1.0, 1.0])
    assert r.status_code == 422


def test_duplicate_timestamp_conflict(client):
    assert _post(client, 0.0, 1.0, 0.5, [1.0, 1.0, 1.0]).status_code == 201
    r = _post(client, 0.0, 2.0, 0.5, [2.0, 2.0, 2.0])
    assert r.status_code == 409


def test_all_anomalous_rejected(client):
    # 中位数本身为某一读数（奇数个）时，该读数偏差为 0 永远保留；
    # 偶数个读数时中位数可能不是实际读数，此时可能全部异常。
    # [0, 0, 10, 10] 中位数 5，容差 1：每个读数偏离均为 5 -> 全部异常
    r = _post(client, 0.0, 5.0, 1.0, [0.0, 0.0, 10.0, 10.0])
    assert r.status_code == 422
    assert "无法计算校准偏差" in r.json()["detail"]


def test_calibrate_interpolation_and_out_of_range(client):
    # t=0: 读数 [9,10,11] 中位 10, ref 12 -> bias 2
    _post(client, 0.0, 12.0, 5.0, [9.0, 10.0, 11.0])
    # t=10: 读数 [19,20,21] 中位 20, ref 24 -> bias 4
    _post(client, 10.0, 24.0, 5.0, [19.0, 20.0, 21.0])

    # 中点插值：bias = 3
    r = client.get("/api/v1/calibrate", params={"timestamp": 5.0, "reading": 15.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bias"] == pytest.approx(3.0)
    assert body["corrected_value"] == pytest.approx(18.0)
    assert body["interval"]["method"] == "linear"
    assert len(body["anomalies"]) == 2
    assert all(
        all(not rd["is_anomaly"] for rd in obs["readings"]) for obs in body["anomalies"]
    )

    # 精确匹配观测时点
    r = client.get("/api/v1/calibrate", params={"timestamp": 0.0, "reading": 10.0})
    body = r.json()
    assert body["interval"]["method"] == "exact"
    assert body["bias"] == pytest.approx(2.0)
    assert body["corrected_value"] == pytest.approx(12.0)
    assert len(body["anomalies"]) == 1

    # 超出覆盖范围 -> 400
    r = client.get("/api/v1/calibrate", params={"timestamp": -1.0})
    assert r.status_code == 400
    assert "超出观测覆盖范围" in r.json()["detail"]

    r = client.get("/api/v1/calibrate", params={"timestamp": 10.0001})
    assert r.status_code == 400


def test_intervals_recomputed_after_new_observation(client):
    _post(client, 0.0, 12.0, 5.0, [10.0, 10.0, 10.0])
    _post(client, 10.0, 24.0, 5.0, [20.0, 20.0, 20.0])
    intervals = client.get("/api/v1/intervals").json()
    assert len(intervals) == 1

    # 在中间插入新观测，原始数据保留，区间重算为两个
    r = _post(client, 5.0, 19.0, 5.0, [16.0, 16.0, 16.0])
    assert r.status_code == 201
    intervals = client.get("/api/v1/intervals").json()
    assert len(intervals) == 2
    assert [iv["start_timestamp"] for iv in intervals] == [0.0, 5.0]

    # 原有两条观测仍在
    obs = client.get("/api/v1/observations").json()
    assert len(obs) == 3


def test_calibrate_without_reading_omits_corrected_value(client):
    _post(client, 0.0, 12.0, 5.0, [10.0, 10.0, 10.0])
    _post(client, 10.0, 24.0, 5.0, [20.0, 20.0, 20.0])
    r = client.get("/api/v1/calibrate", params={"timestamp": 5.0})
    body = r.json()
    assert body["input_reading"] is None
    assert body["corrected_value"] is None
    assert body["bias"] == pytest.approx(3.0)


def test_calibrate_empty_db(client):
    r = client.get("/api/v1/calibrate", params={"timestamp": 0.0})
    assert r.status_code == 400
