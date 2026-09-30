"""Prompt 版本管理（phase-05 §8）。

Prompt 使用版本文件 ``prompts/evolution-v1.md``（随包分发），
``PROMPT_VERSION`` 与文件名一致；修改 Prompt 必须新增版本文件，
旧事件已记录的 ``prompt_version`` 保持可追溯（05-evolution-analyzer.md §15）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

PROMPT_VERSION = "evolution-v1"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache(maxsize=1)
def load_system_prompt(version: str = PROMPT_VERSION) -> str:
    """加载指定版本的 System Prompt 文本（进程内缓存）。

    :raises FileNotFoundError: 版本文件不存在（发布包缺 package-data）。
    """
    return (_PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")


def render_user_message(request_json: dict) -> str:
    """把 AnalyzerRequest 的 JSON 渲染为用户消息（紧凑单行 JSON）。

    ``digest.excluded`` 是可追溯的过滤记录，明确不进入模型输入
    （change_digest.py 隐私注释约定）。
    """
    data = json.loads(json.dumps(request_json))  # deep copy，不改动调用方数据
    digest = data.get("digest")
    if isinstance(digest, dict):
        digest.pop("excluded", None)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
