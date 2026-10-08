"""Tool 目录：名称、JSON Schema、分发表（08-mcp-integration.md §2/§14）。

工具数量刻意保持少（六个），Schema 稳定：字段只增不改语义。
Agent 侧使用规则见 instructions.py（AGENTS.md 样例），不在 Schema 里展开。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from project_evolution.mcp.service import PelToolService

_PROJECT_REF = {
    "type": "string",
    "description": (
        "Project reference: name, id, or root path as served by this server. "
        "Required when the server serves multiple projects."
    ),
}


def _schema(
    properties: dict[str, Any], required: list[str], description: str
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
        "description": description,
    }


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "start_task",
        "description": (
            "Start a tracked development task. Call when beginning a well-defined "
            "unit of work (feature, bugfix, refactor). Returns task_id and the git "
            "baseline captured at start."
        ),
        "inputSchema": _schema(
            {
                "project": _PROJECT_REF,
                "title": {"type": "string", "description": "Short task title"},
                "agent": {"type": "string", "description": "Agent name, e.g. codex"},
                "session_id": {"type": "string", "description": "Existing session to bind"},
            },
            ["title"],
            "Start a task and capture the git baseline.",
        ),
    },
    {
        "name": "checkpoint_task",
        "description": (
            "Record a milestone checkpoint for a running task: saves facts and git "
            "evidence immediately; semantic analysis runs in the background "
            "(analysis_status=pending). Call on subgoal completion, important design "
            "changes, stable test passes, or before context compaction."
        ),
        "inputSchema": _schema(
            {
                "project": _PROJECT_REF,
                "task_id": {"type": "string", "description": "Task id (TASK-...)"},
                "summary": {
                    "type": "string",
                    "description": "What was achieved since the last checkpoint",
                },
                "reason": {"type": "string", "description": "Why this checkpoint, e.g. milestone"},
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Hint of changed paths; git evidence stays canonical",
                },
                "tests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "status": {"type": "string", "enum": ["passed", "failed"]},
                        },
                        "required": ["command", "status"],
                        "additionalProperties": False,
                    },
                    "description": "Test commands run since the last checkpoint",
                },
                "session_id": {"type": "string"},
            },
            ["task_id"],
            "Append a checkpoint with facts and evidence; analysis is async.",
        ),
    },
    {
        "name": "complete_task",
        "description": (
            "Close a task: records a final incremental checkpoint, then marks the "
            "task completed. Idempotent for already-completed tasks."
        ),
        "inputSchema": _schema(
            {
                "project": _PROJECT_REF,
                "task_id": {"type": "string"},
                "summary": {"type": "string", "description": "Final summary of the task"},
                "reason": {"type": "string"},
                "tests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "status": {"type": "string", "enum": ["passed", "failed"]},
                        },
                        "required": ["command", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            ["task_id"],
            "Final checkpoint + close the task.",
        ),
    },
    {
        "name": "get_project_state",
        "description": (
            "Get the compact derived state of a project: active tasks, current "
            "parameters, active features/decisions, recent bug fixes. Use to "
            "restore engineering context when joining a project."
        ),
        "inputSchema": _schema(
            {
                "project": _PROJECT_REF,
                "scope": {"type": "string", "enum": ["summary"]},
            },
            [],
            "Compact current state (never full history).",
        ),
    },
    {
        "name": "get_history",
        "description": (
            "Query history by kind: task | parameter | feature | decision | bug | "
            "file | version. Returns compact results with event ids; fetch "
            "evidence via get_evidence when needed."
        ),
        "inputSchema": _schema(
            {
                "project": _PROJECT_REF,
                "kind": {
                    "type": "string",
                    "enum": ["task", "parameter", "feature", "decision", "bug", "file", "version"],
                },
                "query": {
                    "type": "string",
                    "description": (
                        "task_id / parameter name / feature / subject / file path / bug text"
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["kind"],
            "Compact history query.",
        ),
    },
    {
        "name": "get_evidence",
        "description": (
            "Fetch one evidence item by id (EVD-... diff patch or RAW-... event). "
            "Content is truncated to a safe size; use the returned path for full data."
        ),
        "inputSchema": _schema(
            {
                "project": _PROJECT_REF,
                "evidence_id": {"type": "string", "description": "EVD-... or RAW-..."},
            },
            ["evidence_id"],
            "Progressive disclosure for evidence referenced by history results.",
        ),
    },
]

def register_tools(
    service: PelToolService,
) -> dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]]:
    """把工具名绑定到 service 方法（返回稳定的分发表）。"""
    return {
        "start_task": service.start_task,
        "checkpoint_task": service.checkpoint_task,
        "complete_task": service.complete_task,
        "get_project_state": service.get_project_state,
        "get_history": service.get_history,
        "get_evidence": service.get_evidence,
    }


def tool_names() -> list[str]:
    return [spec["name"] for spec in TOOL_SPECS]
