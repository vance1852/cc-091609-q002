# 代煎工艺编排与交接后端

医院代煎中心的编排后端：接收处方审核结果，生成 **浸泡 → 先煎 → 合煎 → 后下 →
（另包烊化）→ 分装 → 留样** 工艺步骤，并把每一步同时约束在

1. **处方版本**（步骤来自审核后的具体版本，换版只影响未承诺物料）；
2. **设备容量与相容性**（同患者相容步骤合并占机，不同患者绝不混锅）；
3. **实际扫码操作**（称量码、投料码、迁移码、完成上报的真实温度/时长/操作人）

之上。口头提醒无法约束几百口并行锅，所有规则都在扫码入口处强制执行。

## 核心规则

- **后下药不会提前**：后下占合煎末段 5 分钟窗口（同锅相容合并），合煎未进入
  末段窗口时投料扫码直接被拒；故障续煎时把已煎时长计入窗口判断。
- **烊化另包另器**：阿胶类在水浴杯（`bath`）中烊化，与合煎并行、分装前完成，
  绝不与汤剂共锅。
- **不混锅**：煎药锅按“同患者 + 工序相容 + 容量”合并；不同患者内容共锅在
  排程和运行两个层面都会被拒绝。水浴杯/分装台内是独立容器，按容量共享。
- **扫码幂等**：同一投料码重复上报只回放既有占机，不二次投料；故障续煎使用
  独立的锅次迁移码，原药味不重投；完成重试不生成第二份成品。
- **故障保留事实**：设备故障时在锅步骤的已煎温度/时长固化为工艺段，迁移到健康
  锅只补足剩余时长；已完成步骤、已产出成品原样保留。
- **处方变更分级处理**：未称量/未投料的批次直接按新版重建（旧步骤 `superseded`
  并释放占机）；已称量或已投入的批次**整批暂停**，由药师裁决报废或按旧版继续，
  系统不擅自重排已承诺物料。
- **跨班逐锅确认**：交班为每口有在制内容的锅生成待确认记录，接班人未逐锅确认
  前，该锅的投料/续煎一律拒绝。
- **标签如实**：只有全部步骤完成、无工艺偏差（时长不足/温度偏低）且成品唯一时，
  标签才允许显示“流程合格”；未完成、暂停、报废批次不可能拿到合格标签。

## 模块

| 文件 | 职责 |
| --- | --- |
| `medicines/contracts.py` | 对外不可变契约：`Technique`、`Ingredient`、`ProcessStep`、`MachineEvent` |
| `medicines/models.py` | 运行态模型：步骤状态机、批次、设备、实际工艺段、交接记录 |
| `medicines/planning.py` | 步骤生成（先后/后下/烊化时间线）与贪心占机排程 |
| `medicines/orchestrator.py` | 核心规则：扫码门禁、故障迁移、换版裁决、交接、班次报告 |
| `medicines/loader.py` | 按契约读取 `fixtures/decoction_shift.json` |
| `medicines/service.py` | 面向锅旁终端的接口层（扫码动作 + 班末汇总） |
| `medicines/demo.py` | 回放脱敏晚班场景（后下拦截、pot-07 故障迁移、跨班确认） |
| `tests/test_orchestration.py` | 16 条规则测试 |

## 运行

使用 Python 3.11：

```bash
python3 -m compileall medicines        # 契约自检
python3 -m medicines.demo              # 回放 fixtures 场景，写出 shift_report.json
python3 -m pytest tests/ -q            # 规则测试
```

`DecoctionBackend` 的典型用法：

```python
from datetime import datetime
from medicines.loader import load_fixture
from medicines.models import Machine
from medicines.service import DecoctionBackend

fx = load_fixture("fixtures/decoction_shift.json")
api = DecoctionBackend(
    [Machine(f"pot-{i:02d}") for i in range(1, 11)]
    + [Machine("bath-01", kind="bath"), Machine("packer-01", kind="packer")],
    datetime(2026, 9, 16, 20, 0),
)
batch = api.ingest_audit_result(fx.prescription)

api.scan_weigh(batch.batch_id, "soak", "worker-a")
api.scan_charge(batch.batch_id, "soak", "worker-a", "scan-soak")
api.scan_complete(batch.batch_id, "soak", "worker-a", datetime(...))
# ... 先煎、合煎；后下在合煎末段窗口扫码，提前会抛 RuleViolation

report = api.end_shift(datetime(2026, 9, 16, 23, 0))
# report: overdue_risks（逾期风险）/ batches（责任人、标签、分段实际工艺）/ event_log
```

工艺事件按实际发生顺序追加保留，设备故障或处方修订都不会删除已经完成的步骤、
温度时长记录或成品编号。
