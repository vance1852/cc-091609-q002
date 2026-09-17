"""读取 fixtures/decoction_shift.json。

fixture 是脱敏的最小晚班记录：处方号、药味（id/名称/工序）和当班事件。
药味与工序的语义全部以 medicines.contracts 为准；fixture 未提供的剂量、剂数
等字段使用安全默认值，可在药味对象中用 mass_grams/doses 覆盖。
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .contracts import Ingredient, Technique
from .planning import AuditedPrescription

DEFAULT_MASS_GRAMS = 10.0
DEFAULT_DOSES = 1


@dataclass(frozen=True)
class ShiftFixture:
    prescription: AuditedPrescription
    raw_events: tuple[dict, ...]
    source: str


def _parse_prescription(prescription_id: str) -> tuple[str, int]:
    """rx-204-v3 -> (rx-204, 3)；无版本号时按第 1 版处理。"""
    m = re.match(r"^(?P<base>.+?)-v(?P<version>\d+)$", prescription_id)
    if m:
        return m.group("base"), int(m.group("version"))
    return prescription_id, 1


def load_fixture(path: str | Path) -> ShiftFixture:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rx_id = data["prescription"]
    base, version = _parse_prescription(rx_id)
    patient_id = data.get("patient_id") or f"patient-{base.split('-')[-1]}"

    ingredients = tuple(
        Ingredient(
            ingredient_id=item["id"],
            prescription_version=version,
            display_name=item["name"],
            mass_grams=float(item.get("mass_grams", DEFAULT_MASS_GRAMS)),
            technique=Technique(item["technique"]),
        )
        for item in data["ingredients"]
    )
    rx = AuditedPrescription(
        prescription_id=base,
        patient_id=patient_id,
        version=version,
        doses=int(data.get("doses", DEFAULT_DOSES)),
        ingredients=ingredients,
        audit_passed=bool(data.get("audit_passed", True)),
        audit_note=data.get("audit_note", ""),
    )
    return ShiftFixture(
        prescription=rx,
        raw_events=tuple(data.get("events", ())),
        source=str(path),
    )
