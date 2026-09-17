"""代煎编排的内部状态模型。

契约层（medicines.contracts）是对外不可变结构；这里的模型允许编排器在
批次生命周期内推进状态，并集中表达一条硬规则：

**已发生的事实不可被重排或修订删除。** 完成步骤的实际温度/时长、已消费的
扫码投料、已产出的成品在故障迁移与处方变更后都必须原样保留。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .contracts import ProcessStep, Technique


class StepStatus(StrEnum):
    PLANNED = "planned"            # 已排程，尚未称量
    WEIGHED = "weighed"            # 已称量，等待投料
    IN_PROGRESS = "in-progress"    # 已扫码投料，占机进行中
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"    # 设备故障，等待迁移/扫码续作
    PAUSED = "paused"              # 处方版本冲突，等待药师裁决
    SCRAPPED = "scrapped"          # 药师裁决报废
    SUPERSEDED = "superseded"      # 尚未投料即被新版本批次替换


# 物料一旦进入这些状态，处方变更不能再静默重排，必须由药师裁决。
MATERIAL_COMMITTED = frozenset(
    {StepStatus.WEIGHED, StepStatus.IN_PROGRESS, StepStatus.INTERRUPTED}
)


# 已终结、不再参与排程的状态。
TERMINAL = frozenset({StepStatus.COMPLETED, StepStatus.SCRAPPED, StepStatus.SUPERSEDED})


@dataclass(frozen=True)
class ActualRecord:
    """一段真实执行记录。幂等完成：每段记录只写入一次。"""

    machine_id: str
    operator_id: str
    started_at: datetime
    finished_at: datetime
    temperature_celsius: float
    duration_seconds: int


@dataclass(frozen=True)
class RunningSegment:
    """投料后到完成/故障时的在锅段，保存已运行时间与起锅温度用于续煎。"""

    machine_id: str
    operator_id: str
    started_at: datetime
    temperature_celsius: float


@dataclass
class Step:
    spec: ProcessStep
    patient_id: str
    status: StepStatus = StepStatus.PLANNED
    planned_machine_id: str | None = None
    planned_start: datetime | None = None
    planned_finish: datetime | None = None
    actuals: list[ActualRecord] = field(default_factory=list)
    running: RunningSegment | None = None
    # violations 是真正的工艺偏差（影响合格标签）；notes 只记录事件经过。
    violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 已消费的投料扫码码：同一码重试不得二次投料。
    charged_scan_codes: set[str] = field(default_factory=set)
    # 已消费的锅次迁移码：续煎扫码重试不重复开段。
    resume_scan_codes: set[str] = field(default_factory=set)
    # 暂停前状态，药师裁决继续时原样恢复。
    previous_status: StepStatus | None = None
    weighed_by: str | None = None

    @property
    def step_id(self) -> str:
        return self.spec.step_id

    @property
    def technique(self) -> Technique:
        return self.spec.technique

    @property
    def actual_started_at(self) -> datetime | None:
        return self.actuals[0].started_at if self.actuals else None

    @property
    def actual_finished_at(self) -> datetime | None:
        return self.actuals[-1].finished_at if self.actuals else None

    @property
    def actual_temperature_celsius(self) -> float | None:
        if not self.actuals:
            return None
        return max(a.temperature_celsius for a in self.actuals)

    @property
    def actual_duration_seconds(self) -> int | None:
        if not self.actuals:
            return None
        return sum(a.duration_seconds for a in self.actuals)


@dataclass
class Batch:
    batch_id: str
    prescription_id: str
    patient_id: str
    version: int
    doses: int
    steps: list[Step]
    paused_reason: str | None = None
    product_id: str | None = None  # 分装完成后分配，全程唯一
    last_operator: str | None = None  # 最近一次现场操作人（班末责任人）

    def step(self, technique: Technique) -> Step | None:
        for s in self.steps:
            if s.technique == technique:
                return s
        return None

    @property
    def is_done(self) -> bool:
        return self.product_id is not None and all(
            s.status == StepStatus.COMPLETED for s in self.steps
        )


@dataclass(frozen=True)
class Machine:
    machine_id: str
    capacity_batches: int = 3
    faulted: bool = False
    # pot=煎药锅；bath=烊化水浴杯（另包另器）；packer=分装台。
    kind: str = "pot"


@dataclass(frozen=True)
class PotHandoff:
    """一口锅的跨班交接确认，必须逐锅由接收人确认。"""

    machine_id: str
    from_operator: str
    to_operator: str
    batch_ids: tuple[str, ...]
    confirmed: bool = False


class RuleViolation(Exception):
    """操作违反工艺约束（提前投后下药、未确认交接即投料等）。"""
