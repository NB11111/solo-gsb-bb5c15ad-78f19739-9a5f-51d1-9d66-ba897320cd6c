import math
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ReadingOut(BaseModel):
    value: float
    is_anomaly: bool


class ObservationCreate(BaseModel):
    """客户端在某一时点提交的一次校准观测。"""

    timestamp: float = Field(..., description="观测时点，Unix 时间戳（秒，允许浮点）")
    reference_value: float = Field(..., description="该时点的参考（真值）读数")
    tolerance: float = Field(..., gt=0, description="异常判定容差，读数与组中位数之差的绝对值超过该值即判为异常")
    readings: List[float] = Field(..., min_length=3, description="同一时点至少 3 次仪器读数")

    @field_validator("timestamp", "reference_value", "tolerance")
    @classmethod
    def _finite_scalar(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("必须为有限数值")
        return v

    @field_validator("readings")
    @classmethod
    def _finite_readings(cls, values: List[float]) -> List[float]:
        if len(values) < 3:
            raise ValueError("至少需要 3 次读数")
        if any(not math.isfinite(v) for v in values):
            raise ValueError("读数必须全部为有限数值")
        return values


class ObservationOut(BaseModel):
    id: int
    timestamp: float
    reference_value: float
    tolerance: float
    median_raw: float = Field(..., description="全部原始读数（含异常）的中位数")
    bias: float = Field(..., description="校准偏差 = 参考值 - 剔除异常后读数的中位数")
    readings: List[ReadingOut]


class IntervalOut(BaseModel):
    start_observation_id: int
    end_observation_id: int
    start_timestamp: float
    end_timestamp: float
    start_bias: float
    end_bias: float


class IntervalDetail(BaseModel):
    method: Literal["exact", "linear"]
    previous_observation_id: int
    next_observation_id: int
    start_timestamp: float
    end_timestamp: float
    start_bias: float
    end_bias: float


class CalibrateResponse(BaseModel):
    query_timestamp: float
    input_reading: Optional[float] = Field(None, description="待校正读数；未提供时为空")
    bias: float = Field(..., description="查询时点的校准偏差（线性插值或精确匹配）")
    corrected_value: Optional[float] = Field(None, description="校正结果 = 待校正读数 + 偏差")
    interval: IntervalDetail
    anomalies: List[ObservationOut] = Field(
        ..., description="区间端点观测的完整数据及异常明细（精确匹配时仅含一个观测）"
    )
