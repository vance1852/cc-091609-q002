"""代煎编排后端的规则测试。"""

import pytest
from datetime import datetime, timedelta

from medicines.contracts import Ingredient, Technique
from medicines.models import Machine, RuleViolation, StepStatus
from medicines.planning import AuditedPrescription
from medicines.service import DecoctionBackend

T0 = datetime(2026, 9, 16, 20, 0, 0)


def rx(version=1, patient="p1", rxid="rx-1", techniques=("pre-decoct", "add-late", "melt-separately")):
    return AuditedPrescription(
        prescription_id=rxid,
        patient_id=patient,
        version=version,
        doses=1,
        ingredients=tuple(
            Ingredient(
                ingredient_id=f"i-{t}",
                prescription_version=version,
                display_name=t,
                mass_grams=10.0,
                technique=Technique(t),
            )
            for t in techniques
        ),
    )


def machines(pots=3):
    return (
        [Machine(f"pot-{i:02d}", capacity_batches=3) for i in range(1, pots + 1)]
        + [Machine("bath-01", capacity_batches=2, kind="bath")]
        + [Machine("packer-01", capacity_batches=4, kind="packer")]
    )


def make_api(pots=3, now=T0):
    return DecoctionBackend(machines(pots), now)


def _order(api, bid, tech, op, start, finish, code, temp=None):
    api.scan_weigh(bid, tech, op)
    api.scan_charge(bid, tech, op, code, at=start)
    api.scan_complete(bid, tech, op, finish, temperature_celsius=temp)  # noqa: E501


def _full_journey(api, bid, op="op-a", start=T0):
    """标准工艺全流程，返回分装完成时刻。"""
    t = start
    _order(api, bid, "soak", op, t, t + timedelta(minutes=30), "s-soak")
    t += timedelta(minutes=30)
    _order(api, bid, "pre-decoct", op, t, t + timedelta(minutes=30), "s-pre", 100.0)
    t += timedelta(minutes=30)
    api.scan_weigh(bid, "combined", op)
    api.scan_charge(bid, "combined", op, "s-comb", at=t)
    # 烊化与合煎并行。
    _order(api, bid, "melt-separately", op, t, t + timedelta(minutes=10), "s-melt", 85.0)
    # 合煎末段 5 分钟投后下药。
    late_at = t + timedelta(minutes=20)
    api.scan_weigh(bid, "add-late", op)
    api.scan_charge(bid, "add-late", op, "s-late", at=late_at)
    end = t + timedelta(minutes=25)
    api.scan_complete(bid, "combined", op, end, temperature_celsius=100.0)
    api.scan_complete(bid, "add-late", op, end, temperature_celsius=95.0)
    api.scan_weigh(bid, "pack", op)
    api.scan_charge(bid, "pack", op, "s-pack", at=end)
    api.scan_complete(bid, "pack", op, end + timedelta(minutes=6))
    api.scan_weigh(bid, "retain-sample", op)
    api.scan_charge(bid, "retain-sample", op, "s-sample", at=end + timedelta(minutes=6))
    api.scan_complete(
        bid, "retain-sample", op, end + timedelta(minutes=8), temperature_celsius=25.0
    )
    return end


# ---------------------------------------------------------------------------


def test_steps_generated_in_required_techniques():
    api = make_api()
    batch = api.ingest_audit_result(rx())
    names = [s.technique for s in batch.steps]
    assert names == [
        Technique.SOAK,
        Technique.PRE_DECOCT,
        Technique.COMBINED,
        Technique.ADD_LATE,
        Technique.MELT_SEPARATELY,
        Technique.PACK,
        Technique.RETAIN_SAMPLE,
    ]
    # 后下占合煎末段 5 分钟窗口，二者同刻结束。
    combined = batch.step(Technique.COMBINED)
    late = batch.step(Technique.ADD_LATE)
    assert late.spec.earliest_start + timedelta(seconds=late.spec.duration_seconds) == (
        combined.spec.earliest_start + timedelta(seconds=combined.spec.duration_seconds)
    )
    # 烊化在水浴杯、煎煮在锅、分装在分装台。
    assert combined.planned_machine_id.startswith("pot-")
    assert batch.step(Technique.MELT_SEPARATELY).planned_machine_id == "bath-01"
    assert batch.step(Technique.PACK).planned_machine_id == "packer-01"


