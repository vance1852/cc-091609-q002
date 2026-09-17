"""代煎编排领域类型。

在 ``medicines.contracts`` 的药味/工序契约之上，补充处方版本、批次、
设备、投料记录、交接与班次视图所需的数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from medicines.contracts import Ingredient, Technique


class StepState(StrEnum):
    """步骤在生命周期中的状态。"""

    PENDING = "pending"          # 已编排，等待排产
    SCHEDULED = "scheduled"      # 已分配设备与时段，未投料
    WEIGHED = "weighed"          # 药味已称量，尚未入锅
    IN_PROGRESS = "in-progress"  # 已投料，正在作业
    COMPLETED = "completed"      # 已完成（记录温度与时长）
    FAILED = "failed"            # 执行中设备故障
    PAUSED = "paused"            # 受处方变更影响冻结，等待药师裁决
    SCRAPPED = "scrapped"        # 药师裁决报废
    SKIPPED = "skipped"          # 处方修订后无需执行（仅限未投料步骤）


class BatchState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    SCRAPPED = "scrapped"
    DONE = "done"


class MachineState(StrEnum):
    HEALTHY = "healthy"
    FAULTED = "faulted"
    REPAIRED = "repaired"


# 工序的标准先后顺序（同锅内）
TECHNIQUE_ORDER: dict[Technique, int] = {
    Technique.SOAK: 0,
    Technique.PRE_DECOCT: 1,
    Technique.COMBINED: 2,
    Technique.ADD_LATE: 3,
    Technique.MELT_SEPARATELY: 4,
    Technique.PACK: 5,
}

# 样例保留观察步骤：契约未定义留样工序，使用独立标识而非篡改契约
SAMPLE_RETAIN = "sample-retain"
STEP_TECHNIQUES = tuple(Technique) + (SAMPLE_RETAIN,)


@dataclass(frozen=True)
class Prescription:
    """一次处方审核结果（带版本号）。"""

    prescription_id: str
    version: int
    patient_id: str
    ingredients: tuple[Ingredient, ...]
    pack_count: int = 1
    volume_ml: int = 2000
    reviewed_at: datetime | None = None

    @property
    def display(self) -> str:
        return f"{self.prescription_id}-v{self.version}"


@dataclass
class ProcessStep:
    """可编排的工艺步骤。

    与 ``medicines.contracts.ProcessStep`` 不同，这里携带运行期状态：
    实际温度/时长、占用设备、幂等用的扫码投料记录。
    """

    step_id: str
    batch_id: str
    patient_id: str
    prescription_id: str
    prescription_version: int
    technique: str
    ingredient_ids: tuple[str, ...]
    order_index: int
    duration_seconds: int
    depends_on: tuple[str, ...] = ()
    state: StepState = StepState.PENDING
    machine_id: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    actual_temperature_celsius: float | None = None
    actual_duration_seconds: int | None = None
    charged_token: str | None = None          # 扫码投料令牌，None 表示从未投料
    charged_at: datetime | None = None
    charged_by: str | None = None
    preserved_elapsed_seconds: int = 0        # 故障迁移时保留的已执行时长
    resume_required: bool = False             # 故障迁移后等待续做（不重新投料）
    history: list[dict] = field(default_factory=list)

    @property
    def is_charged(self) -> bool:
        """药味是否已经称量或投入。"""
        return self.state in (StepState.WEIGHED, StepState.IN_PROGRESS,
                              StepState.COMPLETED, StepState.FAILED,
                              StepState.PAUSED)

    @property
    def is_finished(self) -> bool:
        return self.state == StepState.COMPLETED

    @property
    def label_compliant(self) -> bool:
        """标签能否宣称"流程合格"：必须按实际工艺（温度/时长）完成。"""
        return (self.state == StepState.COMPLETED
                and self.actual_duration_seconds is not None
                and self.actual_duration_seconds >= self.duration_seconds)

    def snapshot(self) -> dict:
        return {
            "step_id": self.step_id,
            "technique": self.technique,
            "state": self.state.value,
            "machine_id": self.machine_id,
            "ingredient_ids": list(self.ingredient_ids),
            "prescription_version": self.prescription_version,
            "actual_temperature_celsius": self.actual_temperature_celsius,
            "actual_duration_seconds": self.actual_duration_seconds,
            "charged": self.charged_token is not None,
            "resume_required": self.resume_required,
            "compliant": self.label_compliant,
            "history": list(self.history),
        }


@dataclass
class Batch:
    """同一患者同一处方版本的一料药，全程不得与他人共锅。"""

    batch_id: str
    patient_id: str
    prescription_id: str
    version: int
    steps: dict[str, ProcessStep] = field(default_factory=dict)
    state: BatchState = BatchState.ACTIVE
    replacement_of: str | None = None       # 报废后重开批次指向旧批
    pharmacist_decision: str | None = None

    def ordered_steps(self) -> list[ProcessStep]:
        return [self.steps[sid] for sid in
                sorted(self.steps, key=lambda s: self.steps[s].order_index)]


@dataclass
class Machine:
    machine_id: str
    capacity_ml: int
    station: str
    state: MachineState = MachineState.HEALTHY
    faulted_at: datetime | None = None
    repaired_at: datetime | None = None
    # 当前占用该锅的患者；整批结束前不得混入其他患者内容
    occupant_patient_id: str | None = None
    occupant_batch_id: str | None = None


@dataclass
class ChargeRecord:
    """一次扫码投料记录（幂等键：(step_id, token)）。"""

    token: str
    step_id: str
    batch_id: str
    operator_id: str
    at: datetime
    accepted: bool
    reason: str = ""


@dataclass
class HandoffAcknowledgement:
    batch_id: str
    pot_id: str
    from_operator: str
    to_operator: str
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None

    @property
    def done(self) -> bool:
        return self.acknowledged_at is not None


@dataclass
class FinishedGood:
    """成品登记。每批最多一份，故障迁移不得产生第二份。"""

    batch_id: str
    patient_id: str
    prescription_id: str
    produced_at: datetime
    pack_count: int
    compliant: bool
