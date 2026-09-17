"""处方审核结果 -> 工艺步骤编排。

生成顺序：浸泡 -> 先煎 -> 合煎 -> 后下 -> 烊化（另锅）-> 分装 -> 留样。
同患者的先煎/合煎/后下在同一口锅内顺序进行；烊化另包，占用独立烊化设备；
不同患者的内容永远不共享锅次。
"""

from __future__ import annotations

from medicines.contracts import Ingredient, Technique

from .models import (
    Batch,
    BatchState,
    Prescription,
    ProcessStep,
    SAMPLE_RETAIN,
    TECHNIQUE_ORDER,
)

# 标准工艺时长（秒）与默认温度（摄氏度）
STANDARD_DURATION: dict[str, int] = {
    Technique.SOAK: 30 * 60,
    Technique.PRE_DECOCT: 30 * 60,
    Technique.COMBINED: 25 * 60,
    Technique.ADD_LATE: 5 * 60,
    Technique.MELT_SEPARATELY: 10 * 60,
    Technique.PACK: 6 * 60,
    SAMPLE_RETAIN: 2 * 60,
}
STANDARD_TEMPERATURE: dict[str, float] = {
    Technique.SOAK: 25.0,
    Technique.PRE_DECOCT: 100.0,
    Technique.COMBINED: 100.0,
    Technique.ADD_LATE: 100.0,
    Technique.MELT_SEPARATELY: 85.0,
    Technique.PACK: 60.0,
    SAMPLE_RETAIN: 25.0,
}

# 工序使用的设备类别：同锅工序可在一台煎药机上顺序合并占机
DECOCT_POT = "decoct-pot"
MELT_VESSEL = "melt-vessel"
PACK_STATION = "pack-station"

STATION_FOR_TECHNIQUE: dict[str, str] = {
    Technique.SOAK: DECOCT_POT,
    Technique.PRE_DECOCT: DECOCT_POT,
    Technique.COMBINED: DECOCT_POT,
    Technique.ADD_LATE: DECOCT_POT,
    Technique.MELT_SEPARATELY: MELT_VESSEL,
    Technique.PACK: PACK_STATION,
    SAMPLE_RETAIN: PACK_STATION,
}

# 可合并占机（同设备类别上顺序衔接）的工序组合
COMPATIBLE_TECHNIQUES: tuple[frozenset[str], ...] = (
    frozenset({Technique.SOAK, Technique.PRE_DECOCT, Technique.COMBINED,
               Technique.ADD_LATE}),
    frozenset({Technique.PACK, SAMPLE_RETAIN}),
)


def share_machine(a: str, b: str) -> bool:
    """两道工序是否能在同一台设备上顺序合并占机。

    仅同患者同批次才会被调度到一起；烊化使用独立容器，绝不并入煎药锅。
    """
    if a == b:
        return True
    return any(a in group and b in group for group in COMPATIBLE_TECHNIQUES)


def _classify(ingredients: tuple[Ingredient, ...]) -> dict[str, list[Ingredient]]:
    groups: dict[str, list[Ingredient]] = {t: [] for t in Technique}
    for ing in ingredients:
        groups[ing.technique].append(ing)
    return groups


def build_batch(prescription: Prescription, batch_id: str,
                replacement_of: str | None = None) -> Batch:
    """根据处方版本生成完整工艺步骤。"""
    groups = _classify(prescription.ingredients)
    names = {i.ingredient_id for i in prescription.ingredients}

    steps: list[ProcessStep] = []
    completed_ids: list[str] = []

    def add(technique: str, ingredient_ids: tuple[str, ...],
            deps: tuple[str, ...]) -> str:
        sid = f"{batch_id}:{technique}"
        steps.append(ProcessStep(
            step_id=sid,
            batch_id=batch_id,
            patient_id=prescription.patient_id,
            prescription_id=prescription.prescription_id,
            prescription_version=prescription.version,
            technique=technique,
            ingredient_ids=ingredient_ids,
            order_index=TECHNIQUE_ORDER.get(technique, 6),  # type: ignore[arg-type]
            duration_seconds=STANDARD_DURATION[technique],
            depends_on=deps,
        ))
        return sid

    pot_ids = [i.ingredient_id for t in (Technique.PRE_DECOCT, Technique.COMBINED,
                                         Technique.ADD_LATE) for i in groups[t]]
    soak_ids = tuple(pot_ids)
    melt_ids = tuple(i.ingredient_id for i in groups[Technique.MELT_SEPARATELY])
    pack_inputs: list[str] = []

    # 1. 浸泡（入锅煎煮的药味，烊化药不泡）
    if soak_ids:
        soak = add(Technique.SOAK, soak_ids, ())
    else:
        soak = ""

    # 2. 先煎：每种先煎药独立步骤，但在同一口锅内先于合煎
    pre_ids: list[str] = []
    for ing in groups[Technique.PRE_DECOCT]:
        deps = (soak,) if soak else ()
        pre_ids.append(add(Technique.PRE_DECOCT, (ing.ingredient_id,), deps))

    # 3. 合煎（普通药 + 已先煎的药同锅合煎）
    combined_ids = tuple(i.ingredient_id for i in groups[Technique.COMBINED])
    combined = ""
    if combined_ids or pre_ids:
        deps = tuple(pre_ids) if pre_ids else ((soak,) if soak else ())
        combined_ingredients = combined_ids + tuple(
            i.ingredient_id for i in groups[Technique.PRE_DECOCT])
        combined = add(Technique.COMBINED, combined_ingredients, deps)
        pack_inputs.append(combined)

    # 4. 后下：必须在合煎末段才投入，硬依赖合煎步骤，调度器不会提前排产
    late_ids: list[str] = []
    for ing in groups[Technique.ADD_LATE]:
        parent = combined or soak
        late_ids.append(add(Technique.ADD_LATE, (ing.ingredient_id,),
                            (parent,) if parent else ()))
    pack_inputs.extend(late_ids)

    # 5. 另包烊化：独立烊化容器，不与煎药锅混用
    melt = ""
    if melt_ids:
        melt = add(Technique.MELT_SEPARATELY, melt_ids, ())
        pack_inputs.append(melt)

    # 6. 分装：汇合煎液与烊化液
    pack = add(Technique.PACK, tuple(sorted(names)),
               tuple(d for d in pack_inputs if d))

    # 7. 留样：分装后从成品取样，与分装可在同一分装台顺序完成
    add(SAMPLE_RETAIN, (), (pack,))

    batch = Batch(
        batch_id=batch_id,
        patient_id=prescription.patient_id,
        prescription_id=prescription.prescription_id,
        version=prescription.version,
        state=BatchState.ACTIVE,
        replacement_of=replacement_of,
    )
    batch.steps = {s.step_id: s for s in steps}
    return batch
