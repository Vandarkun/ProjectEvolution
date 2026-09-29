# Project Evolution Ledger (PEL)

面向 Coding Agent 的项目工程演进记录系统。回答一个核心问题：
**这个项目是怎样一步一步变成现在这样的？**

当前实现（Phase 01 + Phase 02）：

- **领域模型库**（`project_evolution.models`）：Project / Session / Task /
  TaskSessionLink / Checkpoint / RawEvent / Evidence / EvolutionEvent /
  FeatureChange / ParameterChange / Decision / BugFix / Milestone / Version，
  与设计文档 `02-data-model.md` 严格对齐；Task 状态机按固定转换表实现。
- **统一 ID 生成器**（`project_evolution.ids`）：`new_id("TASK")` -> `TASK-<ULID>`，
  前缀 + 48bit 时间戳 + 80bit 加密随机，进程内同毫秒单调。
- **配置加载**（`project_evolution.config`）：默认配置 +
  `.project-evolution/config.yaml` 覆盖；API Key 只能引用环境变量名。
- **Raw Event Ledger**（`project_evolution.ledger`）：`raw.jsonl` 的可靠追加、
  seq 分配（项目级写锁内单调递增）、批量原子写入（失败回滚不半写）、
  按 seq 严格重放、ID 去重、尾部损坏检测与截断修复、中间损坏明确报错。

## 安装与开发

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[dev]"

.venv/Scripts/python -m pytest        # 测试（含 10k 事件规模测试）
.venv/Scripts/python -m ruff check .  # lint
.venv/Scripts/python -m mypy          # 类型检查
```

## CLI

```bash
pel --version
pel doctor [--project PATH] [--repair] [--json]
```

`pel doctor` 检查 raw ledger 完整性（JSON 可解析、ID 重复、seq 单调、尾部损坏）；
`--repair` 只截断"未确认写入"的损坏尾部，绝不静默修改中间损坏。

## 快速上手

```python
from datetime import datetime, timezone
from project_evolution.ids import new_id
from project_evolution.ledger import RawEventLedger
from project_evolution.models import RawEvent

ledger = RawEventLedger("./my-project")
stamped = ledger.append(RawEvent(
    id=new_id("RAW"),
    project_id="PRJ-...",
    timestamp=datetime.now(timezone.utc),
    kind="file_modified",
    source="cli",
    payload={"path": "Core/Service.cs"},
))
print(stamped.seq)  # seq 由 Ledger 在写锁内分配

for event in ledger.iter_events():  # 严格按 seq 顺序
    ...
```

设计文档见 `project-evolution-ledger-docs/docs/`。
