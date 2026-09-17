"""代煎编排与交接后端核心。

围绕四个不可违背的约束构建：

1. 每一步都由 *处方版本 + 设备容量 + 实际操作* 共同约束——
   计划来自审核后的处方版本，占机经过容量/相容性检查，推进只接受扫码上报的
   实际操作（含温度、时长、操作人）。
2. 已发生的事实不删除：完成的步骤、温度时长、成品编号在故障迁移、处方变更、
   重排过程中原样保留。
3. 扫码幂等：同一投料扫码码重复上报只返回既有结果，绝不二次投料；故障迁移
   续煎不需要重新投料。
4. 跨班逐锅确认：未完成逐锅交接确认的锅，接班后不能继续投料操作。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .contracts import MachineEvent, Technique
from .models import (
    MATERIAL_COMMITTED,
    TERMINAL,
    ActualRecord,
    Batch,
    Machine,
    PotHandoff,
    RuleViolation,
    RunningSegment,
    Step,
    StepStatus,
)
from .planning import (
    STANDARD_TEMPERATURE,
    TECHNIQUE_KIND,
    AuditedPrescription,
    Scheduler,
    build_batch,
)

# 后下药允许进入的窗口：合煎剩余时间不超过该值时才可投后下药。
ADD_LATE_WINDOW_SECONDS = 5 * 60
# 温度允许偏差，超出记为工艺偏差。
TEMPERATURE_TOLERANCE = 3.0
# 超过计划完成时间多久视为逾期风险。
OVERDUE_GRACE = timedelta(minutes=5)


@dataclass
class ChargeResult:
    step_id: str
    duplicated: bool
    machine_id: str


@dataclass(frozen=True)
class OverdueRisk:
    batch_id: str
    patient_id: str
    step_id: str
    technique: str
    planned_finish: datetime
    risk: str
    responsible: str


@dataclass(frozen=True)
class BatchReport:
    batch_id: str
    patient_id: str
    prescription_id: str
    version: int
    status: str
    responsible: str
    product_id: str | None
    compliant_label: bool
    steps: list[dict]


class DecoctionOrchestrator:
    def __init__(self, machines: list[Machine], base_time: datetime):
        self.base_time = base_time
        self.scheduler = Scheduler(machines)
        self.batches: dict[str, Batch] = {}
        self._events: list[MachineEvent] = []
        self._handoffs: dict[str, PotHandoff] = {}
        # 当班责任人：默认按锅记录当前负责人；交接后更新。
        self._pot_owner: dict[str, str] = {}
        self._pharmacist = "pharmacist-on-duty"
        self._product_seq = 0
        self._event_seq = 0

    # ------------------------------------------------------------------
    # 处方接入
    # ------------------------------------------------------------------

    def admit(self, rx: AuditedPrescription, at: datetime | None = None) -> Batch:
        """接收处方审核结果，生成批次并排程。同一处方同版本不重复接入。"""
        batch = build_batch(rx, at or self.base_time)
        if batch.batch_id in self.batches:
            return self.batches[batch.batch_id]
        self.batches[batch.batch_id] = batch
        self._schedule_batch(batch)
        return batch

    def _schedule_batch(self, batch: Batch) -> None:
        """按契约时间线（earliest_start 已编码先后/并行关系）占机。

        运行期若前置步骤实际完成更晚，则顺延。
        """
        by_id = {s.step_id: s for s in batch.steps}
        for step in batch.steps:
            if step.status in TERMINAL or step.status is StepStatus.PAUSED:
                continue
            earliest = step.spec.earliest_start
            for dep_id in step.spec.depends_on:
                dep = by_id.get(dep_id)
                if dep is None:
                    continue
                anchor = dep.actual_finished_at or dep.planned_finish
                if anchor is not None:
                    earliest = max(earliest, anchor)
            self.scheduler.reserve(step, earliest)

    # ------------------------------------------------------------------
    # 实际操作：称量 / 扫码投料 / 完成
    # ------------------------------------------------------------------

    def _require_handoff_clear(self, machine_id: str, operator_id: str) -> None:
        """跨班后必须逐锅确认交接，否则该锅不接受新操作。"""
        h = self._handoffs.get(machine_id)
        if h is not None and not h.confirmed:
            raise RuleViolation(
                f"{machine_id} 跨班交接未经逐锅确认（{h.from_operator}→{h.to_operator}），"
                f"{operator_id} 不能在该锅继续操作"
            )

    def weigh(self, batch_id: str, technique: Technique, operator_id: str) -> Step:
        """称量。称量后物料即承诺，处方变更只能暂停等待药师裁决。"""
        batch = self.batches[batch_id]
        batch.last_operator = operator_id
        step = self._step(batch_id, technique)
        if step.status in (StepStatus.WEIGHED, StepStatus.IN_PROGRESS, StepStatus.COMPLETED):
            return step  # 称量幂等
        if step.status is not StepStatus.PLANNED:
            raise RuleViolation(f"步骤 {step.step_id} 状态 {step.status} 不可称量")
        step.status = StepStatus.WEIGHED
        step.weighed_by = operator_id
        return step

    def charge(
        self,
        batch_id: str,
        technique: Technique,
        operator_id: str,
        scan_code: str,
        machine_id: str | None = None,
        at: datetime | None = None,
    ) -> ChargeResult:
        """扫码投料。同一扫码码重试不重复投料。"""
        at = at or self.base_time
        step = self._step(batch_id, technique)
        self._batch_of(step).last_operator = operator_id

        # 幂等：该步骤已消费过此扫码码，直接回放既有占机结果。
        if scan_code in step.charged_scan_codes:
            return ChargeResult(step.step_id, duplicated=True, machine_id=step.planned_machine_id)

        if step.status is StepStatus.COMPLETED:
            return ChargeResult(step.step_id, duplicated=True, machine_id=step.planned_machine_id)
        if step.status is StepStatus.PAUSED:
            raise RuleViolation(f"步骤 {step.step_id} 已暂停，等待药师裁决")
        if step.status is StepStatus.INTERRUPTED:
            raise RuleViolation(
                f"步骤 {step.step_id} 因故障中断：须走锅次迁移续煎，不得重新投料"
            )
        if step.status is not StepStatus.WEIGHED:
            raise RuleViolation(f"步骤 {step.step_id} 状态 {step.status}，须先称量才能投料")

        target = machine_id or step.planned_machine_id
        if target is None:
            raise RuleViolation(f"步骤 {step.step_id} 没有可占用的设备")
        machine = self.scheduler.machines.get(target)
        if machine is None:
            raise RuleViolation(f"未知设备 {target}")

        if machine.kind != TECHNIQUE_KIND[technique]:
            raise RuleViolation(
                f"{technique.value} 必须在 {TECHNIQUE_KIND[technique]} 设备上，"
                f"{target} 是 {machine.kind}"
            )
        if machine.faulted:
            raise RuleViolation(f"设备 {target} 故障中，不能投料")
        self._require_handoff_clear(target, operator_id)
        self._guard_capacity_and_purity(target, step)

        # 后下专项门禁：合煎未进入末段窗口不得投后下药。
        if technique is Technique.ADD_LATE:
            self._guard_add_late(step, at)
        # 依赖门禁：所有前置步骤必须实际完成。
        self._guard_dependencies(step)

        step.charged_scan_codes.add(scan_code)
        step.status = StepStatus.IN_PROGRESS
        step.planned_machine_id = target

        step.running = RunningSegment(
            machine_id=target,
            operator_id=operator_id,
            started_at=at,
            temperature_celsius=STANDARD_TEMPERATURE.get(technique, 100.0),
        )
        self._pot_owner[target] = operator_id
        self._record_event(target, step.step_id, operator_id, at, "charge", scan_code)
        return ChargeResult(step.step_id, duplicated=False, machine_id=target)

    def complete(
        self,
        batch_id: str,
        technique: Technique,
        operator_id: str,
        at: datetime,
        temperature_celsius: float | None = None,
        duration_seconds: int | None = None,
    ) -> Step:
        """登记步骤的实际完成，固化温度与时长（不可改写）。"""
        step = self._step(batch_id, technique)
        if step.status is StepStatus.COMPLETED:
            return step  # 完成幂等：扫码重试不会产生第二份记录/成品
        self.batches[batch_id].last_operator = operator_id
        if step.status is not StepStatus.IN_PROGRESS or step.running is None:
            raise RuleViolation(f"步骤 {step.step_id} 未在执行中，不能完成")

        seg = step.running
        segment_duration = max(0, int((at - seg.started_at).total_seconds()))
        # 续煎场景：故障前已完成的时长必须保留，与本段累计。
        preserved = sum(a.duration_seconds for a in step.actuals)
        actual_duration = (
            duration_seconds if duration_seconds is not None else preserved + segment_duration
        )
        temp = temperature_celsius if temperature_celsius is not None else seg.temperature_celsius
        required = step.spec.duration_seconds
        if actual_duration < required:
            step.violations.append(
                f"时长不足：累计实际 {actual_duration}s < 标准 {required}s"
            )
        std_temp = STANDARD_TEMPERATURE.get(technique)
        if std_temp is not None and temp < std_temp - TEMPERATURE_TOLERANCE:
            step.violations.append(f"温度偏低：实际 {temp}℃ < 标准 {std_temp}℃")

        step.actuals.append(
            ActualRecord(
                machine_id=seg.machine_id,
                operator_id=operator_id,
                started_at=seg.started_at,
                finished_at=at,
                temperature_celsius=temp,
                duration_seconds=segment_duration if duration_seconds is None else duration_seconds - preserved,
            )
        )
        step.running = None
        step.status = StepStatus.COMPLETED
        self._record_event(seg.machine_id, step.step_id, operator_id, at, "complete", temp=temp)

        if technique is Technique.PACK:
            # 成品编号在分装完成时一次性分配，迁移/重试都不会重新分配。
            self._product_seq += 1
            self.batches[batch_id].product_id = f"product:{batch_id}:{self._product_seq:04d}"
        return step

    def _guard_dependencies(self, step: Step) -> None:
        batch = self._batch_of(step)
        by_id = {s.step_id: s for s in batch.steps}
        # 后下与合煎同锅末段并行：合煎必须在锅（in-progress），而非已完成。
        if step.technique is Technique.ADD_LATE:
            combined = batch.step(Technique.COMBINED)
            if combined is None or combined.status is not StepStatus.IN_PROGRESS:
                raise RuleViolation("合煎未在进行，后下药不得投入")
            return
        for dep_id in step.spec.depends_on:
            dep = by_id[dep_id]
            if dep.status is not StepStatus.COMPLETED:
                raise RuleViolation(
                    f"步骤 {step.step_id} 的前置 {dep_id}（{dep.technique.value}）尚未完成"
                )

    def _guard_add_late(self, late_step: Step, at: datetime) -> None:
        """后下药只能在合煎进入末段窗口后投入（含故障续煎：保留时长累计计算）。"""
        batch = self._batch_of(late_step)
        combined = batch.step(Technique.COMBINED)
        if combined is None or combined.status is not StepStatus.IN_PROGRESS:
            raise RuleViolation("合煎尚未进行，后下药不得提前投入")
        if combined.running is None:
            raise RuleViolation("合煎未在锅中，后下药不得投入")
        preserved = sum(a.duration_seconds for a in combined.actuals)
        elapsed_this_segment = (at - combined.running.started_at).total_seconds()
        remaining = (
            combined.spec.duration_seconds - preserved - elapsed_this_segment
        )
        if remaining > ADD_LATE_WINDOW_SECONDS:
            raise RuleViolation(
                f"后下药提前投入：合煎剩余 {int(remaining)}s > 窗口 {ADD_LATE_WINDOW_SECONDS}s"
            )

    def _guard_capacity_and_purity(self, machine_id: str, step: Step) -> None:
        machine = self.scheduler.machines[machine_id]
        # 当前仍在该设备锅内的步骤。
        active = [
            s
            for b in self.batches.values()
            for s in b.steps
            if s.status is StepStatus.IN_PROGRESS
            and s.running is not None
            and s.running.machine_id == machine_id
            and s.step_id != step.step_id
        ]
        if len(active) >= machine.capacity_batches:
            raise RuleViolation(f"设备 {machine_id} 容量已满（{machine.capacity_batches} 锅）")
        if machine.kind == "pot":
            for other in active:
                if other.patient_id != step.patient_id:
                    raise RuleViolation(
                        f"禁止混锅：{machine_id} 锅内有其他患者 {other.patient_id} 的内容"
                    )

    # ------------------------------------------------------------------
    # 设备故障与迁移
    # ------------------------------------------------------------------

    def machine_fault(self, machine_id: str, at: datetime, operator_id: str) -> list[Batch]:
        """设备故障：

        - 在锅步骤：固化故障前的温度/时长，迁移到健康锅续煎（不重新投料）；
        - 已完成步骤：原样保留；
        - 该锅上其它批次的未来预留：释放后重新落到健康设备；
        - 已产出的成品不受影响，绝不生成第二份成品。
        """
        self.scheduler.mark_faulted(machine_id)
        self.scheduler.release_machine(machine_id)
        affected: set[str] = set()

        for batch in self.batches.values():
            for step in batch.steps:
                # 在锅步骤：保留已煎段。
                if (
                    step.status is StepStatus.IN_PROGRESS
                    and step.running is not None
                    and step.running.machine_id == machine_id
                ):
                    seg = step.running
                    elapsed = max(0, int((at - seg.started_at).total_seconds()))
                    step.actuals.append(
                        ActualRecord(
                            machine_id=machine_id,
                            operator_id=seg.operator_id,
                            started_at=seg.started_at,
                            finished_at=at,
                            temperature_celsius=seg.temperature_celsius,
                            duration_seconds=elapsed,
                        )
                    )
                    step.running = None
                    step.status = StepStatus.INTERRUPTED
                    step.notes.append(
                        f"{machine_id} 故障中断，已煎 {elapsed}s（温度 {seg.temperature_celsius}℃）保留"
                    )
                    self._record_event(machine_id, step.step_id, operator_id, at, "machine-fault")
                    affected.add(batch.batch_id)
                # 未来预留在故障锅上的步骤：释放后换锅。
                elif (
                    step.status in (StepStatus.PLANNED, StepStatus.WEIGHED)
                    and step.planned_machine_id == machine_id
                ):
                    affected.add(batch.batch_id)

        healthy = [m for m in self.scheduler.machines.values() if not m.faulted]
        for bid in affected:
            self._requeue_after_fault(self.batches[bid], healthy, at)
        return [self.batches[b] for b in sorted(affected)]

    def _requeue_after_fault(self, batch: Batch, healthy: list[Machine], now: datetime) -> None:
        """把受影响批次的未完成步骤落到健康锅；已完成步骤不重新占机。"""
        by_id = {s.step_id: s for s in batch.steps}
        for step in batch.steps:
            if step.status is StepStatus.COMPLETED:
                continue
            if step.status not in (
                StepStatus.INTERRUPTED,
                StepStatus.PLANNED,
                StepStatus.WEIGHED,
            ):
                continue
            done = step.actual_duration_seconds or 0
            remaining = max(0, step.spec.duration_seconds - done)
            if step.status is StepStatus.INTERRUPTED:
                # 在锅中断：即刻可续煎，只需补足剩余时长。
                start = now
                finish = now + timedelta(seconds=max(1, remaining))
            else:
                # 尚未开始的步骤保留契约时间线（烊化并行等）。
                start = step.spec.earliest_start
                for dep_id in step.spec.depends_on:
                    dep = by_id.get(dep_id)
                    if dep is None:
                        continue
                    anchor = dep.actual_finished_at or dep.planned_finish
                    if anchor is not None:
                        start = max(start, anchor)
                # 后下锚定合煎新窗口的末段（结束前 5 分钟）。
                if step.technique is Technique.ADD_LATE:
                    combined = batch.step(Technique.COMBINED)
                    if combined is not None and combined.planned_finish is not None:
                        start = (
                            combined.planned_finish
                            - timedelta(seconds=step.spec.duration_seconds)
                        )
                start = max(start, now)
                finish = start + timedelta(seconds=step.spec.duration_seconds)
            target = self._pick_healthy(step, healthy, start, finish)
            if target is None:
                step.notes.append("无健康设备可迁移，等待设备恢复")
                continue
            # 先清掉该步骤的旧预留（可能在水浴杯/分装台等其它设备上），再登记新占用。
            self.scheduler.release_step(step.step_id)
            self.scheduler.assign(step, target, start, finish)
            if step.status is StepStatus.INTERRUPTED:
                step.notes.append(
                    f"迁移至 {target} 续煎：剩余约 {remaining}s，已完成 {done}s 与温度记录保留，"
                    f"原药味不重复投入"
                )

    def _pick_healthy(
        self,
        step: Step,
        healthy: list[Machine],
        start: datetime,
        finish: datetime,
    ) -> str | None:
        # 按设备登记顺序挑选（确定性），只考虑健康设备。
        healthy_ids = {m.machine_id for m in healthy}
        for mid in self.scheduler.machines:
            if mid not in healthy_ids:
                continue
            if self.scheduler.can_accommodate(step, start, finish, machine_id=mid):
                return mid
        return None

    def resume_interrupted(
        self,
        batch_id: str,
        technique: Technique,
        operator_id: str,
        at: datetime,
        resume_code: str | None = None,
    ) -> Step:
        """故障后在新设备续煎。

        使用 *锅次迁移码* 扫码确认入锅：与药味投料码分属不同语义，不会把药味
        再投一遍；已保留的 actuals 时长继续累计；同一迁移码重试幂等。
        """
        step = self._step(batch_id, technique)
        if resume_code is not None and resume_code in step.resume_scan_codes:
            return step
        if step.status is not StepStatus.INTERRUPTED:
            raise RuleViolation(f"步骤 {step.step_id} 未处于中断状态")
        self.batches[batch_id].last_operator = operator_id
        target = step.planned_machine_id
        if target is None or self.scheduler.machines[target].faulted:
            raise RuleViolation("没有可用的迁移设备")
        self._require_handoff_clear(target, operator_id)
        self._guard_capacity_and_purity(target, step)

        if resume_code is not None:
            step.resume_scan_codes.add(resume_code)
        step.status = StepStatus.IN_PROGRESS
        step.running = RunningSegment(
            machine_id=target,
            operator_id=operator_id,
            started_at=at,
            temperature_celsius=STANDARD_TEMPERATURE.get(technique, 100.0),
        )
        self._pot_owner[target] = operator_id
        self._record_event(target, step.step_id, operator_id, at, "resume")
        return step

    # ------------------------------------------------------------------
    # 处方变更
    # ------------------------------------------------------------------

    def revise_prescription(self, new_rx: AuditedPrescription) -> dict[str, object]:
        """处方换版。

        - 尚未称量/投料的批次：未开始步骤标记 superseded 并释放占机，
          按新版本重建；已完成步骤作为事实保留（旧批保留可追溯，不再推进）。
        - 已称量或已投入（含在锅/中断）的批次：整批未完成步骤暂停，
          交药师决定报废或继续——系统绝不擅自重排已承诺物料。
        """
        result: dict[str, object] = {"superseded": [], "paused": []}
        old = [
            b
            for b in self.batches.values()
            if b.prescription_id == new_rx.prescription_id and b.version != new_rx.version
        ]
        for batch in old:
            committed = any(s.status in MATERIAL_COMMITTED for s in batch.steps)
            if committed:
                for s in batch.steps:
                    if s.status not in TERMINAL:
                        s.previous_status = s.status
                        s.status = StepStatus.PAUSED
                batch.paused_reason = (
                    f"处方变更 v{batch.version}→v{new_rx.version}，"
                    f"物料已称量/投入，暂停等待药师裁决"
                )
                result["paused"].append(batch.batch_id)
            else:
                for s in batch.steps:
                    if s.status is not StepStatus.COMPLETED:
                        s.status = StepStatus.SUPERSEDED
                    self.scheduler.release_step(s.step_id)
                result["superseded"].append(batch.batch_id)
        new_batch = self.admit(new_rx)
        result["new_batch"] = new_batch.batch_id
        return result

    def pharmacist_decision(
        self, batch_id: str, scrap: bool, pharmacist_id: str
    ) -> Batch:
        """药师对暂停批次裁决：报废（scrapped）或按旧版继续。"""
        batch = self.batches[batch_id]
        if not any(s.status is StepStatus.PAUSED for s in batch.steps):
            raise RuleViolation(f"批次 {batch_id} 不处于暂停裁决状态")
        if scrap:
            for s in batch.steps:
                if s.status is StepStatus.PAUSED:
                    s.status = StepStatus.SCRAPPED
                    self.scheduler.release_step(s.step_id)
            batch.paused_reason = (batch.paused_reason or "") + "；药师裁决报废"
        else:
            for s in batch.steps:
                if s.status is StepStatus.PAUSED:
                    s.status = s.previous_status or StepStatus.PLANNED
            batch.paused_reason = (
                batch.paused_reason or ""
            ) + f"；药师 {pharmacist_id} 裁决按 v{batch.version} 继续"
        return batch

    # ------------------------------------------------------------------
    # 跨班交接（逐锅确认）
    # ------------------------------------------------------------------

    def open_handoff(self, machine_ids: list[str], from_op: str, to_op: str) -> list[PotHandoff]:
        """发起交接：为每口当前有在制内容的锅生成一条待确认记录。"""
        out = []
        for mid in machine_ids:
            batch_ids = tuple(
                b.batch_id
                for b in self.batches.values()
                for s in b.steps
                if s.planned_machine_id == mid
                and s.status in (StepStatus.IN_PROGRESS, StepStatus.INTERRUPTED, StepStatus.WEIGHED)
            )
            h = PotHandoff(
                machine_id=mid,
                from_operator=from_op,
                to_operator=to_op,
                batch_ids=batch_ids,
                confirmed=False,
            )
            self._handoffs[mid] = h
            out.append(h)
        return out

    def confirm_handoff(self, machine_id: str, to_operator: str) -> PotHandoff:
        """接班人逐锅确认。未确认前该锅的投料/续煎会被拒绝。"""
        h = self._handoffs.get(machine_id)
        if h is None:
            raise RuleViolation(f"{machine_id} 没有待确认的交接")
        if to_operator != h.to_operator:
            raise RuleViolation(f"{machine_id} 必须由接班人 {h.to_operator} 本人确认")
        self._handoffs[machine_id] = PotHandoff(
            machine_id=h.machine_id,
            from_operator=h.from_operator,
            to_operator=h.to_operator,
            batch_ids=h.batch_ids,
            confirmed=True,
        )
        self._pot_owner[machine_id] = to_operator
        return self._handoffs[machine_id]

    def responsible_for(self, batch: Batch) -> str:
        """批次当前责任人。

        暂停裁决 → 值班药师；在锅/中断 → 该锅当前负责人（交接后即接班人）；
        仅称量未投料 → 称量人；尚未开始 → 计划设备负责人。
        """
        if any(s.status is StepStatus.PAUSED for s in batch.steps):
            return self._pharmacist
        for s in batch.steps:
            if s.running is not None:
                return self._pot_owner.get(s.running.machine_id, s.running.operator_id)
        for s in batch.steps:
            if s.status is StepStatus.INTERRUPTED and s.planned_machine_id:
                owner = self._pot_owner.get(s.planned_machine_id)
                if owner:
                    return owner
        for s in batch.steps:
            if s.status is StepStatus.WEIGHED and s.weighed_by:
                return s.weighed_by
        if batch.last_operator:
            return batch.last_operator
        planned = next((s.planned_machine_id for s in batch.steps if s.planned_machine_id), None)
        return self._pot_owner.get(planned or "", "unassigned")

    # ------------------------------------------------------------------
    # 班次结束报告
    # ------------------------------------------------------------------

    def shift_report(self, now: datetime) -> list[BatchReport]:
        reports = []
        for batch in self.batches.values():
            steps_view = []
            for s in batch.steps:
                steps_view.append(
                    {
                        "step_id": s.step_id,
                        "technique": s.technique.value,
                        "status": s.status.value,
                        "machine": s.planned_machine_id,
                        "planned_finish": s.planned_finish.isoformat()
                        if s.planned_finish
                        else None,
                        "actual": None
                        if not s.actuals
                        else {
                            "started_at": s.actual_started_at.isoformat(),
                            "finished_at": s.actual_finished_at.isoformat(),
                            "temperature_celsius": s.actual_temperature_celsius,
                            "duration_seconds": s.actual_duration_seconds,
                            "segments": [
                                {
                                    "machine": a.machine_id,
                                    "operator": a.operator_id,
                                    "from": a.started_at.isoformat(),
                                    "to": a.finished_at.isoformat(),
                                    "temperature_celsius": a.temperature_celsius,
                                    "duration_seconds": a.duration_seconds,
                                }
                                for a in s.actuals
                            ],
                        },
                        "violations": list(s.violations),
                        "notes": list(s.notes),
                    }
                )
            # 标签合规：全部步骤完成、无工艺偏差、成品唯一，标签才允许显示“流程合格”。
            compliant = (
                batch.is_done
                and all(not s.violations for s in batch.steps)
                and all(s.status is StepStatus.COMPLETED for s in batch.steps)
            )
            reports.append(
                BatchReport(
                    batch_id=batch.batch_id,
                    patient_id=batch.patient_id,
                    prescription_id=batch.prescription_id,
                    version=batch.version,
                    status="done" if batch.is_done else "active",
                    responsible=self.responsible_for(batch),
                    product_id=batch.product_id,
                    compliant_label=compliant,
                    steps=steps_view,
                )
            )
        return reports

    def overdue_risks(self, now: datetime) -> list[OverdueRisk]:
        risks = []
        for batch in self.batches.values():
            for s in batch.steps:
                if s.status in TERMINAL:
                    continue
                deadline = s.planned_finish
                if deadline is None:
                    continue
                if s.status is StepStatus.PAUSED:
                    risk = "暂停待药师裁决，存在逾期风险"
                elif s.status is StepStatus.INTERRUPTED:
                    risk = "设备故障中断待续煎，存在逾期风险"
                elif now + OVERDUE_GRACE >= deadline:
                    risk = "接近或超过计划完成时间"
                else:
                    continue
                risks.append(
                    OverdueRisk(
                        batch_id=batch.batch_id,
                        patient_id=batch.patient_id,
                        step_id=s.step_id,
                        technique=s.technique.value,
                        planned_finish=deadline,
                        risk=risk,
                        responsible=self.responsible_for(batch),
                    )
                )
        return risks

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _step(self, batch_id: str, technique: Technique) -> Step:
        batch = self.batches[batch_id]
        step = batch.step(technique)
        if step is None:
            raise RuleViolation(f"批次 {batch_id} 没有 {technique.value} 步骤")
        return step

    def _batch_of(self, step: Step) -> Batch:
        return next(b for b in self.batches.values() if step in b.steps)

    def _record_event(
        self,
        machine_id: str,
        step_id: str,
        operator_id: str,
        at: datetime,
        kind: str,
        scan_code: str | None = None,
        temp: float | None = None,
    ) -> None:
        self._event_seq += 1
        self._events.append(
            MachineEvent(
                event_id=f"evt:{self._event_seq:05d}:{kind}",
                machine_id=machine_id,
                step_id=step_id,
                occurred_at=at,
                operator_id=operator_id,
                temperature_celsius=temp,
                scan_code=scan_code,
            )
        )

    @property
    def events(self) -> list[MachineEvent]:
        return list(self._events)
