"""药味、煎煮步骤与设备状态。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Technique(StrEnum):
    SOAK = "soak"
    PRE_DECOCT = "pre-decoct"
    COMBINED = "combined"
    ADD_LATE = "add-late"
    MELT_SEPARATELY = "melt-separately"
    PACK = "pack"
    RETAIN_SAMPLE = "retain-sample"


@dataclass(frozen=True)
class Ingredient:
    ingredient_id: str
    prescription_version: int
    display_name: str
    mass_grams: float
    technique: Technique


@dataclass(frozen=True)
class ProcessStep:
    step_id: str
    batch_id: str
    technique: Technique
    ingredient_ids: tuple[str, ...]
    earliest_start: datetime
    duration_seconds: int
    depends_on: tuple[str, ...] = ()
    # 实际执行数据在步骤完成后补登，仅允许写入一次。
    actual_started_at: datetime | None = None
    actual_finished_at: datetime | None = None
    actual_temperature_celsius: float | None = None
    actual_duration_seconds: int | None = None
    machine_id: str | None = None


@dataclass(frozen=True)
class MachineEvent:
    event_id: str
    machine_id: str
    step_id: str
    occurred_at: datetime
    operator_id: str
    temperature_celsius: float | None = None
    # scan_code 用于扫码投料/重试的幂等去重：同一扫码事件重复上报不会二次投料。
    scan_code: str | None = None
