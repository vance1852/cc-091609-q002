"""重放 fixtures/decoction_shift.json 的晚班场景并打印关键约束结果。

运行：python3 -m decoction.demo
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from medicines.contracts import Technique

from . import loader
from .models import SAMPLE_RETAIN
from .store import OrchestrationError

FIXTURE = Path(__file__).resolve().parent.parent / (
    "fixtures/decoction_shift.json")


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += timedelta(seconds=seconds)


def main() -> None:
    clock = Clock(datetime(2026, 9, 17, 20, 0))
    store = loader.bootstrap_fleet_store(clock)
    loaded = loader.load_fixture(FIXTURE)
    rx = loaded["prescription"]
    batch = store.admit_prescription(rx, owner="worker-a")
    sid = lambda t: f"{batch.batch_id}:{t}"

    print(f"收治 {rx.display}，生成 {len(batch.steps)} 个步骤：")
    for s in batch.ordered_steps():
        print(f"  - {s.technique:<16} {s.machine_id:<8} "
              f"依赖 {[d.split(':')[-1] for d in s.depends_on]}")

    # 1) 后下提前投料必须被拒
    try:
        store.charge(sid(Technique.ADD_LATE), "late-early", "worker-a")
        raise SystemExit("违规：后下药提前投入未被拦截")
    except OrchestrationError as exc:
        print(f"\n[约束] 后下提前投料被拒：{exc}")

    # 2) 正常做到合煎，合煎进行 10 分钟时 pot-07 故障
    for tech in (Technique.SOAK, Technique.PRE_DECOCT):
        store.charge(sid(tech), f"tok-{tech}", "worker-a")
        clock.advance(store.steps[sid(tech)].duration_seconds)
        store.complete_step(sid(tech), "worker-a")
    combined = store.steps[sid(Technique.COMBINED)]
    fault_pot = combined.machine_id
    store.charge(sid(Technique.COMBINED), "tok-combined", "worker-a")
    clock.advance(600)
    store.report_machine_fault(fault_pot)
    moved = store.reschedule_after_fault()
    m = moved["migrated"][0]
    print(f"[故障] {fault_pot} 在合煎 600s 时故障 -> 迁至 "
          f"{m['to_machine']}，保留 {m['preserved_elapsed_seconds']}s，"
          f"续做 {m['remaining_seconds']}s")

    # 3) 故障后禁止重复扫码投料，走续做
    try:
        store.charge(sid(Technique.COMBINED), "tok-combined-2", "worker-b")
        raise SystemExit("违规：故障迁移后允许了重复投料")
    except OrchestrationError as exc:
        print(f"[约束] 迁移后重复投料被拒：{exc}")
    store.resume_step(sid(Technique.COMBINED), "worker-b")
    clock.advance(combined.duration_seconds - 600)
    store.complete_step(sid(Technique.COMBINED), "worker-b")

    # 4) 后下在合煎完成之后
    late = store.steps[sid(Technique.ADD_LATE)]
    store.charge(sid(Technique.ADD_LATE), "tok-late", "worker-b")
    assert late.started_at >= combined.completed_at
    clock.advance(late.duration_seconds)
    store.complete_step(sid(Technique.ADD_LATE), "worker-b")
    print(f"[时序] 后下开始 {late.started_at:%H:%M:%S} >= "
          f"合煎完成 {combined.completed_at:%H:%M:%S}，后下未提前")

    # 5) 烊化（独立容器）-> 分装 -> 留样
    for tech in (Technique.MELT_SEPARATELY, Technique.PACK, SAMPLE_RETAIN):
        store.charge(sid(tech), f"tok-{tech}", "worker-b")
        clock.advance(store.steps[sid(tech)].duration_seconds)
        store.complete_step(sid(tech), "worker-b")

    goods = list(store.finished_goods.values())
    assert len(goods) == 1 and goods[0].compliant
    print(f"[成品] 全流程完成，成品仅 {len(goods)} 份，"
          f"标签合格 = {goods[0].compliant}")

    # 6) 跨班逐锅确认
    hs = store.begin_shift_handoff("worker-b", "worker-c")
    print(f"[交接] {len(hs)} 口锅待逐锅确认，确认前责任人仍为 "
          f"{store.current_owner(batch.batch_id)}")
    for h in hs:
        store.acknowledge_handoff(h.batch_id, h.pot_id, "worker-c")
    print(f"[交接] 全部确认后责任人 = {store.current_owner(batch.batch_id)}")

    # 7) 班次报告
    report = store.shift_report()
    print(f"[班报] 逾期 {len(report['overdue_risks'])} 项，"
          f"故障机 {report['faulted_machines']}，"
          f"工艺记录 {len(report['process_records'])} 条")


if __name__ == "__main__":
    main()
