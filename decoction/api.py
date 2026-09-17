"""代煎编排与交接 HTTP 后端（仅依赖标准库）。

路由：
  POST /prescriptions                 接收处方审核结果，生成工艺步骤并排产
  POST /prescriptions/revise          处方版本变更（未投料重排 / 已投料暂停）
  POST /decisions                     药师对暂停批次裁决 scrap | continue
  GET  /batches                       批次与步骤视图
  GET  /batches/{batch_id}
  POST /steps/{step_id}/weigh         扫码称量
  POST /steps/{step_id}/charge        扫码投料（幂等，校验前序依赖）
  POST /steps/{step_id}/complete      记录实际温度/时长并完成
  POST /machines/register             登记设备 {machine_id, capacity_ml, station}
  POST /machines/{machine_id}/fault   设备故障
  POST /machines/{machine_id}/repair  设备修复
  POST /reschedule                    故障后重排
  POST /handoffs                      发起跨班逐锅交接
  POST /handoffs/ack                  逐锅确认交接
  GET  /report/shift                  班次结束报告（逾期/责任人/工艺记录/成品）
  POST /fixtures/load                 载入 fixtures/decoction_shift.json
  GET  /audit                         审计事件流
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from medicines.contracts import Ingredient, Technique

from . import loader as fixture_loader
from .models import BatchState, Prescription
from .store import DecoctionStore, OrchestrationError

DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent / (
    "fixtures/decoction_shift.json")


class ApiState:
    def __init__(self):
        self.lock = threading.RLock()
        self.store = DecoctionStore(clock=datetime.now)
        fixture_loader.bootstrap_fleet(self.store)


STATE = ApiState()


def _prescription_from_payload(p: dict) -> Prescription:
    version = int(p["version"])
    ingredients = tuple(
        Ingredient(
            ingredient_id=str(i["id"]),
            prescription_version=version,
            display_name=str(i.get("name", i["id"])),
            mass_grams=float(i.get("mass_grams", 10.0)),
            technique=Technique(i["technique"]),
        ) for i in p.get("ingredients", [])
    )
    return Prescription(
        prescription_id=str(p["prescription_id"]),
        version=version,
        patient_id=str(p["patient_id"]),
        ingredients=ingredients,
        pack_count=int(p.get("pack_count", 1)),
        volume_ml=int(p.get("volume_ml", 2000)),
        reviewed_at=datetime.now(),
    )


def _batch_view(store: DecoctionStore, batch) -> dict:
    return {
        "batch_id": batch.batch_id,
        "patient_id": batch.patient_id,
        "prescription_id": batch.prescription_id,
        "version": batch.version,
        "state": batch.state.value,
        "owner": store.current_owner(batch.batch_id),
        "replacement_of": batch.replacement_of,
        "pharmacist_decision": batch.pharmacist_decision,
        "steps": [s.snapshot() | {
            "scheduled_start": s.scheduled_start.isoformat()
            if s.scheduled_start else None,
            "scheduled_end": s.scheduled_end.isoformat()
            if s.scheduled_end else None,
            "started_at": s.started_at.isoformat()
            if s.started_at else None,
            "completed_at": s.completed_at.isoformat()
            if s.completed_at else None,
            "depends_on": list(s.depends_on),
        } for s in batch.ordered_steps()],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "DecoctionOrchestrator/1.0"

    # -------------------------------------------------------------- 工具方法

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt, *args):  # 静音默认访问日志
        return

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        store = STATE.store
        try:
            with STATE.lock:
                if path == "/batches":
                    self._send(200, [_batch_view(store, b)
                                     for b in store.batches.values()])
                    return
                if path.startswith("/batches/"):
                    batch_id = path.split("/", 2)[2]
                    batch = store.batches.get(batch_id)
                    if batch is None:
                        self._send(404, {"error": "批次不存在"})
                        return
                    self._send(200, _batch_view(store, batch))
                    return
                if path == "/report/shift":
                    self._send(200, store.shift_report())
                    return
                if path == "/audit":
                    self._send(200, {"events": store.audit_log})
                    return
                if path == "/machines":
                    self._send(200, [
                        {"machine_id": m.machine_id,
                         "capacity_ml": m.capacity_ml,
                         "station": m.station,
                         "state": m.state.value,
                         "occupant_patient_id": m.occupant_patient_id,
                         "occupant_batch_id": m.occupant_batch_id}
                        for m in store.machines.values()])
                    return
                self._send(404, {"error": f"未知路由 {path}"})
        except OrchestrationError as exc:
            self._send(409, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    # ----------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        store = STATE.store
        try:
            payload = self._read_json()
            with STATE.lock:
                self._route_post(path, payload)
        except OrchestrationError as exc:
            # 409：业务规则冲突（后下提前、重复投料、混锅、暂停中操作…）
            self._send(409, {"error": str(exc)})
        except (KeyError, ValueError) as exc:
            self._send(400, {"error": f"请求不合法：{exc}"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route_post(self, path: str, payload: dict) -> None:
        store = STATE.store

        if path == "/prescriptions":
            rx = _prescription_from_payload(payload)
            batch = store.admit_prescription(
                rx, owner=str(payload.get("owner", "(unassigned)")))
            self._send(201, _batch_view(store, batch))
            return

        if path == "/prescriptions/revise":
            rx = _prescription_from_payload(payload)
            try:
                decision = store.revise_prescription(rx)
                self._send(200, decision.__dict__ | {
                    "raised_at": decision.raised_at.isoformat(),
                    "resolved_at": decision.resolved_at.isoformat()
                    if decision.resolved_at else None})
            except OrchestrationError:
                raise
            return

        if path == "/decisions":
            result = store.pharmacist_resolve(
                batch_id=payload["batch_id"],
                decision=payload["decision"],
                pharmacist_id=payload["pharmacist_id"])
            self._send(200, result)
            return

        if path.startswith("/steps/"):
            rest = path[len("/steps/"):]
            step_id, action = rest.split("/", 1)
            if action == "weigh":
                rec = store.weigh(step_id, token=payload["token"],
                                  operator_id=payload["operator_id"])
            elif action == "charge":
                rec = store.charge(step_id, token=payload["token"],
                                   operator_id=payload["operator_id"])
            elif action == "complete":
                step = store.complete_step(
                    step_id,
                    operator_id=payload["operator_id"],
                    temperature_celsius=payload.get("temperature_celsius"),
                    duration_seconds=payload.get("duration_seconds"))
                self._send(200, step.snapshot())
                return
            elif action == "resume":
                step = store.resume_step(
                    step_id, operator_id=payload["operator_id"])
                self._send(200, step.snapshot())
                return
            else:
                self._send(404, {"error": f"未知操作 {action}"})
                return
            self._send(200, {"token": rec.token, "step_id": rec.step_id,
                             "accepted": rec.accepted, "reason": rec.reason,
                             "at": rec.at.isoformat()})
            return

        if path == "/machines/register":
            m = store.register_machine(
                payload["machine_id"], int(payload["capacity_ml"]),
                payload["station"])
            self._send(201, {"machine_id": m.machine_id,
                             "capacity_ml": m.capacity_ml,
                             "station": m.station})
            return

        if path.startswith("/machines/"):
            machine_id = path.split("/", 2)[2]
            if machine_id.endswith("/fault"):
                result = store.report_machine_fault(machine_id[:-6])
                self._send(200, result)
                return
            if machine_id.endswith("/repair"):
                store.repair_machine(machine_id[:-7])
                self._send(200, {"machine_id": machine_id[:-7],
                                 "state": "repaired"})
                return
            self._send(404, {"error": f"未知机器操作 {path}"})
            return

        if path == "/reschedule":
            self._send(200, store.reschedule_after_fault())
            return

        if path == "/handoffs":
            handoffs = store.begin_shift_handoff(
                payload["from_operator"], payload["to_operator"])
            self._send(201, [
                {"batch_id": h.batch_id, "pot_id": h.pot_id,
                 "from_operator": h.from_operator,
                 "to_operator": h.to_operator,
                 "acknowledged": h.done} for h in handoffs])
            return

        if path == "/handoffs/ack":
            h = store.acknowledge_handoff(
                payload["batch_id"], payload["pot_id"],
                payload["ack_by"])
            self._send(200, {"batch_id": h.batch_id, "pot_id": h.pot_id,
                             "acknowledged_at": h.acknowledged_at.isoformat(),
                             "acknowledged_by": h.acknowledged_by,
                             "owner": store.current_owner(h.batch_id)})
            return

        if path == "/fixtures/load":
            target = Path(payload.get("path", str(DEFAULT_FIXTURE)))
            loaded = fixture_loader.load_fixture(target)
            rx = loaded["prescription"]
            owner = payload.get("owner", "worker-a")
            batch = store.admit_prescription(rx, owner=owner)
            results = [fixture_loader.apply_event(store, ev)
                       for ev in loaded["events"]]
            self._send(201, {
                "fixture": str(target),
                "prescription": rx.display,
                "patient_id": rx.patient_id,
                "batch_id": batch.batch_id,
                "event_results": results,
            })
            return

        self._send(404, {"error": f"未知路由 {path}"})


def create_server(host: str = "127.0.0.1", port: int = 8080
                  ) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="代煎编排与交接后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--load-fixture", default=None,
                        help="启动时载入指定 fixture JSON")
    args = parser.parse_args()

    if args.load_fixture:
        loaded = fixture_loader.load_fixture(args.load_fixture)
        rx = loaded["prescription"]
        batch = STATE.store.admit_prescription(rx, owner="worker-a")
        for ev in loaded["events"]:
            fixture_loader.apply_event(STATE.store, ev)
        print(f"已载入 {args.load_fixture} -> 批次 {batch.batch_id}")

    httpd = create_server(args.host, args.port)
    print(f"代煎编排后端运行于 http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


if __name__ == "__main__":
    main()
