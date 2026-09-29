"""PEL 错误类型层次。

所有 Core 模块抛出的异常都必须继承 ``PelError``，
禁止在业务代码中抛出裸 ``Exception``。
"""

from __future__ import annotations


class PelError(Exception):
    """PEL 基础异常。"""


class ConfigError(PelError):
    """配置文件缺失关键字段、损坏或不合法。"""


class ValidationError(PelError):
    """领域对象校验失败。

    注意：这是 PEL 自身的语义校验异常，
    与 ``pydantic.ValidationError`` 不同；后者在边界处会被包装成本类型。
    """


class ProjectNotFoundError(PelError):
    """指定的项目根目录不存在。"""


class InvalidStateError(PelError):
    """非法状态转换或非法运行时状态（例如 Task 终态后的生命周期调用）。"""


class StorageError(PelError):
    """持久化层（Ledger / Lock / 文件）错误。"""


class EntityNotFoundError(PelError):
    """请求的领域对象（Task / Session 等）不存在。"""


class TaskNotFoundError(EntityNotFoundError):
    """指定的 Task 不存在。"""


class SessionNotFoundError(EntityNotFoundError):
    """指定的 Session 不存在。"""
