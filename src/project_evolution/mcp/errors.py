"""Tool 层统一错误语义（phase-07 §5，08-mcp-integration.md §11）。

错误返回固定三字段：

```text
code          稳定错误码（本模块常量，永不改名）
message       人可读、可直接透传给 Agent 的说明（不含 Python traceback）
recoverable   Agent 修正参数 / 稍后重试是否可能成功
```

错误码集合 = 08 §11 统一列表 + 两个补充：

- ``PROJECT_AMBIGUOUS``：多项目无法唯一解析（phase-07 §4 要求显式报错，不猜）；
- ``EVIDENCE_NOT_FOUND``：可选工具 get_evidence 的下钻目标不存在；
- ``INTERNAL_ERROR``：兜底（细节写 server 日志，不泄漏 traceback）。
"""

from __future__ import annotations

from project_evolution.errors import (
    ConfigError,
    InvalidStateError,
    PelError,
    StorageError,
    TaskNotFoundError,
    ValidationError,
)
from project_evolution.git.adapter import GitError
from project_evolution.index.projection import ProjectionError

PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
PROJECT_AMBIGUOUS = "PROJECT_AMBIGUOUS"
TASK_NOT_FOUND = "TASK_NOT_FOUND"
INVALID_TASK_STATE = "INVALID_TASK_STATE"
GIT_UNAVAILABLE = "GIT_UNAVAILABLE"
LEDGER_WRITE_FAILED = "LEDGER_WRITE_FAILED"
ANALYSIS_PENDING = "ANALYSIS_PENDING"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
INTERNAL_ERROR = "INTERNAL_ERROR"

#: 默认是否可恢复（Agent 侧修正后重试可能成功）
_RECOVERABLE: dict[str, bool] = {
    PROJECT_NOT_FOUND: True,
    PROJECT_AMBIGUOUS: True,
    TASK_NOT_FOUND: True,
    INVALID_TASK_STATE: True,
    GIT_UNAVAILABLE: True,
    LEDGER_WRITE_FAILED: True,
    ANALYSIS_PENDING: True,
    INVALID_ARGUMENT: True,
    EVIDENCE_NOT_FOUND: True,
    INTERNAL_ERROR: False,
}

#: Core 异常 -> 稳定错误码（未知 PelError 子类按 LEDGER_WRITE_FAILED 处理太激进，
#: 统一归 INTERNAL_ERROR 并保留 message）
_CODE_BY_EXCEPTION: tuple[tuple[type[BaseException], str], ...] = (
    (ValidationError, INVALID_ARGUMENT),
    (TaskNotFoundError, TASK_NOT_FOUND),
    (InvalidStateError, INVALID_TASK_STATE),
    (ConfigError, INVALID_ARGUMENT),
    (GitError, GIT_UNAVAILABLE),
    (StorageError, LEDGER_WRITE_FAILED),
    (ProjectionError, LEDGER_WRITE_FAILED),
)


class ToolError(Exception):
    """工具执行失败：返回稳定错误码而不是 traceback。"""

    def __init__(self, code: str, message: str, *, recoverable: bool | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = (
            _RECOVERABLE.get(code, False) if recoverable is None else recoverable
        )

    def to_payload(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "recoverable": self.recoverable}


def tool_error_from_exception(exc: BaseException) -> ToolError:
    """把 Core 异常映射为稳定 ToolError；未知异常归 INTERNAL_ERROR。"""
    for exc_type, code in _CODE_BY_EXCEPTION:
        if isinstance(exc, exc_type):
            return ToolError(code, str(exc))
    if isinstance(exc, PelError):
        return ToolError(INTERNAL_ERROR, str(exc))
    return ToolError(
        INTERNAL_ERROR,
        f"unexpected internal error: {type(exc).__name__}",
        recoverable=True,
    )
