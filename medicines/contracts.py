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


@dataclass(frozen=True)
class MachineEvent:
    event_id: str
    machine_id: str
    step_id: str
    occurred_at: datetime
    operator_id: str
    temperature_celsius: float | None = None
