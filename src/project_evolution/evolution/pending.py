"""Pending Analysis 队列（phase-05 §9，05-evolution-analyzer.md §16）。

模型 timeout / API failure 时：RawEvent / Evidence 已提交、语义分析未完成。
失败现场保存到::

    .project-evolution/.runtime/pending/<analysis_id>.json

后续 ``retry_pending()`` 重试；Raw Ledger 不回滚。
Coding Agent 的当前任务不因 Analyzer 失败而判定开发失败。
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from project_evolution.config import PEL_DIR_NAME

_LOG = logging.getLogger(__name__)


class PendingAnalysis(BaseModel):
    """一次失败分析的待重试记录。"""

    analysis_id: str  # ANL-...
    analysis_key: str
    task_id: str
    checkpoint_id: str
    provider: str
    attempts: int = 0
    last_error: str = ""
    last_error_kind: str = "error"  # timeout / api / network / invalid_output / error
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PendingStore:
    """.runtime/pending/ 目录的读写（文件名即 analysis_id）。"""

    def __init__(self, project_root: str | Path, *, runtime_dir: str = ".runtime"):
        self._dir = Path(project_root) / PEL_DIR_NAME / runtime_dir / "pending"

    def dir(self) -> Path:
        return self._dir

    def save(self, record: PendingAnalysis) -> None:
        record = record.model_copy(
            update={
                "attempts": record.attempts + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{record.analysis_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def get(self, analysis_id: str) -> PendingAnalysis | None:
        return self._load(self._dir / f"{analysis_id}.json")

    def find_by_key(self, analysis_key: str) -> PendingAnalysis | None:
        """按 analysis_key 查找待重试记录（重试失败应更新而非新增）。"""
        for record in self.list_pending():
            if record.analysis_key == analysis_key:
                return record
        return None

    def list_pending(self) -> list[PendingAnalysis]:
        if not self._dir.is_dir():
            return []
        records = []
        for path in sorted(self._dir.glob("*.json")):
            record = self._load(path)
            if record is not None:
                records.append(record)
        records.sort(key=lambda r: (r.created_at, r.analysis_id))
        return records

    def remove(self, analysis_id: str) -> None:
        path = self._dir / f"{analysis_id}.json"
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    def remove_for_checkpoint(self, task_id: str, checkpoint_id: str) -> int:
        """清掉某 checkpoint 的全部待重试记录（成功后调用）。"""
        removed = 0
        for record in self.list_pending():
            if record.task_id == task_id and record.checkpoint_id == checkpoint_id:
                self.remove(record.analysis_id)
                removed += 1
        return removed

    # ----------------------------------------------------------------- inside

    def _load(self, path: Path) -> PendingAnalysis | None:
        if not path.is_file():
            return None
        try:
            return PendingAnalysis.model_validate(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError) as exc:
            _LOG.warning("ignoring corrupt pending record %s: %s", path, exc)
            return None
