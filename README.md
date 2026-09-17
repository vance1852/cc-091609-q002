# 代煎工艺编排

该项目描述处方药味、特殊煎法、设备和跨班交接。工艺事件按实际发生顺序保留，设备故障或处方修订不会删除已经完成的步骤。

`medicines/contracts.py` 提供药味、工序和设备占用结构，`fixtures/decoction_shift.json` 记录一段脱敏晚班。使用 Python 3.11，仅依赖标准库，可通过 `python -m compileall medicines` 检查契约。

## 编排规则

接收处方审核结果后，按 **处方版本 + 设备容量 + 实际操作** 共同约束生成七步：

| 顺序 | 工序 | 设备 | 约束 |
| --- | --- | --- | --- |
| 1 | soak 浸泡 | 煎药锅 | 入锅药味同泡，烊化药不泡 |
| 2 | pre-decoct 先煎 | 同一煎药锅 | 先于合煎 |
| 3 | combined 合煎 | 同一煎药锅 | 先煎药同锅合煎 |
| 4 | add-late 后下 | 同一煎药锅 | **硬依赖合煎完成，提前扫码一律拒绝** |
| 5 | melt-separately 烊化 | 独立烊化容器 | 另包，不与煎药锅混用 |
| 6 | pack 分装 | 分装台 | 汇合煎液与烊化液，此时才登记成品 |
| 7 | sample-retain 留样 | 分装台 | 分装后取样，复核成品标签合规性 |

- 同患者相容步骤合并占一台机（浸泡/先煎/合煎/后下连排）；**不同患者内容不得混锅**，设备有患者占用锁。
- 排产同时受设备容量（`volume_ml <= capacity_ml`）与时间轴约束。
- 扫码投料按 `(step_id, token)` 幂等：重扫返回原记录，**绝不产生第二次投料**，令牌不得跨步骤挪用。
- 只有实际温度/时长记录完整、达到标准时长的步骤，标签才能标记 `compliant: true`。

## 处方变更

- 新版本到达时**尚未称量/投料**的批次：旧步骤整体 `skipped`，自动按新版本重开批次。
- **已称量或已投入**的批次：整批 `paused`，禁止一切投料，挂起药师裁决：
  - `continue`：按旧版本继续（已称量药料保持 weighed）；
  - `scrap`：在制品报废、不产成品，并按新版本另开批次（`replacement_of` 指向旧批）。

## 设备故障

- 在制步骤记录已执行时长与温度后置为 failed；**已完成步骤永不参与重排**，温度与时长原样保留。
- 重排只把未完成步骤（按剩余时长）迁到满足容量与患者隔离的健康设备。
- 已投料内容随锅迁移：扫码重新投料被拒，必须显式 `resume` 续做；完成时实际时长 = 保留时长 + 续做时长。
- 每批最多登记一份成品，故障迁移**不会产生第二份成品**。

## 跨班交接

交班按每口在用锅（含故障锅）生成逐锅确认条目；接班人逐锅扫码确认前，责任人始终是交班人；全部确认后责任人切换。

## 使用

```bash
python3 -m decoction.demo                       # 命令行重放样例晚班（含断言）
python3 -m decoction.api --port 8080            # 启动 HTTP 后端
python3 -m unittest discover -s tests           # 15 项规则测试
```

启动后可一键载入样例：`POST /fixtures/load`（自动应用 pot-07 故障与 worker-a→worker-b 交接事件）。

### HTTP 接口

| 方法与路径 | 作用 |
| --- | --- |
| `POST /prescriptions` | 接收处方审核结果，生成步骤并排产 |
| `POST /prescriptions/revise` | 处方版本变更（未投料自动重开 / 已投料暂停待裁） |
| `POST /decisions` | 药师裁决 `scrap` / `continue` |
| `GET /batches`、`GET /batches/{id}` | 批次与步骤（含计划/实际时间、温度、合规标记） |
| `POST /steps/{id}/weigh` | 扫码称量 |
| `POST /steps/{id}/charge` | 扫码投料（校验前序依赖，幂等防重投） |
| `POST /steps/{id}/resume` | 故障迁移后续做（不重新投料） |
| `POST /steps/{id}/complete` | 记录实际温度/时长并完成 |
| `POST /machines/register` | 登记设备（容量、工位） |
| `POST /machines/{id}/fault`、`/repair` | 设备故障 / 修复 |
| `POST /reschedule` | 故障后重排 |
| `POST /handoffs`、`POST /handoffs/ack` | 发起 / 逐锅确认跨班交接 |
| `GET /report/shift` | 班次结束：逾期风险、责任人、实际工艺记录、成品 |
| `GET /audit` | 完整审计事件流 |

业务规则冲突（后下提前、重复投料、混锅、暂停中操作、容量不足等）统一返回 `409`。

## 代码结构

- `medicines/contracts.py`：药味、工序、设备事件契约（未改动）
- `decoction/models.py`：处方/批次/步骤/设备/交接/成品领域模型
- `decoction/planner.py`：处方 → 七步工艺与相容合并规则
- `decoction/store.py`：调度排产、幂等投料、处方修订、故障迁移、交接、班报
- `decoction/loader.py`：读取 `fixtures/decoction_shift.json` 与设备队列
- `decoction/api.py`：标准库 HTTP 后端
- `decoction/demo.py`：样例晚班端到端重放
