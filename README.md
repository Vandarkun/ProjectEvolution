# Project Evolution Ledger (PEL)

面向 Coding Agent 的项目工程演进记录系统。回答一个核心问题：
**这个项目是怎样一步一步变成现在这样的？**

当前实现（Phase 01 ~ Phase 04）：

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
- **Git 变化分析**（`project_evolution.git`）：只读 GitAdapter（子命令白名单 +
  超时 + 参数数组，`.project-evolution/` 从一切采集永久排除）、逐文件内容快照 +
  sha256 blob 去重、快照间增量 diff（不做 patch 相减）、rename 识别（hash 精确 /
  内容相似度 + find-renames 交叉验证）、diff evidence（patch 落
  `evidence/diffs/`，secret 只记 path+hash 不落内容）、non-Git 明确降级。
- **ChangeDigest**（`project_evolution.evolution`）：发往模型的确定性变更摘要
  （changed/added/deleted/renamed、diff_stat、预算内 hunk 选取、truncated），
  privacy filter 硬编码排除 secret 与 PEL 自身目录，generated/binary/lock
  可追溯地记入 excluded。

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
from project_evolution.git import (
    build_digest_for_checkpoint,
    create_git_aware_task_services,
)

# Task/Checkpoint 服务 + Git 采集（non-Git 目录自动降级）
services = create_git_aware_task_services("./my-project", project_id="PRJ-...")

# 开始 Task：记录 HEAD/branch/status + 工作区快照（Task 前的 Dirty Tree 进基线）
task = services.tasks.start_task("PRJ-...", "实现外部窗口透明度", agent="codex")

# ... 编码 ...

# 阶段 Checkpoint：对比上一快照逐文件增量 -> diff evidence + changed-file 事件
cp = services.checkpoints.create_checkpoint(task.id, summary="核心逻辑完成")

# 重建该 Checkpoint 的 ChangeDigest（Phase 05 的模型输入；secret 已过滤）
digest = build_digest_for_checkpoint(
    "./my-project", project_id="PRJ-...", task_id=task.id, checkpoint_id=cp.id
)

services.tasks.complete_task(task.id, summary="透明度完成")

for event in services.store.replay():  # 严格按 seq 顺序
    ...
```

设计文档见 `project-evolution-ledger-docs/docs/`。
