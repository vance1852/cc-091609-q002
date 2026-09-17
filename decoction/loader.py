"""读取 fixtures/decoction_shift.json 并按 medicines/contracts.py 的
药味与工序定义还原处方、设备与晚班事件。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from medicines.contracts import Ingredient, Technique

from .models import Prescription
from .planner import DECOCT_POT, MELT_VESSEL, PACK_STATION

# 脱敏样例未给克重，编排仅依赖工序分组；默认 10g
DEFAULT_MASS_GRAMS = 10.0

_RX_VERSION_RE = re.compile(r"^(?P<rid>.+)-v(?P<version>\d+)$")

# 标准煎药中心设备队列（容量毫升）；样例故障机 pot-07 在列
DEFAULT_FLEET: tuple[tuple[str, int, str], ...] = (
    ("pot-07", 3000, DECOCT_POT),
    ("pot-08", 3000, DECOCT_POT),
    ("pot-09", 2000, DECOCT_POT),
    ("melt-01", 1000, MELT_VESSEL),
    ("melt-02", 1000, MELT_VESSEL),
    ("pack-01", 0, PACK_STATION),
)


def parse_prescription_id(raw: str) -> tuple[str, int]:
    """rx-204-v3 -> ('rx-204', 3)。"""
    m = _RX_VERSION_RE.match(raw)
    if not m:
        return raw, 1
    return m.group("rid"), int(m.group("version"))


def load_fixture(path: str | Path) -> dict:
    """解析样例文件为处方对象与事件列表（不做任何写操作）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rx_id, version = parse_prescription_id(data["prescription"])
    ingredients = tuple(
        Ingredient(
            ingredient_id=item["id"],
            prescription_version=version,
            display_name=item["name"],
            mass_grams=DEFAULT_MASS_GRAMS,
            technique=Technique(item["technique"]),
        ) for item in data["ingredients"]
    )
    rx = Prescription(
        prescription_id=rx_id,
        version=version,
        patient_id=f"pat-{rx_id}",
        ingredients=ingredients,
        pack_count=2,
        reviewed_at=datetime.now(),
    )
    return {"prescription": rx, "events": tuple(data["events"]),
            "raw_prescription": data["prescription"]}


def bootstrap_fleet(store) -> None:
    """按 DEFAULT_FLEET 登记设备。"""
    for machine_id, capacity, station in DEFAULT_FLEET:
        store.register_machine(machine_id, capacity, station)


def bootstrap_fleet_store(clock) -> "DecoctionStore":
    """构造已登记标准设备队列的 store（延迟导入避免循环依赖）。"""
    from .store import DecoctionStore

    store = DecoctionStore(clock=clock)
    bootstrap_fleet(store)
    return store


def apply_event(store, event: dict) -> dict:
    """把样例事件应用到已就绪的 store 上。

    - machine-fault：故障并立即重排，保留已完成步骤与实际温度/时长；
    - shift-handoff：生成逐锅交接确认条目。
    """
    kind = event["kind"]
    if kind == "machine-fault":
        fault = store.report_machine_fault(event["machine"])
        moved = store.reschedule_after_fault()
        return {"fault": fault, "reschedule": moved}
    if kind == "shift-handoff":
        handoffs = store.begin_shift_handoff(
            event["from"], event["to"])
        return {"handoffs": [
            {"batch_id": h.batch_id, "pot_id": h.pot_id,
             "from_operator": h.from_operator,
             "to_operator": h.to_operator} for h in handoffs]}
    raise ValueError(f"未知事件类型 {kind}")
