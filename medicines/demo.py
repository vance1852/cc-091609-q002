"""回放 fixtures/decoction_shift.json 的脱敏晚班场景。

运行：python -m medicines.demo

场景覆盖：
1. 后下药（薄荷）在合煎末段窗口前扫码投料 -> 拒绝；窗口到达后才接受；
2. 同一投料码重复扫码 -> 幂等回放，不二次投料；
3. pot-07 故障 -> 已完成的浸泡/先煎与合煎已煎段（温度、时长）原样保留，
   迁移到 pot-08 续煎，原药味不重复投入；
4. 跨班交接未逐锅确认前任班操作被拒，确认后放行；
5. 不同患者内容试图同锅 -> 拒绝混锅；
6. 班次结束：逾期风险、当前责任人、实际工艺记录、唯一成品编号。
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from .contracts import Ingredient, Technique
from .loader import load_fixture
from .models import Machine, RuleViolation, StepStatus
from .planning import AuditedPrescription
from .service import DecoctionBackend

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "decoction_shift.json"
T0 = datetime(2026, 9, 16, 20, 0, 0)


def _filler_rx(n: int) -> AuditedPrescription:
    """构造含先煎的他院患者批次：浸泡→先煎→合煎连续占住一口锅。"""
    return AuditedPrescription(
        prescription_id=f"rx-fill-{n}",
        patient_id=f"patient-fill-{n}",
        version=1,
        doses=1,
        ingredients=(
            Ingredient(
                ingredient_id=f"fill-{n}",
                prescription_version=1,
                display_name="龙骨",
                mass_grams=15.0,
                technique=Technique.PRE_DECOCT,
            ),
        ),
    )


def _drive_step(api, batch_id, tech, operator, start, finish, scan, temp=None):
    """称量→投料→完成的标准推进。"""
    api.scan_weigh(batch_id, tech, operator)
    api.scan_charge(batch_id, tech, operator, scan, at=start)
    api.scan_complete(batch_id, tech, operator, finish, temperature_celsius=temp)


def main() -> None:
    fixture = load_fixture(FIXTURE)
    rx = fixture.prescription

    machines = [Machine(f"pot-{i:02d}", capacity_batches=3) for i in range(1, 11)]
    machines += [Machine("bath-01", capacity_batches=2, kind="bath")]
    machines += [Machine("packer-01", capacity_batches=20, kind="packer")]
    api = DecoctionBackend(machines, T0)

    # 6 个他院患者的合煎正占着 pot-01..06（不同患者不得混锅），
    # 其合煎窗口与目标批次重合，迫使目标批次排到 pot-07。
    for n in range(6):
        api.ingest_audit_result(
            _filler_rx(n), at=T0 + timedelta(minutes=30, seconds=n)
        )
    batch = api.ingest_audit_result(rx)
    bid = batch.batch_id
    assert batch.step(Technique.COMBINED).planned_machine_id == "pot-07", (
        "目标批次应被排到 pot-07"
    )

    results: list[str] = []

    def log(msg):
        results.append(msg)
        print(msg)

    log(f"批次 {bid} 已排程（{rx.prescription_id} v{rx.version}，患者 {rx.patient_id}）")

    # 浸泡、先煎正常完成。
    _drive_step(api, bid, "soak", "worker-a", T0, T0 + timedelta(minutes=30), "scan-soak")
    _drive_step(
        api, bid, "pre-decoct", "worker-a",
        T0 + timedelta(minutes=30), T0 + timedelta(minutes=60),
        "scan-pre", temp=100.0,
    )

    # 合煎 20:00 投料。
    api.scan_weigh(bid, "combined", "worker-a")
    api.scan_charge(bid, "combined", "worker-a", "scan-combined", at=T0 + timedelta(minutes=60))

    # 混锅攻击：另一患者称量后试图此时上 pot-07 -> 必须拒绝。
    other = next(
        b for b in api.core.batches.values() if b.patient_id != batch.patient_id
    )
    api.scan_weigh(other.batch_id, "combined", "worker-a")
    try:
        api.scan_charge(other.batch_id, "combined", "worker-a", "scan-mix-attack",
                        at=T0 + timedelta(minutes=61), machine_id="pot-07")
        raise AssertionError("混锅应当被拒绝")
    except RuleViolation as e:
        log(f"[拦截] 混锅被拒绝：{e}")

    # 后下药提前投料（合煎刚开始）-> 拒绝。
    api.scan_weigh(bid, "add-late", "worker-a")
    try:
        api.scan_charge(bid, "add-late", "worker-a", "scan-mint",
                        at=T0 + timedelta(minutes=62))
        raise AssertionError("后下提前投料应当被拒绝")
    except RuleViolation as e:
        log(f"[拦截] 后下药提前投入被拒绝：{e}")

    # 21:10，合煎进行到第 10 分钟，pot-07 故障。
    fault_at = T0 + timedelta(minutes=70)
    affected = api.report_machine_fault("pot-07", fault_at, "worker-a")
    combined = api.core.batches[bid].step(Technique.COMBINED)
    preserved = combined.actual_duration_seconds
    log(f"[故障] pot-07 故障，受影响批次 {[b.batch_id for b in affected]}，"
        f"合煎已煎 {preserved}s、温度 {combined.actual_temperature_celsius}℃ 已保留，"
        f"迁移目标 {combined.planned_machine_id}")
    assert preserved == 600
    assert combined.status is StepStatus.INTERRUPTED
    # 已完成步骤不被重排：浸泡、先煎仍在原锅记录中且为 completed。
    assert api.core.batches[bid].step(Technique.SOAK).status is StepStatus.COMPLETED
    assert api.core.batches[bid].step(Technique.PRE_DECOCT).status is StepStatus.COMPLETED

    # 跨班交接：接班人未逐锅确认前任不了锅。
    migrate_target = combined.planned_machine_id
    api.begin_handoff([migrate_target], "worker-a", "worker-b")
    try:
        api.scan_resume(bid, "combined", "worker-b", fault_at + timedelta(minutes=1))
        raise AssertionError("未确认交接应当被拒绝")
    except RuleViolation as e:
        log(f"[拦截] 未逐锅确认交接即续煎被拒绝：{e}")
    api.confirm_pot(migrate_target, "worker-b")
    log(f"[交接] worker-b 已逐锅确认 {migrate_target}")

    # 续煎：迁移码扫码入新锅，不重复投药味；重复扫码幂等。
    resume_at = T0 + timedelta(minutes=71)
    api.scan_resume(bid, "combined", "worker-b", resume_at, resume_code="migrate-pot07")
    api.scan_resume(bid, "combined", "worker-b", resume_at, resume_code="migrate-pot07")
    log("[幂等] 迁移码重复扫码不重复开段，原药味不二次投入")

    # 合煎剩余 15 分钟（21:11 续煎）：后下药在末段 5 分钟窗口（21:21）才可投入。
    late_at = resume_at + timedelta(minutes=10)
    charge_late = api.scan_charge(bid, "add-late", "worker-b", "scan-mint", at=late_at)
    assert not charge_late.duplicated
    # 重复扫码：不二次投料。
    again = api.scan_charge(bid, "add-late", "worker-b", "scan-mint", at=late_at)
    assert again.duplicated
    log("[幂等] 后下药扫码重试返回既有占机，未二次投料")

    # 烊化另锅进行（21:11 投阿胶，21:21 完成，分装前兑入）。
    _drive_step(
        api, bid, "melt-separately", "worker-b",
        resume_at, resume_at + timedelta(minutes=10),
        "scan-ejiao", temp=85.0,
    )

    # 合煎、后下 21:26 完成（保留 10 分钟 + 续煎 15 分钟 = 标准 25 分钟）。
    end_boil = resume_at + timedelta(minutes=15)
    api.scan_complete(bid, "combined", "worker-b", end_boil, temperature_celsius=100.0)
    api.scan_complete(bid, "add-late", "worker-b", end_boil, temperature_celsius=95.0)

    # 分装：成品编号此时一次性生成。
    api.scan_weigh(bid, "pack", "worker-b")
    api.scan_charge(bid, "pack", "worker-b", "scan-pack", at=end_boil)
    api.scan_complete(bid, "pack", "worker-b", end_boil + timedelta(minutes=6))
    product_before = api.core.batches[bid].product_id
    # 完成重试幂等：不会再生成一份成品。
    api.scan_complete(bid, "pack", "worker-b", end_boil + timedelta(minutes=6))
    assert api.core.batches[bid].product_id == product_before
    log(f"[成品] 分装完成，唯一成品编号 {product_before}，重试未生成第二份")

    # 留样。
    api.scan_weigh(bid, "retain-sample", "worker-b")
    api.scan_charge(bid, "retain-sample", "worker-b", "scan-sample",
                    at=end_boil + timedelta(minutes=6))
    api.scan_complete(bid, "retain-sample", "worker-b",
                      end_boil + timedelta(minutes=8), temperature_celsius=25.0)

    report = api.end_shift(T0 + timedelta(minutes=90))
    batch_report = next(b for b in report["batches"] if b["batch_id"] == bid)
    log(f"[标签] 流程合格标签 = {batch_report['compliant_label']}（全部步骤完成且无工艺偏差）")
    log(f"[责任] 班末责任人 = {batch_report['responsible']}")
    assert batch_report["compliant_label"] is True
    assert batch_report["responsible"] == "worker-b"

    combined_view = next(s for s in batch_report["steps"] if s["technique"] == "combined")
    log(f"[记录] 合煎实际工艺：{json.dumps(combined_view['actual'], ensure_ascii=False)}")
    assert len(combined_view["actual"]["segments"]) == 2  # pot-07 + pot-08 两段
    assert combined_view["actual"]["duration_seconds"] == 25 * 60

    log(f"[班末] 逾期风险 {len(report['overdue_risks'])} 条，事件 "
        f"{len(report['event_log'])} 条")
    out = Path(__file__).resolve().parent.parent / "shift_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log(f"[输出] 班次报告已写入 {out}")


if __name__ == "__main__":
    main()
