# Project Evolution Ledger (PEL)

面向 Coding Agent 的项目工程演进记录系统。回答一个核心问题：
**这个项目是怎样一步一步变成现在这样的？**

当前实现（Phase 01 + Phase 02 + Phase 03）：

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
- **Task 生命周期**（`project_evolution.tasks`）：start/pause/resume/complete/cancel
  状态机（终态拒绝一切生命周期调用，重复 complete 幂等）、Session 绑定与跨 Session
  接续、Checkpoint index 在 Task 内严格递增；Canonical 状态由 RawEvent Ledger
  投影重建，`.runtime/tasks/` journal 只是恢复用镜像。

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
from project_evolution.tasks import create_task_services

services = create_task_services("./my-project")

# 开始 Task（可选绑定 agent Session）
task = services.tasks.start_task("PRJ-...", "实现外部窗口透明度", agent="codex")

# 阶段 Checkpoint：index 在 Task 内严格递增
services.checkpoints.create_checkpoint(task.id, summary="核心逻辑完成", reason="milestone")

# 换一个 Session 继续（Session 结束不会自动完成 Task）
services.sessions.end_session(session_a.id)
services.sessions.bind_task(task.id, session_b.id)

# 完成（重复调用幂等，不追加事件）
services.tasks.complete_task(task.id, summary="透明度完成")

for event in services.store.replay():  # 严格按 seq 顺序
    ...
```

设计文档见 `project-evolution-ledger-docs/docs/`。
