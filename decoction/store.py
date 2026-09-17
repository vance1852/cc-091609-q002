"""代煎编排核心：调度、扫码投料幂等、处方修订、故障迁移、跨班交接。

所有写操作都以"处方版本 + 设备容量 + 实际操作"共同约束，任何一步未满足
依赖（如后下依赖合煎完成）都不能投料；已完成步骤的温度与时长不可变。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from medicines.contracts import Technique

from .models import (
    Batch,
    BatchState,
    ChargeRecord,
    FinishedGood,
    HandoffAcknowledgement,
    Machine,
    MachineState,
    Prescription,
    ProcessStep,
    SAMPLE_RETAIN,
    StepState,
)
from .planner import (
    DECOCT_POT,
    MELT_VESSEL,
    STATION_FOR_TECHNIQUE,
    STANDARD_TEMPERATURE,
    build_batch,
    share_machine,
)

# 可重排的状态：已完成/报废/跳过/执行中的步骤不参与重排
RESCHEDULABLE = (StepState.PENDING, StepState.SCHEDULED,
                 StepState.WEIGHED, StepState.FAILED)
TERMINAL = (StepState.COMPLETED, StepState.SCRAPPED, StepState.SKIPPED)


class OrchestrationError(Exception):
    """业务规则冲突（未满足依赖、混锅、重复投料等）。"""


@dataclass
class Reservation:
    start: datetime
    end: datetime
    step_id: str


@dataclass
class PharmacistDecision:
    batch_id: str
    prescription_id: str
    old_version: int
    new_version: int
    raised_at: datetime
    resolved_at: datetime | None = None
    decision: str | None = None        # "scrap" | "continue"
    pharmacist_id: str | None = None
    replacement_batch_id: str | None = None


class DecoctionStore:
    def __init__(self, clock):
        # clock() -> 业务当前时间，可注入便于重放与测试
        self._clock = clock
        self.prescriptions: dict[str, Prescription] = {}
        self.machines: dict[str, Machine] = {}
        self.reservations: dict[str, list[Reservation]] = {}
        self.batches: dict[str, Batch] = {}
        self.steps: dict[str, ProcessStep] = {}
        self.charge_records: dict[tuple[str, str], ChargeRecord] = {}
        self.tokens: dict[str, ChargeRecord] = {}
        self.finished_goods: dict[str, FinishedGood] = {}
        self.batch_owner: dict[str, str] = {}
        self.handoffs: list[HandoffAcknowledgement] = []
        self.pharmacist_decisions: list[PharmacistDecision] = []
        self.audit_log: list[dict] = []
        self._batch_seq = 0

    # ------------------------------------------------------------------ 基础

    def _log(self, event: str, **payload) -> None:
        self.audit_log.append({"at": self._clock().isoformat(),
                               "event": event, **payload})

    def register_machine(self, machine_id: str, capacity_ml: int,
                         station: str) -> Machine:
        m = Machine(machine_id=machine_id, capacity_ml=capacity_ml,
                    station=station)
        self.machines[machine_id] = m
        self.reservations[machine_id] = []
        return m

    def _station_machines(self, station: str) -> list[Machine]:
        return sorted((m for m in self.machines.values()
                       if m.station == station), key=lambda m: m.machine_id)

    def _rx_of(self, batch: Batch) -> Prescription:
        return self.prescriptions[f"{batch.prescription_id}-v{batch.version}"]

    # ------------------------------------------------------------ 处方与批次

    def admit_prescription(self, rx: Prescription, owner: str) -> Batch:
        """接收处方审核结果，生成批次并编排、排产。"""
        self._batch_seq += 1
        batch_id = (f"batch-{rx.prescription_id}-v{rx.version}"
                    f"-{self._batch_seq:03d}")
        batch = build_batch(rx, batch_id)
        self.prescriptions[rx.display] = rx
        self.batches[batch_id] = batch
        self.steps.update(batch.steps)
        self.batch_owner[batch_id] = owner
        try:
            self._schedule_batch(batch)
        except OrchestrationError:
            # 无可用设备时回滚，避免留下无法执行的半成品批次
            self.batches.pop(batch_id, None)
            for sid in batch.steps:
                self.steps.pop(sid, None)
            self.batch_owner.pop(batch_id, None)
            self.prescriptions.pop(rx.display, None)
            self._batch_seq -= 1
            raise
        self._log("prescription_admitted", batch_id=batch_id,
                  prescription=rx.display, patient_id=rx.patient_id, owner=owner)
        return batch

    # ------------------------------------------------------------ 调度排产

    def _eligible_machines(self, station: str, step: ProcessStep
                           ) -> list[Machine]:
        """候选设备：健康、容量足够、未被其他患者占用。"""
        batch = self.batches[step.batch_id]
        volume = self._rx_of(batch).volume_ml
        out: list[Machine] = []
        for m in self._station_machines(station):
            if m.state == MachineState.FAULTED:
                continue
            if station == DECOCT_POT and m.capacity_ml < volume:
                continue
            if (m.occupant_patient_id is not None
                    and m.occupant_patient_id != step.patient_id):
                # 不同患者内容不得混锅
                continue
            out.append(m)
        return out

    def _earliest_slot(self, machine: Machine, ready_at: datetime,
                       duration: int) -> datetime:
        """在设备时间轴上找 ready_at 之后长度足够的第一个空档。"""
        intervals = sorted(
            (r for r in self.reservations[machine.machine_id]
             if r.end > ready_at),
            key=lambda r: r.start)
        start = ready_at
        for res in intervals:
            if res.start - start >= timedelta(seconds=duration):
                return start
            start = max(start, res.end)
        return start

    def _drop_reservations(self, machine_id: str, *step_ids: str) -> None:
        drop = set(step_ids)
        self.reservations[machine_id] = [
            r for r in self.reservations[machine_id]
            if r.step_id not in drop]

    def _schedule_batch(self, batch: Batch,
                        from_step_id: str | None = None) -> None:
        """按依赖拓扑为步骤分配设备与计划时段。

        from_step_id 给定时只重排该步骤及其后继（故障迁移用）；
        已完成步骤永不参与重排，其温度/时长/占用记录原样保留。
        """
        ordered = batch.ordered_steps()
        if from_step_id:
            idx = next(i for i, s in enumerate(ordered)
                       if s.step_id == from_step_id)
            for s in ordered[idx:]:
                if s.state in RESCHEDULABLE and s.machine_id:
                    self._drop_reservations(s.machine_id, s.step_id)
            ordered = ordered[idx:]

        end_cache: dict[str, datetime] = {}
        for s in self.steps.values():
            if s.state == StepState.COMPLETED and s.completed_at:
                end_cache[s.step_id] = s.completed_at

        for step in ordered:
            if step.state in (StepState.COMPLETED, StepState.SCRAPPED,
                              StepState.SKIPPED, StepState.IN_PROGRESS,
                              StepState.PAUSED):
                if step.scheduled_end:
                    end_cache[step.step_id] = step.scheduled_end
                continue

            station = STATION_FOR_TECHNIQUE[step.technique]
            # 故障迁移的步骤只重排剩余时长，保留时长不再占机
            sched_duration = (step.duration_seconds
                              - step.preserved_elapsed_seconds)
            deps_end = [end_cache[d] for d in step.depends_on
                        if d in end_cache]
            ready_at = max(deps_end) if deps_end else self._clock()

            # 合并占机：优先本批同锅组前序步骤所在且仍健康的那台机器
            preferred: Machine | None = None
            for d in step.depends_on:
                dep = self.steps.get(d)
                if (dep and dep.machine_id
                        and share_machine(dep.technique, step.technique)):
                    cand = self.machines[dep.machine_id]
                    if (cand.state != MachineState.FAULTED
                            and cand.occupant_patient_id
                            in (None, step.patient_id)):
                        preferred = cand
                        break

            eligible = self._eligible_machines(station, step)
            machine: Machine | None = None
            if preferred is not None and preferred in eligible:
                machine = preferred
            else:
                best_at = None
                for cand in eligible:
                    at = self._earliest_slot(
                        cand, ready_at, sched_duration)
                    if best_at is None or at < best_at:
                        machine, best_at = cand, at
            if machine is None:
                raise OrchestrationError(
                    f"无可用设备：步骤 {step.step_id}（{station}）"
                    "无满足容量与患者隔离要求的健康机器")

            start = self._earliest_slot(
                machine, ready_at, sched_duration)
            end = start + timedelta(seconds=sched_duration)

            old_machine = step.machine_id
            if old_machine and old_machine != machine.machine_id:
                self._drop_reservations(old_machine, step.step_id)
            self.reservations[machine.machine_id].append(
                Reservation(start, end, step.step_id))

            step.machine_id = machine.machine_id
            step.scheduled_start = start
            step.scheduled_end = end
            if step.state != StepState.WEIGHED:
                step.state = StepState.SCHEDULED
            step.history.append({"at": self._clock().isoformat(),
                                 "type": "scheduled",
                                 "machine_id": machine.machine_id,
                                 "start": start.isoformat(),
                                 "end": end.isoformat()})
            end_cache[step.step_id] = end

            # 患者占锅锁：同批同患者在该锅连续作业，结束前他人不得使用
            if station in (DECOCT_POT, MELT_VESSEL):
                machine.occupant_patient_id = batch.patient_id
                machine.occupant_batch_id = batch.batch_id

    def _release_machine_if_done(self, machine: Machine) -> None:
        """该机器上本批步骤全部结束后释放患者占用锁。"""
        batch_id = machine.occupant_batch_id
        if not batch_id:
            return
        batch = self.batches.get(batch_id)
        if batch is None:
            machine.occupant_patient_id = None
            machine.occupant_batch_id = None
            return
        on_machine = [s for s in batch.steps.values()
                      if s.machine_id == machine.machine_id]
        if on_machine and all(s.state in TERMINAL for s in on_machine):
            machine.occupant_patient_id = None
            machine.occupant_batch_id = None

    # ------------------------------------------------------ 称量与扫码投料

    def weigh(self, step_id: str, token: str, operator_id: str) -> ChargeRecord:
        step = self._require_step(step_id)
        self._guard_batch_active(step)
        rec = self._idempotent(step, token, operator_id)
        if rec is not None:
            return rec
        if step.state not in (StepState.PENDING, StepState.SCHEDULED):
            raise OrchestrationError(
                f"{step.step_id} 状态为 {step.state.value}，不能称量")
        step.state = StepState.WEIGHED
        step.charged_token = token
        step.charged_at = self._clock()
        step.charged_by = operator_id
        rec = self._record_charge(step, token, operator_id, reason="weighed")
        step.history.append({"at": rec.at.isoformat(), "type": "weighed",
                             "operator_id": operator_id, "token": token})
        self._log("step_weighed", step_id=step_id, token=token,
                  operator_id=operator_id)
        return rec

    def charge(self, step_id: str, token: str, operator_id: str) -> ChargeRecord:
        """扫码投料：校验全部前序步骤已完成，幂等防重复投料。"""
        step = self._require_step(step_id)
        self._guard_batch_active(step)
        rec = self._idempotent(step, token, operator_id)
        if rec is not None:
            return rec
        if step.state not in (StepState.SCHEDULED, StepState.WEIGHED):
            raise OrchestrationError(
                f"{step.step_id} 已投料或状态为 {step.state.value}，"
                "扫码不能重复投料")
        for dep_id in step.depends_on:
            dep = self.steps[dep_id]
            if dep.state != StepState.COMPLETED:
                # 后下药在合煎完成前扫码会在此被拒绝，不会提前下锅
                raise OrchestrationError(
                    f"{step.step_id} 的前序 {dep_id}（{dep.technique}）"
                    "尚未完成，禁止投料")
        if step.resume_required:
            raise OrchestrationError(
                f"{step.step_id} 的药料已随故障锅迁出，应在 "
                f"{step.machine_id} 续做，不得重新扫码投料")

        step.state = StepState.IN_PROGRESS
        step.started_at = self._clock()
        step.charged_token = token
        step.charged_at = step.started_at
        step.charged_by = operator_id
        rec = self._record_charge(step, token, operator_id, reason="charged")
        step.history.append({"at": rec.at.isoformat(), "type": "charged",
                             "operator_id": operator_id, "token": token})
        self._log("step_charged", step_id=step_id, token=token,
                  operator_id=operator_id, machine_id=step.machine_id)
        return rec

    def complete_step(self, step_id: str, operator_id: str,
                      temperature_celsius: float | None = None,
                      duration_seconds: int | None = None) -> ProcessStep:
        """记录实际温度与时长并完成步骤。

        故障后续做的步骤，实际时长 = 迁移前保留时长 + 本次时长。
        只有分装步骤完成才登记成品，整批只可能有一份成品。
        """
        step = self._require_step(step_id)
        self._guard_batch_active(step)
        batch = self.batches[step.batch_id]
        if step.state != StepState.IN_PROGRESS:
            raise OrchestrationError(
                f"{step.step_id} 不在执行中（{step.state.value}），不能完成")

        temp = (temperature_celsius
                if temperature_celsius is not None
                else STANDARD_TEMPERATURE.get(step.technique))
        this_duration = (duration_seconds
                         if duration_seconds is not None
                         else step.duration_seconds - step.preserved_elapsed_seconds)
        actual_total = step.preserved_elapsed_seconds + max(0, this_duration)

        step.actual_temperature_celsius = temp
        step.actual_duration_seconds = actual_total
        step.completed_at = self._clock()
        step.state = StepState.COMPLETED
        step.history.append({
            "at": step.completed_at.isoformat(), "type": "completed",
            "operator_id": operator_id,
            "temperature_celsius": temp,
            "duration_seconds": actual_total,
            "preserved_seconds": step.preserved_elapsed_seconds,
        })
        self._log("step_completed", step_id=step_id, operator_id=operator_id,
                  temperature_celsius=temp, duration_seconds=actual_total)

        if step.technique == Technique.PACK:
            self._register_finished_good(step.batch_id)
        if step.technique == SAMPLE_RETAIN:
            good = self.finished_goods.get(step.batch_id)
            if good is not None:
                good.compliant = self._batch_compliant(batch)
                self._log("finished_good_revalidated",
                          batch_id=step.batch_id, compliant=good.compliant)

        machine = self.machines.get(step.machine_id or "")
        if machine:
            self._release_machine_if_done(machine)

        batch_done = (all(s.state in (StepState.COMPLETED, StepState.SCRAPPED,
                                      StepState.SKIPPED)
                          for s in batch.steps.values())
                      and batch.batch_id in self.finished_goods)
        if batch_done:
            batch.state = BatchState.DONE
        return step

    def _batch_compliant(self, batch: Batch) -> bool:
        return all(
            s.state in TERMINAL
            and (s.state != StepState.COMPLETED or s.label_compliant)
            for s in batch.steps.values())

    def _register_finished_good(self, batch_id: str) -> None:
        if batch_id in self.finished_goods:
            # 故障迁移重跑分装也不会生成第二份成品
            raise OrchestrationError(f"批次 {batch_id} 已登记成品，禁止重复产出")
        batch = self.batches[batch_id]
        rx = self._rx_of(batch)
        # 分装完成即产出成品；留样在成品之后，其合规结论待留样完成后复核
        compliant = all(
            s.technique == SAMPLE_RETAIN
            or (s.state in TERMINAL
                and (s.state != StepState.COMPLETED or s.label_compliant))
            for s in batch.steps.values())
        good = FinishedGood(
            batch_id=batch_id, patient_id=batch.patient_id,
            prescription_id=batch.prescription_id,
            produced_at=self._clock(), pack_count=rx.pack_count,
            compliant=compliant)
        self.finished_goods[batch_id] = good
        self._log("finished_good_registered", batch_id=batch_id,
                  compliant=compliant)

    # ------------------------------------------------------- 故障迁移后续做

    def resume_step(self, step_id: str, operator_id: str) -> ProcessStep:
        """故障迁移后的步骤在新锅续做：不重新投料，保留时长继续计时。"""
        step = self._require_step(step_id)
        self._guard_batch_active(step)
        if not step.resume_required:
            raise OrchestrationError(
                f"{step.step_id} 不是待续做步骤，无需 resume")
        if step.state != StepState.SCHEDULED:
            raise OrchestrationError(
                f"{step.step_id} 状态 {step.state.value}，无法续做")
        for dep_id in step.depends_on:
            dep = self.steps[dep_id]
            if dep.state != StepState.COMPLETED:
                raise OrchestrationError(
                    f"{step.step_id} 的前序 {dep_id} 尚未完成，不能续做")
        step.resume_required = False
        step.state = StepState.IN_PROGRESS
        step.started_at = self._clock()
        step.history.append({"at": step.started_at.isoformat(),
                             "type": "resumed",
                             "operator_id": operator_id,
                             "machine_id": step.machine_id,
                             "preserved_elapsed_seconds":
                                 step.preserved_elapsed_seconds})
        self._log("step_resumed", step_id=step_id,
                  operator_id=operator_id, machine_id=step.machine_id,
                  preserved_seconds=step.preserved_elapsed_seconds)
        return step

    # -------------------------------------------------------- 幂等与守卫

    def _require_step(self, step_id: str) -> ProcessStep:
        if step_id not in self.steps:
            raise OrchestrationError(f"未知步骤 {step_id}")
        return self.steps[step_id]

    def _guard_batch_active(self, step: ProcessStep) -> None:
        batch = self.batches[step.batch_id]
        if batch.state == BatchState.PAUSED:
            raise OrchestrationError(
                f"批次 {batch.batch_id} 因处方变更已暂停，等待药师裁决，"
                "禁止任何投料操作")
        if batch.state == BatchState.SCRAPPED:
            raise OrchestrationError(f"批次 {batch.batch_id} 已报废")

    def _idempotent(self, step: ProcessStep, token: str,
                    operator_id: str) -> ChargeRecord | None:
        """扫码重试处理。

        - 同令牌已完成称量、步骤仍处于 WEIGHED：返回 None，允许同码推进投料；
        - 同令牌已投料（执行中/已完成等）：返回原受理记录，绝不二次投料；
        - 令牌属于其他步骤：拒绝挪用。
        """
        existing = self.charge_records.get((step.step_id, token))
        if existing is not None:
            if existing.reason == "weighed" and step.state == StepState.WEIGHED:
                return None
            self._log("scan_replayed", step_id=step.step_id, token=token,
                      operator_id=operator_id,
                      original_at=existing.at.isoformat())
            return existing
        if token in self.tokens:
            stolen = self.tokens[token]
            raise OrchestrationError(
                f"令牌 {token} 已用于 {stolen.step_id}，不能挪用到 "
                f"{step.step_id}")
        return None

    def _record_charge(self, step: ProcessStep, token: str,
                       operator_id: str, *, reason: str) -> ChargeRecord:
        rec = ChargeRecord(token=token, step_id=step.step_id,
                           batch_id=step.batch_id, operator_id=operator_id,
                           at=self._clock(), accepted=True, reason=reason)
        self.charge_records[(step.step_id, token)] = rec
        self.tokens[token] = rec
        return rec

    # ------------------------------------------------------------ 处方修订

    def revise_prescription(self, rx: Prescription) -> PharmacistDecision:
        """处方新版本到达。

        - 尚无任何称量/投料的活跃批次：尚未投料，旧批步骤整体作废，
          直接按新版本重开批次。
        - 已称量或已投料的批次：整批暂停，挂起药师裁决（报废/继续），
          新版本登记留待裁决后启用。
        """
        self.prescriptions[rx.display] = rx
        prior = sorted((b for b in self.batches.values()
                        if b.prescription_id == rx.prescription_id
                        and b.version < rx.version
                        and b.state in (BatchState.ACTIVE, BatchState.PAUSED)),
                       key=lambda b: b.version, reverse=True)
        if not prior:
            # 没有旧版本活跃批次：按普通收治处理
            new_batch = self.admit_prescription(rx, owner="(unassigned)")
            return PharmacistDecision(
                batch_id=new_batch.batch_id,
                prescription_id=rx.prescription_id,
                old_version=rx.version, new_version=rx.version,
                raised_at=self._clock(), resolved_at=self._clock(),
                decision="admitted", pharmacist_id="system",
                replacement_batch_id=new_batch.batch_id)

        batch = prior[0]
        charged = [s for s in batch.steps.values() if s.is_charged]
        if not charged:
            for s in batch.steps.values():
                if s.state in (StepState.PENDING, StepState.SCHEDULED,
                               StepState.WEIGHED):
                    s.state = StepState.SKIPPED
                    s.history.append({"at": self._clock().isoformat(),
                                      "type": "superseded",
                                      "new_version": rx.version})
            batch.state = BatchState.SCRAPPED
            batch.pharmacist_decision = f"auto-superseded-by-v{rx.version}"
            for m in self.machines.values():
                self._release_machine_if_done(m)
            self._log("batch_superseded", batch_id=batch.batch_id,
                      new_version=rx.version)
            new_batch = self.admit_prescription(rx, owner="(unassigned)")
            decision = PharmacistDecision(
                batch_id=batch.batch_id,
                prescription_id=rx.prescription_id,
                old_version=batch.version, new_version=rx.version,
                raised_at=self._clock(), resolved_at=self._clock(),
                decision="replace-unstarted", pharmacist_id="system",
                replacement_batch_id=new_batch.batch_id)
            self.pharmacist_decisions.append(decision)
            return decision

        # 已有称量/投料：暂停，等待药师逐批裁决
        if batch.state != BatchState.PAUSED:
            batch.state = BatchState.PAUSED
            for s in batch.steps.values():
                if s.state in (StepState.PENDING, StepState.SCHEDULED):
                    s.state = StepState.PAUSED
            self._log("batch_paused_for_revision",
                      batch_id=batch.batch_id,
                      old_version=batch.version, new_version=rx.version)
        decision = PharmacistDecision(
            batch_id=batch.batch_id, prescription_id=rx.prescription_id,
            old_version=batch.version, new_version=rx.version,
            raised_at=self._clock())
        self.pharmacist_decisions.append(decision)
        return decision

    def pharmacist_resolve(self, batch_id: str, decision: str,
                           pharmacist_id: str) -> dict:
        """药师对暂停批次裁决：scrap（报废并按新版重开）或 continue（继续旧批）。"""
        batch = self.batches[batch_id]
        pending = next((d for d in self.pharmacist_decisions
                        if d.batch_id == batch_id and d.decision is None), None)
        if pending is None:
            raise OrchestrationError(f"批次 {batch_id} 无待裁决的处方变更")
        if decision not in ("scrap", "continue"):
            raise OrchestrationError("裁决必须是 scrap 或 continue")
        pending.resolved_at = self._clock()
        pending.pharmacist_id = pharmacist_id
        pending.decision = decision

        if decision == "continue":
            batch.pharmacist_decision = (
                f"continue-v{batch.version} by {pharmacist_id}")
            for s in batch.steps.values():
                if s.state == StepState.PAUSED:
                    s.state = StepState.SCHEDULED
            batch.state = BatchState.ACTIVE
            self._schedule_batch(batch)
            self._log("batch_resumed", batch_id=batch_id,
                      pharmacist_id=pharmacist_id)
            return {"batch_id": batch_id, "decision": "continue"}

        for s in batch.steps.values():
            if s.state not in TERMINAL:
                s.state = StepState.SCRAPPED
                s.history.append({"at": self._clock().isoformat(),
                                  "type": "scrapped",
                                  "by": pharmacist_id})
        batch.state = BatchState.SCRAPPED
        batch.pharmacist_decision = f"scrapped by {pharmacist_id}"
        for m in self.machines.values():
            if m.occupant_batch_id == batch_id:
                m.occupant_patient_id = None
                m.occupant_batch_id = None
        self._log("batch_scrapped", batch_id=batch_id,
                  pharmacist_id=pharmacist_id)

        rx = self.prescriptions.get(
            f"{batch.prescription_id}-v{pending.new_version}")
        result = {"batch_id": batch_id, "decision": "scrap"}
        if rx is not None:
            new_batch = self.admit_prescription(rx, owner="(unassigned)")
            new_batch.replacement_of = batch_id
            pending.replacement_batch_id = new_batch.batch_id
            result["replacement_batch_id"] = new_batch.batch_id
        return result

    # ------------------------------------------------------------ 设备故障

    def report_machine_fault(self, machine_id: str) -> dict:
        """设备故障：完成步骤原样保留，中断步骤携带已执行时长与温度迁出。"""
        machine = self.machines.get(machine_id)
        if machine is None:
            raise OrchestrationError(f"未知设备 {machine_id}")
        now = self._clock()
        machine.state = MachineState.FAULTED
        machine.faulted_at = now

        affected: list[str] = []
        for step in self.steps.values():
            if step.machine_id != machine_id:
                continue
            if step.state == StepState.IN_PROGRESS:
                elapsed = 0
                if step.started_at:
                    elapsed = int((now - step.started_at).total_seconds())
                budget = (step.duration_seconds
                          - step.preserved_elapsed_seconds)
                step.preserved_elapsed_seconds += max(
                    0, min(elapsed, budget))
                step.state = StepState.FAILED
                step.history.append({
                    "at": now.isoformat(), "type": "machine_fault",
                    "machine_id": machine_id,
                    "preserved_elapsed_seconds":
                        step.preserved_elapsed_seconds,
                    "temperature_celsius":
                        step.actual_temperature_celsius,
                })
                affected.append(step.step_id)
            elif step.state in (StepState.SCHEDULED, StepState.WEIGHED):
                affected.append(step.step_id)

        # 故障机的未完成预约全部作废，时间轴交还健康重排
        self.reservations[machine_id] = [
            r for r in self.reservations[machine_id]
            if self.steps[r.step_id].state == StepState.COMPLETED]
        machine.occupant_patient_id = None
        machine.occupant_batch_id = None
        self._log("machine_fault", machine_id=machine_id,
                  affected_steps=affected)
        return {"machine_id": machine_id, "affected_steps": affected}

    def repair_machine(self, machine_id: str) -> None:
        machine = self.machines[machine_id]
        machine.state = MachineState.REPAIRED
        machine.repaired_at = self._clock()
        self._log("machine_repaired", machine_id=machine_id)

    def reschedule_after_fault(self) -> dict:
        """故障后重排：完成步骤保留；未完成步骤迁移到健康设备续做。"""
        migrated: list[dict] = []
        failed_steps = sorted(
            (s for s in self.steps.values()
             if s.state == StepState.FAILED),
            key=lambda s: (s.batch_id, s.order_index))
        for step in failed_steps:
            old_machine = step.machine_id
            step.machine_id = None
            remaining = step.duration_seconds - step.preserved_elapsed_seconds
            # 已投料内容随锅迁移：续做而非重新投料，扫码投料会被拒绝
            step.resume_required = True
            self._schedule_batch(self.batches[step.batch_id],
                                 from_step_id=step.step_id)
            migrated.append({
                "step_id": step.step_id,
                "from_machine": old_machine,
                "to_machine": step.machine_id,
                "preserved_elapsed_seconds":
                    step.preserved_elapsed_seconds,
                "remaining_seconds": remaining,
            })
            step.history.append({
                "at": self._clock().isoformat(), "type": "migrated",
                "from_machine": old_machine,
                "to_machine": step.machine_id,
                "preserved_elapsed_seconds":
                    step.preserved_elapsed_seconds,
            })
            self._log("step_migrated", step_id=step.step_id,
                      from_machine=old_machine, to_machine=step.machine_id,
                      preserved_seconds=step.preserved_elapsed_seconds)

        # 仅排产在故障机上的后续步骤同样需要迁走
        for batch in self.batches.values():
            if batch.state == BatchState.SCRAPPED:
                continue
            stranded = [s for s in batch.ordered_steps()
                        if s.state in (StepState.SCHEDULED, StepState.WEIGHED)
                        and (m := self.machines.get(s.machine_id or ""))
                        and m.state == MachineState.FAULTED]
            if stranded:
                self._schedule_batch(batch,
                                     from_step_id=stranded[0].step_id)
        return {"migrated": migrated}

    # ------------------------------------------------------------ 跨班交接

    def begin_shift_handoff(self, from_operator: str,
                            to_operator: str) -> list[HandoffAcknowledgement]:
        """为每口在用/待用药锅生成逐锅确认条目。

        接班人未逐锅确认前，责任人仍是交班人，责任不落空。
        """
        existing = {(h.batch_id, h.pot_id) for h in self.handoffs
                    if not h.done}
        created: list[HandoffAcknowledgement] = []
        for batch in sorted(self.batches.values(), key=lambda b: b.batch_id):
            if batch.state == BatchState.SCRAPPED:
                continue
            pot_ids = sorted({s.machine_id for s in batch.steps.values()
                              if s.machine_id
                              and s.state not in (StepState.SKIPPED,
                                                  StepState.SCRAPPED)})
            for pot_id in pot_ids:
                key = (batch.batch_id, pot_id)
                if key in existing:
                    continue
                h = HandoffAcknowledgement(
                    batch_id=batch.batch_id, pot_id=pot_id,
                    from_operator=from_operator, to_operator=to_operator)
                self.handoffs.append(h)
                created.append(h)
        self._log("shift_handoff_begin", from_operator=from_operator,
                  to_operator=to_operator, pots=len(created))
        return created

    def acknowledge_handoff(self, batch_id: str, pot_id: str,
                            ack_by: str) -> HandoffAcknowledgement:
        h = next((x for x in self.handoffs
                  if x.batch_id == batch_id and x.pot_id == pot_id
                  and not x.done), None)
        if h is None:
            raise OrchestrationError(
                f"无待确认交接：批次 {batch_id} 锅 {pot_id}")
        if ack_by != h.to_operator:
            raise OrchestrationError(
                f"仅接班人 {h.to_operator} 可确认，{ack_by} 无权操作")
        h.acknowledged_at = self._clock()
        h.acknowledged_by = ack_by
        self._log("handoff_acknowledged", batch_id=batch_id, pot_id=pot_id,
                  by=ack_by)

        # 该批次所有锅均逐锅确认后，责任人才切换给接班人
        pending = [x for x in self.handoffs
                   if x.batch_id == batch_id and not x.done]
        if not pending:
            self.batch_owner[batch_id] = ack_by
            self._log("batch_owner_transferred", batch_id=batch_id,
                      to_operator=ack_by)
        return h

    def current_owner(self, batch_id: str) -> str:
        return self.batch_owner.get(batch_id, "(unassigned)")

    # ------------------------------------------------------------ 班次报告

    def shift_report(self) -> dict:
        now = self._clock()
        overdue: list[dict] = []
        for step in self.steps.values():
            if step.state in TERMINAL:
                continue
            risk = None
            if step.state == StepState.IN_PROGRESS and step.started_at:
                due = step.started_at + timedelta(
                    seconds=step.duration_seconds)
                if now > due:
                    risk = f"执行超时 {int((now - due).total_seconds())}s"
            elif step.scheduled_end and now > step.scheduled_end:
                risk = (f"计划已逾期 "
                        f"{int((now - step.scheduled_end).total_seconds())}s")
            if risk:
                overdue.append({"step_id": step.step_id,
                                "batch_id": step.batch_id,
                                "technique": step.technique,
                                "machine_id": step.machine_id,
                                "state": step.state.value, "risk": risk})

        process_records = []
        for batch in self.batches.values():
            for s in batch.ordered_steps():
                process_records.append({
                    "batch_id": batch.batch_id,
                    "owner": self.current_owner(batch.batch_id),
                    **s.snapshot(),
                })

        return {
            "generated_at": now.isoformat(),
            "overdue_risks": overdue,
            "paused_batches": [b.batch_id for b in self.batches.values()
                               if b.state == BatchState.PAUSED],
            "pending_pharmacist_decisions": [
                {"batch_id": d.batch_id, "old_version": d.old_version,
                 "new_version": d.new_version,
                 "raised_at": d.raised_at.isoformat()}
                for d in self.pharmacist_decisions if d.decision is None],
            "pending_handoffs": [
                {"batch_id": h.batch_id, "pot_id": h.pot_id,
                 "from_operator": h.from_operator,
                 "to_operator": h.to_operator}
                for h in self.handoffs if not h.done],
            "faulted_machines": [m.machine_id for m in self.machines.values()
                                 if m.state == MachineState.FAULTED],
            "current_owners": {b: self.current_owner(b)
                               for b in self.batches},
            "process_records": process_records,
            "finished_goods": [
                {"batch_id": g.batch_id, "patient_id": g.patient_id,
                 "prescription_id": g.prescription_id,
                 "produced_at": g.produced_at.isoformat(),
                 "pack_count": g.pack_count, "compliant": g.compliant}
                for g in self.finished_goods.values()],
        }
