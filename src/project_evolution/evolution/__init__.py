"""Evolution 层（phase-04 起步）。

当前只包含确定性的 ChangeDigest 构建；evolution.jsonl 与语义分析
属于 Phase 05，本阶段明确不做。
"""

from project_evolution.evolution.change_digest import (
    ChangeDigest,
    ChangeDigestBuilder,
    SelectedHunk,
    privacy_exclusion,
)

__all__ = [
    "ChangeDigest",
    "ChangeDigestBuilder",
    "SelectedHunk",
    "privacy_exclusion",
]
