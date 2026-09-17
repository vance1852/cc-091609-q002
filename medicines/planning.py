"""根据处方审核结果生成代煎工艺步骤，并把相容步骤合并占机。

生成的步骤链（一张处方一个批次）：

    浸泡 soak
      └─ 先煎 pre-decoct        （仅有先煎药味时；其依赖浸泡）
           └─ 合煎 combined      （普通药味并入；先煎药味留锅）
                ├─ 后下 add-late（合煎末段才允许投料，由运行期门禁保证）
                ├─ 烊化 melt-separately（另包，另锅隔水烊化，完成后兑入）
                ├─ 分装 pack     （依赖全部煎煮/烊化步骤）
                └─ 留样 retain-sample（分装时同步留样，与分装同批）

合并占机规则：同一设备容量窗口内，只有**工艺相容且同患者**的步骤能共享
一台煎药机；不同患者的内容绝不允许混锅。烊化使用独立烊化杯，不与汤剂共锅。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .contracts import Ingredient, ProcessStep, Technique
from .models import Batch, Machine, Step

# 各工序标准时长（秒）与参考温度（摄氏度）。
STANDARD_DURATION = {
    Technique.SOAK: 30 * 60,
    Technique.PRE_DECOCT: 30 * 60,
    Technique.COMBINED: 25 * 60,
    Technique.ADD_LATE: 5 * 60,       # 后下药只煎 5 分钟
    Technique.MELT_SEPARATELY: 10 * 60,
    Technique.PACK: 6 * 60,
    Technique.RETAIN_SAMPLE: 2 * 60,
}
STANDARD_TEMPERATURE = {
    Technique.PRE_DECOCT: 100.0,
    Technique.COMBINED: 100.0,
    Technique.ADD_LATE: 95.0,
    Technique.MELT_SEPARATELY: 85.0,  # 隔水烊化
}

# 哪些工序允许与合煎共占同一台煎药机（同一患者前提下）。
POT_COMPATIBLE = frozenset(
    {Technique.SOAK, Technique.PRE_DECOCT, Technique.COMBINED, Technique.ADD_LATE}
)

# 工序 -> 设备类型。
TECHNIQUE_KIND = {
    Technique.SOAK: "pot",
    Technique.PRE_DECOCT: "pot",
    Technique.COMBINED: "pot",
    Technique.ADD_LATE: "pot",
    Technique.MELT_SEPARATELY: "bath",   # 另包烊化：独立水浴杯
    Technique.PACK: "packer",
    Technique.RETAIN_SAMPLE: "packer",
}
# 只有煎药锅存在“混锅”问题；水浴杯、分装台上不同患者内容处于独立容器。
MIXING_SENSITIVE_KINDS = frozenset({"pot"})


@dataclass(frozen=True)
class AuditedPrescription:
    """处方审核结果。audit_passed=False 的处方不会进入编排。"""

    prescription_id: str
    patient_id: str
    version: int
    doses: int
    ingredients: tuple[Ingredient, ...]
    audit_passed: bool = True
    audit_note: str = ""


def build_steps(rx: AuditedPrescription, base_time: datetime) -> list[Step]:
    """把一张审核通过的处方展开成有序工艺步骤。

    后下与合煎共享同一口锅的末段时间窗（合煎结束前 5 分钟投药、同刻结束），
    因此它们是“相容步骤合并占机”的典型；运行期门禁保证后下药不会被提前投入。
    """
    if not rx.audit_passed:
        raise ValueError(f"处方 {rx.prescription_id} 未通过审核：{rx.audit_note}")

    def iids(tech: Technique) -> tuple[str, ...]:
        return tuple(i.ingredient_id for i in rx.ingredients if i.technique is tech)

    pre = iids(Technique.PRE_DECOCT)
    late = iids(Technique.ADD_LATE)
    melt = iids(Technique.MELT_SEPARATELY)
    special = {Technique.PRE_DECOCT, Technique.ADD_LATE, Technique.MELT_SEPARATELY}
    ordinary = tuple(
        i.ingredient_id for i in rx.ingredients if i.technique not in special
    )

    bid = f"batch:{rx.patient_id}:{rx.prescription_id}:v{rx.version}"
    specs: list[ProcessStep] = []

    def add(
        tech: Technique,
        ingredients: tuple[str, ...],
        start: datetime,
        deps: tuple[str, ...],
    ) -> str:
        sid = f"{bid}:{tech.value}"
        specs.append(
            ProcessStep(
                step_id=sid,
                batch_id=bid,
                technique=tech,
                ingredient_ids=ingredients,
                earliest_start=start,
                duration_seconds=STANDARD_DURATION[tech],
                depends_on=deps,
            )
        )
        return sid

    cursor = base_time
    soak = add(
        Technique.SOAK,
        tuple(i.ingredient_id for i in rx.ingredients),
        cursor,
        (),
    )
    cursor += timedelta(seconds=STANDARD_DURATION[Technique.SOAK])

    deps: tuple[str, ...] = (soak,)
    if pre:
        pre_id = add(Technique.PRE_DECOCT, pre, cursor, deps)
        cursor += timedelta(seconds=STANDARD_DURATION[Technique.PRE_DECOCT])
        deps = (pre_id,)

    combined_start = cursor
    # 合煎包含普通药味；若存在先煎药味，其药液/药渣留锅合煎。
    combined = add(Technique.COMBINED, ordinary + pre, combined_start, deps)
    combined_finish = combined_start + timedelta(
        seconds=STANDARD_DURATION[Technique.COMBINED]
    )
    pack_deps = (combined,)

    late_id = None
    if late:
        # 后下占合煎末段窗口：同锅、同患者、相容合并。
        late_start = combined_finish - timedelta(
            seconds=STANDARD_DURATION[Technique.ADD_LATE]
        )
        late_id = add(Technique.ADD_LATE, late, late_start, (combined,))
        pack_deps += (late_id,)

    melt_id = None
    if melt:
        # 烊化另包另锅（水浴杯），与合煎并行；只需先煎/浸泡链完成，分装前必须兑入。
        melt_id = add(Technique.MELT_SEPARATELY, melt, combined_start, deps)
        melt_finish = combined_start + timedelta(
            seconds=STANDARD_DURATION[Technique.MELT_SEPARATELY]
        )
        pack_start = max(combined_finish, melt_finish)
        pack_deps += (melt_id,)
    else:
        pack_start = combined_finish

    pack = add(Technique.PACK, (), pack_start, pack_deps)
    sample_start = pack_start + timedelta(seconds=STANDARD_DURATION[Technique.PACK])
    add(Technique.RETAIN_SAMPLE, (), sample_start, (pack,))

    return [Step(spec=s, patient_id=rx.patient_id) for s in specs]


def build_batch(rx: AuditedPrescription, base_time: datetime) -> Batch:
    steps = build_steps(rx, base_time)
    return Batch(
        batch_id=steps[0].spec.batch_id,
        prescription_id=rx.prescription_id,
        patient_id=rx.patient_id,
        version=rx.version,
        doses=rx.doses,
        steps=steps,
    )


# ---------------------------------------------------------------------------
# 占机合并
# ---------------------------------------------------------------------------


@dataclass
class Reservation:
    machine_id: str
    step_id: str
    patient_id: str
    technique: Technique
    start: datetime
    finish: datetime


class Scheduler:
    """贪心排程：相容且同患者的步骤可在容量内合并到同一台设备。"""

    def __init__(self, machines: list[Machine]):
        self.machines = {m.machine_id: m for m in machines}
        self._timeline: dict[str, list[Reservation]] = {m.machine_id: [] for m in machines}

    def release_step(self, step_id: str) -> None:
        """释放某步骤的全部占机预留（批次被替换/迁移时使用）。"""
        for mid in self._timeline:
            self._timeline[mid] = [
                r for r in self._timeline[mid] if r.step_id != step_id
            ]

    def release_machine(self, machine_id: str) -> None:
        """迁移时清空故障锅上尚未消费的预留（已完成段是事实，不在这里）。"""
        self._timeline[machine_id] = []

    def _fits(self, machine: Machine, start: datetime, finish: datetime, *, step_id: str = "") -> bool:
        if machine.faulted:
            return False
        active = [
            r
            for r in self._timeline[machine.machine_id]
            if r.step_id != step_id and r.finish > start and r.start < finish
        ]
        return len(active) < machine.capacity_batches

    def _compatible(
        self, machine: Machine, step: Step, start: datetime, finish: datetime
    ) -> bool:
        """同机窗口内的占用必须相容。

        煎药锅：同患者且工艺相容（绝不混锅）；
        水浴杯/分装台：内容物在各自独立容器内，按容量共享即可。
        """
        for r in self._timeline[machine.machine_id]:
            if r.step_id == step.step_id:
                continue
            if r.finish <= start or r.start >= finish:
                continue
            if machine.kind in MIXING_SENSITIVE_KINDS:
                if r.patient_id != step.patient_id:
                    return False
                if r.technique not in POT_COMPATIBLE or step.technique not in POT_COMPATIBLE:
                    return False
        return True

    def reserve(self, step: Step, start: datetime) -> Reservation:
        duration = step.spec.duration_seconds
        finish = start + timedelta(seconds=duration)
        required_kind = TECHNIQUE_KIND[step.technique]
        for machine in self.machines.values():
            if machine.kind != required_kind:
                continue
            if self._fits(machine, start, finish, step_id=step.step_id) and self._compatible(
                machine, step, start, finish
            ):
                res = Reservation(
                    machine_id=machine.machine_id,
                    step_id=step.step_id,
                    patient_id=step.patient_id,
                    technique=step.technique,
                    start=start,
                    finish=finish,
                )
                self._timeline[machine.machine_id].append(res)
                step.planned_machine_id = machine.machine_id
                step.planned_start = start
                step.planned_finish = finish
                return res
        raise RuntimeError(
            f"没有可容纳步骤 {step.step_id}（{step.technique.value}）的空闲 {required_kind} 设备"
        )

    def reservations_for(self, machine_id: str) -> list[Reservation]:
        return list(self._timeline.get(machine_id, ()))

    def can_accommodate(
        self, step: Step, start: datetime, finish: datetime, *, machine_id: str | None = None
    ) -> str | None:
        """返回能在该窗口容纳步骤的设备；指定 machine_id 时只检查该设备。"""
        required_kind = TECHNIQUE_KIND[step.technique]
        for machine in self.machines.values():
            if machine.kind != required_kind:
                continue
            if machine_id is not None and machine.machine_id != machine_id:
                continue
            if self._fits(machine, start, finish, step_id=step.step_id) and self._compatible(
                machine, step, start, finish
            ):
                return machine.machine_id
        return None

    def assign(
        self, step: Step, machine_id: str, start: datetime, finish: datetime
    ) -> Reservation:
        """故障迁移时直接落到指定健康设备（同患者不混锅由调用方保证）。"""
        res = Reservation(
            machine_id=machine_id,
            step_id=step.step_id,
            patient_id=step.patient_id,
            technique=step.technique,
            start=start,
            finish=finish,
        )
        self._timeline[machine_id].append(res)
        step.planned_machine_id = machine_id
        step.planned_start = start
        step.planned_finish = finish
        return res

    def mark_faulted(self, machine_id: str) -> None:
        m = self.machines.get(machine_id)
        if m is not None:
            self.machines[machine_id] = Machine(
                machine_id=m.machine_id,
                capacity_batches=m.capacity_batches,
                faulted=True,
                kind=m.kind,
            )
