"""代煎编排端到端规则测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from medicines.contracts import Ingredient, Technique

from decoction import loader
from decoction.models import BatchState, Prescription, SAMPLE_RETAIN, StepState
from decoction.planner import (DECOCT_POT, MELT_VESSEL, PACK_STATION,
                               build_batch, share_machine)
from decoction.store import DecoctionStore, OrchestrationError


class MutableClock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def make_rx(version: int = 3, *, rx_id="rx-204", patient="pat-204",
            ingredients=None, volume_ml=2000) -> Prescription:
    if ingredients is None:
        ingredients = (
            Ingredient("i-1", version, "附片", 10.0, Technique.PRE_DECOCT),
            Ingredient("i-2", version, "薄荷", 3.0, Technique.ADD_LATE),
            Ingredient("i-3", version, "阿胶", 6.0,
                       Technique.MELT_SEPARATELY),
        )
    return Prescription(rx_id, version, patient, tuple(ingredients),
                        pack_count=2, volume_ml=volume_ml)


def new_store(clock=None) -> DecoctionStore:
    clock = clock or MutableClock(datetime(2026, 9, 17, 20, 0))
    store = DecoctionStore(clock=clock)
    store.register_machine("pot-07", 3000, DECOCT_POT)
    store.register_machine("pot-08", 3000, DECOCT_POT)
    store.register_machine("pot-09", 1500, DECOCT_POT)
    store.register_machine("melt-01", 1000, MELT_VESSEL)
    store.register_machine("melt-02", 1000, MELT_VESSEL)
    store.register_machine("pack-01", 0, PACK_STATION)
    return store


def step(batch, technique):
    hits = [s for s in batch.steps.values() if s.technique == technique]
    assert len(hits) == 1, f"{technique} -> {hits}"
    return hits[0]


class PlanningTests(unittest.TestCase):
    def test_generates_all_seven_steps_in_order(self):
        rx = make_rx()
        batch = build_batch(rx, "b1")
        techniques = [s.technique for s in batch.ordered_steps()]
        self.assertEqual(techniques, [
            Technique.SOAK, Technique.PRE_DECOCT, Technique.COMBINED,
            Technique.ADD_LATE, Technique.MELT_SEPARATELY,
            Technique.PACK, SAMPLE_RETAIN,
        ])
        # 后下硬依赖合煎
        late = step(batch, Technique.ADD_LATE)
        self.assertEqual(late.depends_on, (step(batch, Technique.COMBINED).step_id,))
        # 烊化是独立容器，且不与煎煮工序共锅
        melt = step(batch, Technique.MELT_SEPARATELY)
        self.assertFalse(share_machine(melt.technique, Technique.COMBINED))
        # 先煎/合煎/后下可合并在同一口煎药锅
        self.assertTrue(share_machine(Technique.PRE_DECOCT, Technique.COMBINED))
        self.assertTrue(share_machine(Technique.COMBINED, Technique.ADD_LATE))
        # 分装依赖煎液与烊化液两路
        pack = step(batch, Technique.PACK)
        self.assertIn(melt.step_id, pack.depends_on)
        self.assertIn(late.step_id, pack.depends_on)

    def test_compatible_steps_share_one_pot(self):
        store = new_store()
        batch = store.admit_prescription(make_rx(), owner="worker-a")
        pot_steps = [s for s in batch.ordered_steps()
                     if s.technique in (Technique.SOAK, Technique.PRE_DECOCT,
                                        Technique.COMBINED, Technique.ADD_LATE)]
        pots = {s.machine_id for s in pot_steps}
        self.assertEqual(len(pots), 1, "同患者相容步骤应合并占同一台机")
        # 烊化在另一台设备
        self.assertNotEqual(step(batch, Technique.MELT_SEPARATELY).machine_id,
                            pots.pop())
        # 时间不重叠：后下计划开始 >= 合煎计划结束
        combined = step(batch, Technique.COMBINED)
        late = step(batch, Technique.ADD_LATE)
        self.assertGreaterEqual(late.scheduled_start, combined.scheduled_end)


class ChargeGuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 17, 20, 0))
        self.store = new_store(self.clock)
        self.batch = self.store.admit_prescription(make_rx(), owner="worker-a")

    def run_to(self, technique, token_prefix="t"):
        """把工序按依赖执行到 technique 完成。"""
        order = [Technique.SOAK, Technique.PRE_DECOCT, Technique.COMBINED,
                 Technique.ADD_LATE, Technique.MELT_SEPARATELY,
                 Technique.PACK, SAMPLE_RETAIN]
        idx = order.index(technique)
        n = 0
        for tech in order[:idx + 1]:
            n += 1
            s = step(self.batch, tech)
            self.store.charge(s.step_id, f"{token_prefix}-{n}", "worker-a")
            self.clock.advance(s.duration_seconds)
            self.store.complete_step(s.step_id, "worker-a")

    def test_add_late_cannot_be_charged_early(self):
        """样例核心事故：后下药在合煎完成前投入必须被拒绝。"""
        soak = step(self.batch, Technique.SOAK)
        self.store.charge(soak.step_id, "tok-soak", "worker-a")
        self.clock.advance(soak.duration_seconds)
        self.store.complete_step(soak.step_id, "worker-a")
        pre = step(self.batch, Technique.PRE_DECOCT)
        self.store.charge(pre.step_id, "tok-pre", "worker-a")

        late = step(self.batch, Technique.ADD_LATE)
        with self.assertRaises(OrchestrationError) as cm:
            self.store.charge(late.step_id, "tok-late", "worker-a")
        self.assertIn("尚未完成", str(cm.exception))
        self.assertEqual(late.state, StepState.SCHEDULED)
        self.assertIsNone(late.charged_token)

    def test_scan_replay_is_idempotent(self):
        soak = step(self.batch, Technique.SOAK)
        first = self.store.charge(soak.step_id, "QR-1", "worker-a")
        self.assertTrue(first.accepted)
        # 同码重扫：返回原记录，不产生第二次投料
        replay = self.store.charge(soak.step_id, "QR-1", "worker-a")
        self.assertEqual(replay.at, first.at)
        self.assertEqual(soak.history.count(
            next(h for h in soak.history if h["type"] == "charged")), 1)
        self.assertEqual(len([h for h in soak.history
                              if h["type"] == "charged"]), 1)
        # 令牌不能挪用到别的步骤
        with self.assertRaises(OrchestrationError):
            self.store.charge(step(self.batch, Technique.PRE_DECOCT).step_id,
                              "QR-1", "worker-a")

    def test_different_patients_never_share_pot(self):
        # 患者 A 已占住 pot-07（先煎进行中）
        soak_a = step(self.batch, Technique.SOAK)
        pot_a = soak_a.machine_id
        self.store.charge(soak_a.step_id, "a-1", "worker-a")

        rx_b = make_rx(rx_id="rx-300", patient="pat-300")
        batch_b = self.store.admit_prescription(rx_b, owner="worker-b")
        soak_b = step(batch_b, Technique.SOAK)
        self.assertNotEqual(soak_b.machine_id, pot_a,
                            "不同患者不得被排到同一口锅")
        # A 用锅期间，B 的任何步骤都不会落上 A 的锅
        self.assertTrue(all(
            s.machine_id != pot_a for s in batch_b.steps.values()
            if s.machine_id))

    def test_label_not_compliant_when_incomplete(self):
        soak = step(self.batch, Technique.SOAK)
        self.store.charge(soak.step_id, "tok", "worker-a")
        # 未完成时标签不得显示合格
        self.assertFalse(soak.label_compliant)
        self.clock.advance(soak.duration_seconds)
        self.store.complete_step(soak.step_id, "worker-a")
        self.assertTrue(soak.label_compliant)


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 17, 20, 0))
        self.store = new_store(self.clock)
        self.batch = self.store.admit_prescription(make_rx(3),
                                                   owner="worker-a")

    def test_revision_before_charge_auto_replaces(self):
        decision = self.store.revise_prescription(make_rx(4))
        self.assertEqual(decision.decision, "replace-unstarted")
        self.assertEqual(self.batch.state, BatchState.SCRAPPED)
        self.assertTrue(all(s.state == StepState.SKIPPED
                            for s in self.batch.steps.values()))
        new = self.store.batches[decision.replacement_batch_id]
        self.assertEqual(new.version, 4)
        self.assertEqual(new.state, BatchState.ACTIVE)

    def test_revision_after_weigh_pauses_for_pharmacist(self):
        soak = step(self.batch, Technique.SOAK)
        self.store.weigh(soak.step_id, "w-1", "worker-a")
        decision = self.store.revise_prescription(make_rx(4))
        self.assertIsNone(decision.decision)
        self.assertEqual(self.batch.state, BatchState.PAUSED)
        # 暂停期间任何投料都被拒绝
        with self.assertRaises(OrchestrationError):
            self.store.charge(soak.step_id, "w-1", "worker-a")
        # 药师裁决继续：旧版本批次恢复
        self.store.pharmacist_resolve(self.batch.batch_id, "continue",
                                      "pharmacist-li")
        self.assertEqual(self.batch.state, BatchState.ACTIVE)
        # 已称量的药料仍在，保持 weighed 可直接投料
        self.assertEqual(soak.state, StepState.WEIGHED)
        self.store.charge(soak.step_id, "w-1", "worker-a")
        self.assertEqual(soak.state, StepState.IN_PROGRESS)

    def test_revision_after_charge_scrap_opens_new_batch(self):
        soak = step(self.batch, Technique.SOAK)
        self.store.charge(soak.step_id, "tok", "worker-a")
        self.store.revise_prescription(make_rx(4))
        result = self.store.pharmacist_resolve(
            self.batch.batch_id, "scrap", "pharmacist-li")
        self.assertEqual(self.batch.state, BatchState.SCRAPPED)
        self.assertTrue(all(s.state == StepState.SCRAPPED
                            for s in self.batch.steps.values()
                            if not s.is_finished))
        new = self.store.batches[result["replacement_batch_id"]]
        self.assertEqual(new.version, 4)
        self.assertEqual(new.replacement_of, self.batch.batch_id)
        # 报废批不会产出成品
        self.assertNotIn(self.batch.batch_id, self.store.finished_goods)


class FaultMigrationTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 17, 20, 0))
        self.store = new_store(self.clock)
        self.batch = self.store.admit_prescription(make_rx(),
                                                   owner="worker-a")

    def _execute(self, tech):
        s = step(self.batch, tech)
        self.store.charge(s.step_id, f"tok-{tech}", "worker-a")
        self.clock.advance(s.duration_seconds)
        self.store.complete_step(s.step_id, "worker-a")
        return s

    def test_fault_preserves_completed_and_migrates_in_progress(self):
        soak = self._execute(Technique.SOAK)
        pre = step(self.batch, Technique.PRE_DECOCT)
        fault_pot = pre.machine_id
        self.store.charge(pre.step_id, "tok-pre", "worker-a")
        # 先煎进行 10 分钟后锅故障（标准 30 分钟）
        self.clock.advance(600)
        result = self.store.report_machine_fault(fault_pot)
        self.assertIn(pre.step_id, result["affected_steps"])
        self.assertEqual(pre.preserved_elapsed_seconds, 600)
        self.assertEqual(pre.state, StepState.FAILED)
        # 已完成的浸泡温度与时长原样保留
        self.assertEqual(soak.state, StepState.COMPLETED)
        self.assertEqual(soak.actual_duration_seconds, soak.duration_seconds)
        self.assertIsNotNone(soak.actual_temperature_celsius)

        moved = self.store.reschedule_after_fault()
        migration = next(m for m in moved["migrated"]
                         if m["step_id"] == pre.step_id)
        self.assertEqual(migration["from_machine"], fault_pot)
        self.assertNotEqual(migration["to_machine"], fault_pot)
        self.assertEqual(migration["preserved_elapsed_seconds"], 600)
        self.assertEqual(migration["remaining_seconds"],
                         pre.duration_seconds - 600)

        # 迁移后重新扫码投料必须被拒绝：药料已在锅内，只能续做。
        # 同码重放走幂等，返回原受理记录但不改变状态、不产生第二次投料；
        replay = self.store.charge(pre.step_id, "tok-pre", "worker-a")
        self.assertEqual(replay.reason, "charged")
        self.assertEqual(pre.state, StepState.SCHEDULED)
        self.assertTrue(pre.resume_required)
        # 新令牌投料被明确拒绝
        with self.assertRaises(OrchestrationError):
            self.store.charge(pre.step_id, "tok-pre-2", "worker-a")
        self.store.resume_step(pre.step_id, "worker-b")
        # 续做只需补足剩余 20 分钟，实际总时长仍为标准时长
        self.clock.advance(20 * 60)
        self.store.complete_step(pre.step_id, "worker-b")
        self.assertEqual(pre.actual_duration_seconds, pre.duration_seconds)
        self.assertEqual(pre.state, StepState.COMPLETED)

    def test_full_run_after_fault_yields_single_good(self):
        """故障迁移跑完整个工艺：成品只有一份，标签合格。"""
        for tech in (Technique.SOAK, Technique.PRE_DECOCT):
            self._execute(tech)
        combined = step(self.batch, Technique.COMBINED)
        fault_pot = combined.machine_id
        self.store.charge(combined.step_id, "tok-comb", "worker-a")
        self.clock.advance(900)
        self.store.report_machine_fault(fault_pot)
        self.store.reschedule_after_fault()
        self.store.resume_step(combined.step_id, "worker-b")
        self.clock.advance(combined.duration_seconds - 900)
        self.store.complete_step(combined.step_id, "worker-b")

        for tech in (Technique.ADD_LATE, Technique.MELT_SEPARATELY,
                     Technique.PACK, SAMPLE_RETAIN):
            self._execute(tech)

        goods = list(self.store.finished_goods.values())
        self.assertEqual(len(goods), 1, "故障迁移不得产生第二份成品")
        good = goods[0]
        self.assertTrue(good.compliant)
        self.assertEqual(self.batch.state, BatchState.DONE)
        # 后下药确实晚于合煎
        late = step(self.batch, Technique.ADD_LATE)
        comb = step(self.batch, Technique.COMBINED)
        self.assertGreaterEqual(late.started_at, comb.completed_at)

    def test_capacity_constraint_blocks_undersized_pot(self):
        # 容量 1500ml 的 pot-09 不能接需要 2000ml 的批次
        store = DecoctionStore(clock=lambda: datetime(2026, 9, 17, 20, 0))
        store.register_machine("small", 1000, DECOCT_POT)
        with self.assertRaises(OrchestrationError):
            store.admit_prescription(make_rx(volume_ml=2000), owner="a")


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 17, 23, 0))
        self.store = new_store(self.clock)
        self.batch = self.store.admit_prescription(make_rx(),
                                                   owner="worker-a")

    def test_owner_changes_only_after_all_pots_acknowledged(self):
        hs = self.store.begin_shift_handoff("worker-a", "worker-b")
        # 该批涉及三台设备（煎药锅、烊化容器、分装台），需逐锅确认
        self.assertEqual(len(hs), 3)
        self.assertEqual(self.store.current_owner(self.batch.batch_id),
                         "worker-a", "未逐锅确认前责任人不变")
        # 交班人不能替接班人确认
        with self.assertRaises(OrchestrationError):
            self.store.acknowledge_handoff(
                self.batch.batch_id, hs[0].pot_id, "worker-a")
        for h in hs:
            self.store.acknowledge_handoff(
                self.batch.batch_id, h.pot_id, "worker-b")
        self.assertEqual(self.store.current_owner(self.batch.batch_id),
                         "worker-b")


class ShiftReportTests(unittest.TestCase):
    def test_report_lists_overdue_owner_and_records(self):
        clock = MutableClock(datetime(2026, 9, 17, 23, 0))
        store = new_store(clock)
        batch = store.admit_prescription(make_rx(), owner="worker-a")
        # 浸泡按时完成；先煎开始后超时未完成
        soak = step(batch, Technique.SOAK)
        store.charge(soak.step_id, "t1", "worker-a")
        clock.advance(soak.duration_seconds)
        store.complete_step(soak.step_id, "worker-a")
        pre = step(batch, Technique.PRE_DECOCT)
        store.charge(pre.step_id, "t2", "worker-a")
        clock.advance(pre.duration_seconds + 120)

        report = store.shift_report()
        risks = {r["step_id"] for r in report["overdue_risks"]}
        self.assertIn(pre.step_id, risks)
        self.assertNotIn(soak.step_id, risks)
        self.assertEqual(report["current_owners"][batch.batch_id],
                         "worker-a")
        records = {r["step_id"]: r for r in report["process_records"]}
        self.assertEqual(records[soak.step_id]["state"], "completed")
        self.assertTrue(records[soak.step_id]["compliant"])
        self.assertEqual(records[pre.step_id]["state"], "in-progress")


class FixtureTests(unittest.TestCase):
    def test_load_sample_shift(self):
        clock = MutableClock(datetime(2026, 9, 17, 23, 0))
        store = DecoctionStore(clock=clock)
        loader.bootstrap_fleet(store)
        loaded = loader.load_fixture("fixtures/decoction_shift.json")
        rx = loaded["prescription"]
        self.assertEqual(rx.prescription_id, "rx-204")
        self.assertEqual(rx.version, 3)
        techniques = {i.ingredient_id: i.technique for i in rx.ingredients}
        self.assertEqual(techniques, {
            "i-1": Technique.PRE_DECOCT,
            "i-2": Technique.ADD_LATE,
            "i-3": Technique.MELT_SEPARATELY,
        })
        batch = store.admit_prescription(rx, owner="worker-a")
        # 执行到合煎，让 pot-07 上有在制步骤
        for tech in (Technique.SOAK, Technique.PRE_DECOCT):
            s = step(batch, tech)
            store.charge(s.step_id, f"tok-{tech}", "worker-a")
            clock.advance(s.duration_seconds)
            store.complete_step(s.step_id, "worker-a")
        combined = step(batch, Technique.COMBINED)
        self.assertEqual(combined.machine_id, "pot-07")
        store.charge(combined.step_id, "tok-comb", "worker-a")
        clock.advance(600)

        results = [loader.apply_event(store, ev) for ev in loaded["events"]]
        # 故障事件：合煎迁出 pot-07，保留 10 分钟
        fault_result = results[0]["reschedule"]["migrated"]
        self.assertEqual(fault_result[0]["from_machine"], "pot-07")
        self.assertEqual(fault_result[0]["preserved_elapsed_seconds"], 600)
        # 交接事件：生成逐锅确认
        self.assertTrue(results[1]["handoffs"])

        # 后下药仍在合煎之后，不会提前
        late = step(batch, Technique.ADD_LATE)
        self.assertIn(combined.step_id, late.depends_on)


if __name__ == "__main__":
    unittest.main()