def test_add_late_cannot_be_charged_early():
    api = make_api()
    bid = api.ingest_audit_result(rx()).batch_id
    _order(api, bid, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    _order(api, bid, "pre-decoct", "op-a", T0 + timedelta(minutes=30),
           T0 + timedelta(minutes=60), "c2", 100.0)
    api.scan_weigh(bid, "combined", "op-a")
    api.scan_charge(bid, "combined", "op-a", "c3", at=T0 + timedelta(minutes=60))
    api.scan_weigh(bid, "add-late", "op-a")
    # 合煎刚开始，后下必须拒绝。
    with pytest.raises(RuleViolation, match="后下药提前投入"):
        api.scan_charge(bid, "add-late", "op-a", "c4",
                        at=T0 + timedelta(minutes=62))
    assert api.core.batches[bid].step(Technique.ADD_LATE).status is StepStatus.WEIGHED


def test_scan_retry_is_idempotent_no_double_charge():
    api = make_api()
    bid = api.ingest_audit_result(rx()).batch_id
    _order(api, bid, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    _order(api, bid, "pre-decoct", "op-a", T0 + timedelta(minutes=30),
           T0 + timedelta(minutes=60), "c2", 100.0)
    api.scan_weigh(bid, "combined", "op-a")
    first = api.scan_charge(bid, "combined", "op-a", "code-x",
                            at=T0 + timedelta(minutes=60))
    second = api.scan_charge(bid, "combined", "op-a", "code-x",
                             at=T0 + timedelta(minutes=61))
    assert first.duplicated is False
    assert second.duplicated is True
    assert second.machine_id == first.machine_id


def test_different_patients_never_share_pot():
    api = make_api(pots=2)
    b1 = api.ingest_audit_result(rx(patient="p1", rxid="rx-1"))
    b2 = api.ingest_audit_result(rx(patient="p2", rxid="rx-2"))
    # 排程层面：不同患者被分到不同锅。
    assert b1.step(Technique.SOAK).planned_machine_id != b2.step(Technique.SOAK).planned_machine_id
    api.scan_weigh(b1.batch_id, "soak", "op-a")
    api.scan_charge(b1.batch_id, "soak", "op-a", "m1", at=T0)
    # 运行层面：即使扫码时指定 b1 的锅也拒绝混锅。
    api.scan_weigh(b2.batch_id, "soak", "op-a")
    with pytest.raises(RuleViolation, match="禁止混锅"):
        api.scan_charge(b2.batch_id, "soak", "op-a", "m2", at=T0,
                        machine_id=b1.step(Technique.SOAK).planned_machine_id)


def test_scheduler_rejects_third_patient_without_free_pot():
    api = make_api(pots=2)
    api.ingest_audit_result(rx(patient="p1", rxid="rx-1"))
    api.ingest_audit_result(rx(patient="p2", rxid="rx-2"))
    # 两口锅各被一名患者的相容时间线占住，第三名患者无锅可排。
    with pytest.raises(RuntimeError, match="没有可容纳"):
        api.ingest_audit_result(rx(patient="p3", rxid="rx-3"))


def test_fault_preserves_completed_steps_and_migrates_without_second_product():
    api = make_api()
    bid = api.ingest_audit_result(rx()).batch_id
    _order(api, bid, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    _order(api, bid, "pre-decoct", "op-a", T0 + timedelta(minutes=30),
           T0 + timedelta(minutes=60), "c2", 100.0)
    api.scan_weigh(bid, "combined", "op-a")
    fault_machine = api.core.batches[bid].step(Technique.COMBINED).planned_machine_id
    api.scan_charge(bid, "combined", "op-a", "c3", at=T0 + timedelta(minutes=60))

    fault_at = T0 + timedelta(minutes=70)
    api.report_machine_fault(fault_machine, fault_at, "op-a")
    combined = api.core.batches[bid].step(Technique.COMBINED)
    assert combined.status is StepStatus.INTERRUPTED
    assert combined.actual_duration_seconds == 600  # 已煎 10 分钟保留
    assert combined.actual_temperature_celsius == 100.0
    # 已完成步骤不被重排。
    assert api.core.batches[bid].step(Technique.SOAK).status is StepStatus.COMPLETED
    assert api.core.batches[bid].step(Technique.PRE_DECOCT).status is StepStatus.COMPLETED
    new_machine = combined.planned_machine_id
    assert new_machine != fault_machine

    # 迁移续煎不需要重新投料（无投料码），已有时长继续累计。
    resume_at = fault_at + timedelta(minutes=1)
    api.scan_resume(bid, "combined", "op-a", resume_at)
    end = resume_at + timedelta(minutes=15)
    # 后下在合煎末段窗口投入，与合煎同刻完成。
    api.scan_weigh(bid, "add-late", "op-a")
    api.scan_charge(bid, "add-late", "op-a", "c6", at=end - timedelta(minutes=5))
    api.scan_complete(bid, "combined", "op-a", end, temperature_celsius=100.0)
    assert combined.actual_duration_seconds == 25 * 60  # 10 + 15
    api.scan_complete(bid, "add-late", "op-a", end, temperature_celsius=95.0)

    # 烊化、分装、留样走完。
    api.scan_weigh(bid, "melt-separately", "op-a")
    api.scan_charge(bid, "melt-separately", "op-a", "c5", at=resume_at)
    api.scan_complete(bid, "melt-separately", "op-a",
                      resume_at + timedelta(minutes=10), temperature_celsius=85.0)
    api.scan_weigh(bid, "pack", "op-a")
    api.scan_charge(bid, "pack", "op-a", "c7", at=end)
    api.scan_complete(bid, "pack", "op-a", end + timedelta(minutes=6))
    product = api.core.batches[bid].product_id
    api.scan_weigh(bid, "retain-sample", "op-a")
    api.scan_charge(bid, "retain-sample", "op-a", "c8",
                    at=end + timedelta(minutes=6))
    api.scan_complete(bid, "retain-sample", "op-a",
                      end + timedelta(minutes=8), temperature_celsius=25.0)
    # 任何重复完成都不产生第二份成品。
    api.scan_complete(bid, "pack", "op-a", end + timedelta(minutes=30))
    assert api.core.batches[bid].product_id == product


def test_revision_only_affects_uncommitted_batch():
    api = make_api()
    # A 批次：仅排程、未称量 -> 换版后被替换。
    batch_a = api.ingest_audit_result(rx(version=1, patient="p1", rxid="rx-1"))
    # B 批次：已称量 -> 换版后暂停待裁决。
    batch_b = api.ingest_audit_result(rx(version=1, patient="p2", rxid="rx-2"))
    api.scan_weigh(batch_b.batch_id, "soak", "op-a")

    result = api.revise(
        rx(version=2, patient="p1", rxid="rx-1")
    )
    # 只提了 rx-1 的新版：rx-2 不受影响。
    assert batch_a.batch_id in result["superseded"]
    assert batch_b.batch_id not in result["paused"]
    assert batch_a.step(Technique.SOAK).status is StepStatus.SUPERSEDED
    assert "v2" in result["new_batch"]


def test_revision_pauses_committed_batch_and_pharmacist_decides():
    api = make_api()
    batch = api.ingest_audit_result(rx(version=1))
    _order(api, batch.batch_id, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    api.scan_weigh(batch.batch_id, "pre-decoct", "op-a")  # 已称量、未投料
    api.revise(rx(version=2))
    pre = api.core.batches[batch.batch_id].step(Technique.PRE_DECOCT)
    assert pre.status is StepStatus.PAUSED
    # 暂停状态不能投料。
    with pytest.raises(RuleViolation, match="等待药师裁决"):
        api.scan_charge(batch.batch_id, "pre-decoct", "op-a", "x",
                        at=T0 + timedelta(minutes=31))
    # 药师裁决继续：恢复到称量态，不丢称量事实。
    api.pharmacist_decide(batch.batch_id, scrap=False, pharmacist="pharm-1")
    assert pre.status is StepStatus.WEIGHED


def test_handoff_must_be_confirmed_per_pot():
    api = make_api()
    bid = api.ingest_audit_result(rx()).batch_id
    _order(api, bid, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    _order(api, bid, "pre-decoct", "op-a", T0 + timedelta(minutes=30),
           T0 + timedelta(minutes=60), "c2", 100.0)
    api.scan_weigh(bid, "combined", "op-a")
    mid = api.core.batches[bid].step(Technique.COMBINED).planned_machine_id
    api.scan_charge(bid, "combined", "op-a", "c3", at=T0 + timedelta(minutes=60))
    api.report_machine_fault(mid, T0 + timedelta(minutes=70), "op-a")
    target = api.core.batches[bid].step(Technique.COMBINED).planned_machine_id

    api.begin_handoff([target], "op-a", "op-b")
    with pytest.raises(RuleViolation, match="交接未经逐锅确认"):
        api.scan_resume(bid, "combined", "op-b", T0 + timedelta(minutes=71))
    # 非接班人不能确认。
    with pytest.raises(RuleViolation, match="本人确认"):
        api.confirm_pot(target, "op-a")
    api.confirm_pot(target, "op-b")
    api.scan_resume(bid, "combined", "op-b", T0 + timedelta(minutes=71))


def test_compliant_batch_gets_green_label_and_report_shape():
    api = make_api()
    bid = api.ingest_audit_result(rx()).batch_id
    _full_journey(api, bid)
    report = api.end_shift(T0 + timedelta(minutes=100))
    view = next(b for b in report["batches"] if b["batch_id"] == bid)
    assert view["compliant_label"] is True
    assert view["product_id"] is not None
    assert view["responsible"] == "op-a"
    assert {s["technique"] for s in view["steps"]} >= {
        "soak", "pre-decoct", "combined", "add-late",
        "melt-separately", "pack", "retain-sample",
    }


def test_low_temperature_marks_process_deviation():
    api = make_api()
    bid = api.ingest_audit_result(rx()).batch_id
    _full_journey(api, bid)
    # 篡改一次先煎温度为偏低，验证标签逻辑：重新走一张会偏低的批次。
    api2 = make_api()
    bid2 = api2.ingest_audit_result(rx(patient="p9", rxid="rx-9")).batch_id
    _order(api2, bid2, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    _order(api2, bid2, "pre-decoct", "op-a", T0 + timedelta(minutes=30),
           T0 + timedelta(minutes=60), "c2", temp=80.0)  # 明显偏低
    view = next(
        s for s in api2.core.shift_report(T0 + timedelta(minutes=61))[0].steps
        if s["technique"] == "pre-decoct"
    )
    assert any("温度偏低" in v for v in view["violations"])


def test_overdue_report_flags_interrupted_batch():
    api = make_api()
    bid = api.ingest_audit_result(rx()).batch_id
    _order(api, bid, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    _order(api, bid, "pre-decoct", "op-a", T0 + timedelta(minutes=30),
           T0 + timedelta(minutes=60), "c2", 100.0)
    api.scan_weigh(bid, "combined", "op-a")
    mid = api.core.batches[bid].step(Technique.COMBINED).planned_machine_id
    api.scan_charge(bid, "combined", "op-a", "c3", at=T0 + timedelta(minutes=60))
    api.report_machine_fault(mid, T0 + timedelta(minutes=70), "op-a")
    risks = api.core.overdue_risks(T0 + timedelta(minutes=200))
    assert any(r.step_id.endswith("combined") for r in risks)
    risk = next(r for r in risks if r.step_id.endswith("combined"))
    assert "故障" in risk.risk


def test_interrupted_step_cannot_be_recharged_and_resume_is_idempotent():
    api = make_api()
    bid = api.ingest_audit_result(rx()).batch_id
    _order(api, bid, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    _order(api, bid, "pre-decoct", "op-a", T0 + timedelta(minutes=30),
           T0 + timedelta(minutes=60), "c2", 100.0)
    api.scan_weigh(bid, "combined", "op-a")
    mid = api.core.batches[bid].step(Technique.COMBINED).planned_machine_id
    api.scan_charge(bid, "combined", "op-a", "c3", at=T0 + timedelta(minutes=60))
    fault_at = T0 + timedelta(minutes=70)
    api.report_machine_fault(mid, fault_at, "op-a")
    step = api.core.batches[bid].step(Technique.COMBINED)
    # 中断后禁止重新投料（防二次投药味）。
    with pytest.raises(RuleViolation, match="不得重新投料"):
        api.scan_charge(bid, "combined", "op-a", "c3-again",
                        at=fault_at + timedelta(minutes=1))
    segments_before = len(step.actuals)
    api.scan_resume(bid, "combined", "op-a", fault_at + timedelta(minutes=1),
                    resume_code="mig-1")
    # 同一迁移码重试：不重复开段。
    api.scan_resume(bid, "combined", "op-a", fault_at + timedelta(minutes=2),
                    resume_code="mig-1")
    assert len(step.actuals) == segments_before
    assert step.running.started_at == fault_at + timedelta(minutes=1)


def test_unapproved_prescription_rejected():
    bad = AuditedPrescription(
        prescription_id="rx-x", patient_id="p1", version=1, doses=1,
        ingredients=(), audit_passed=False, audit_note="配伍禁忌",
    )
    api = make_api()
    with pytest.raises(ValueError, match="未通过审核"):
        api.ingest_audit_result(bad)


def test_add_late_window_after_fault_respects_preserved_time():
    api = make_api()
    bid = api.ingest_audit_result(rx()).batch_id
    _order(api, bid, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    _order(api, bid, "pre-decoct", "op-a", T0 + timedelta(minutes=30),
           T0 + timedelta(minutes=60), "c2", 100.0)
    api.scan_weigh(bid, "combined", "op-a")
    mid = api.core.batches[bid].step(Technique.COMBINED).planned_machine_id
    api.scan_charge(bid, "combined", "op-a", "c3", at=T0 + timedelta(minutes=60))
    fault_at = T0 + timedelta(minutes=70)
    api.report_machine_fault(mid, fault_at, "op-a")
    resume_at = fault_at + timedelta(minutes=1)
    api.scan_resume(bid, "combined", "op-a", resume_at)
    api.scan_weigh(bid, "add-late", "op-a")
    # 续煎 5 分钟时（已保留 10 + 5 = 15 分钟，尚余 10 分钟），后下仍被拒绝。
    with pytest.raises(RuleViolation, match="后下药提前投入"):
        api.scan_charge(bid, "add-late", "op-a", "c6",
                        at=resume_at + timedelta(minutes=5))
    # 只剩 5 分钟时才放行。
    ok = api.scan_charge(bid, "add-late", "op-a", "c6",
                         at=resume_at + timedelta(minutes=10))
    assert ok.duplicated is False


def test_incomplete_or_scrapped_batch_label_is_not_compliant():
    api = make_api()
    batch = api.ingest_audit_result(rx())
    _order(api, batch.batch_id, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    api.scan_weigh(batch.batch_id, "pre-decoct", "op-a")
    api.revise(rx(version=2))
    api.pharmacist_decide(batch.batch_id, scrap=True, pharmacist="pharm-1")
    report = api.end_shift(T0 + timedelta(minutes=120))
    view = next(b for b in report["batches"] if b["batch_id"] == batch.batch_id)
    assert view["compliant_label"] is False
    assert view["product_id"] is None
    # 暂停裁决期间责任人是值班药师。
    api2 = make_api()
    b2 = api2.ingest_audit_result(rx(version=1, patient="p2", rxid="rx-2"))
    _order(api2, b2.batch_id, "soak", "op-a", T0, T0 + timedelta(minutes=30), "c1")
    api2.scan_weigh(b2.batch_id, "pre-decoct", "op-a")
    api2.revise(rx(version=2, patient="p2", rxid="rx-2"))
    assert api2.core.responsible_for(api2.core.batches[b2.batch_id]) == "pharmacist-on-duty"
    # 新换版批次存在且未完成，同样不得挂合格标签。
    new_view = next(b for b in report["batches"] if "v2" in b["batch_id"])
    assert new_view["compliant_label"] is False
