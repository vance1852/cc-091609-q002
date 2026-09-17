"""代煎编排与交接后端的对外接口层。

所有现场动作都以“扫码上报”的形式进入后端：称量码、投料码、完成码、
锅次迁移码。后端根据处方版本、设备容量与实际操作共同决定接受或拒绝，
拒绝原因可直接回显到锅旁终端。
"""

from dataclasses import asdict
from datetime import datetime

from .contracts import Technique
from .models import Machine, RuleViolation
from .orchestrator import DecoctionOrchestrator
from .planning import AuditedPrescription


class DecoctionBackend:
    def __init__(self, machines: list[Machine], now: datetime):
        self.core = DecoctionOrchestrator(machines, now)
        self.now = now

    # -- 处方审核结果接入 -------------------------------------------------

    def ingest_audit_result(self, rx: AuditedPrescription, at: datetime | None = None):
        return self.core.admit(rx, at)

    def revise(self, rx: AuditedPrescription):
        return self.core.revise_prescription(rx)

    # -- 现场扫码操作 -----------------------------------------------------

    def scan_weigh(self, batch_id: str, technique: str, operator: str):
        return self.core.weigh(batch_id, Technique(technique), operator)

    def scan_charge(
        self,
        batch_id: str,
        technique: str,
        operator: str,
        scan_code: str,
        at: datetime | None = None,
        machine_id: str | None = None,
    ):
        return self.core.charge(
            batch_id, Technique(technique), operator, scan_code, machine_id, at or self.now
        )

    def scan_complete(
        self,
        batch_id: str,
        technique: str,
        operator: str,
        at: datetime,
        temperature_celsius: float | None = None,
        duration_seconds: int | None = None,
    ):
        return self.core.complete(
            batch_id,
            Technique(technique),
            operator,
            at,
            temperature_celsius,
            duration_seconds,
        )

    def report_machine_fault(self, machine_id: str, at: datetime, operator: str):
        return self.core.machine_fault(machine_id, at, operator)

    def scan_resume(
        self, batch_id: str, technique: str, operator: str, at: datetime,
        resume_code: str | None = None,
    ):
        return self.core.resume_interrupted(
            batch_id, Technique(technique), operator, at, resume_code
        )

    # -- 药师裁决 ---------------------------------------------------------

    def pharmacist_decide(self, batch_id: str, scrap: bool, pharmacist: str):
        return self.core.pharmacist_decision(batch_id, scrap, pharmacist)

    # -- 跨班交接 ---------------------------------------------------------

    def begin_handoff(self, machine_ids: list[str], from_op: str, to_op: str):
        return self.core.open_handoff(machine_ids, from_op, to_op)

    def confirm_pot(self, machine_id: str, to_operator: str):
        return self.core.confirm_handoff(machine_id, to_operator)

    # -- 班次结束 ---------------------------------------------------------

    def end_shift(self, now: datetime) -> dict:
        self.now = now
        reports = [asdict(r) for r in self.core.shift_report(now)]
        risks = [asdict(r) for r in self.core.overdue_risks(now)]
        for r in risks:
            r["planned_finish"] = r["planned_finish"].isoformat()
        return {
            "generated_at": now.isoformat(),
            "overdue_risks": risks,
            "batches": reports,
            "event_log": [
                {
                    "event_id": e.event_id,
                    "machine_id": e.machine_id,
                    "step_id": e.step_id,
                    "at": e.occurred_at.isoformat(),
                    "operator": e.operator_id,
                    "temperature_celsius": e.temperature_celsius,
                    "scan_code": e.scan_code,
                }
                for e in self.core.events
            ],
        }

    @staticmethod
    def reject_message(exc: RuleViolation) -> dict:
        return {"accepted": False, "reason": str(exc)}
