# 代煎工艺编排

该项目描述处方药味、特殊煎法、设备和跨班交接。工艺事件按实际发生顺序保留，设备故障或处方修订不会删除已经完成的步骤。

`medicines/contracts.py` 提供药味、工序和设备占用结构，`fixtures/decoction_shift.json` 记录一段脱敏晚班。使用 Python 3.11，可通过 `python -m compileall medicines` 检查契约。
