"""Runtime Task Journal：``.runtime/tasks/<task_id>/state.json``。

只是方便恢复的临时状态镜像（phase-03 §9）：

- Canonical 生命周期始终由 RawEvent Ledger 重建；
- journal 丢失/损坏只降级为"少一个镜像"，不丢失任何 Task 历史；
- 读取失败静默返回 None 并记 warning，调用方回退 Ledger 投影。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from project_evolution.tasks.state import TaskRuntimeState

_LOG = logging.getLogger(__name__)


class TaskJournal:
    """按 task_id 读写运行时状态镜像，原子替换写入。"""

    def __init__(self, tasks_dir: str | Path):
        self._tasks_dir = Path(tasks_dir)

    def path_for(self, task_id: str) -> Path:
        return self._tasks_dir / task_id / "state.json"

    def write(self, state: TaskRuntimeState) -> None:
        path = self.path_for(state.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("state.json.tmp")
        tmp.write_text(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def read(self, task_id: str) -> TaskRuntimeState | None:
        path = self.path_for(task_id)
        if not path.is_file():
            return None
        try:
            return TaskRuntimeState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, PydanticValidationError) as exc:
            _LOG.warning(
                "runtime journal for task %s is unreadable (%s: %s); falling back to ledger",
                task_id,
                type(exc).__name__,
                exc,
            )
            return None

    def delete(self, task_id: str) -> None:
        """删除某 Task 的 journal（测试与诊断用；Ledger 不受影响）。"""
        task_dir = self.path_for(task_id).parent
        if task_dir.is_dir():
            shutil.rmtree(task_dir, ignore_errors=True)
