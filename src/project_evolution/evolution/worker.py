"""后台语义分析 worker（phase-05 §3"background analysis worker（默认异步）"）。

05-evolution-analyzer.md §16 的默认执行模式：

- Checkpoint 事实与 Evidence 先落盘并立即返回（同步路径只做确定性采集）；
- 语义分析在独立后台线程执行；
- 分析失败进入 pending（由 :class:`EvolutionAnalyzerService` 负责），
  worker 只记录诊断，绝不向提交线程抛出——
  Coding Agent 当前任务不因 Memory/Analyzer 失败而判定开发失败。
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from project_evolution.errors import PelError
from project_evolution.evolution.analyzer_service import (
    EvolutionAnalyzerService,
)

_LOG = logging.getLogger(__name__)

#: 空队列轮询间隔（秒）
_POLL_INTERVAL = 0.05


class BackgroundAnalysisWorker:
    """单线程后台分析 worker；submit() 非阻塞，供 checkpoint 挂钩调用。"""

    def __init__(self, analyzer: EvolutionAnalyzerService):
        self._analyzer = analyzer
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        #: 后台失败诊断（真实状态在 pending 队列，这里只便于观测）
        self.failures: list[str] = []
        self.completed: list[tuple[str, str, str]] = []  # (task_id, checkpoint_id, status)

    # ---------------------------------------------------------------- public

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(self, task_id: str, checkpoint_id: str) -> None:
        """把一次分析排队（幂等由 analysis_key 保证，重复提交安全）。"""
        if not self.running:
            self.start()
        self._queue.put((task_id, checkpoint_id))

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="pel-evolution-analyzer", daemon=True
        )
        self._thread.start()

    def stop(self, *, drain: bool = True, timeout: float = 60.0) -> None:
        """停止 worker；默认先处理完队列中已提交的分析。"""
        if drain:
            self.wait_idle(timeout=timeout)
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_POLL_INTERVAL * 20)
        self._thread = None

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """等待队列清空且当前分析完成；超时返回 False。

        用 ``unfinished_tasks``（含已入队未取走）+ ``_idle``（当前项完成）
        双条件，避免"入队瞬间队列看起来空闲"的竞态。
        """
        deadline = time.monotonic() + timeout
        while True:
            if self._queue.unfinished_tasks == 0 and self._idle.is_set():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    # ---------------------------------------------------------------- inside

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task_id, checkpoint_id = self._queue.get(timeout=_POLL_INTERVAL)
            except queue.Empty:
                continue
            self._idle.clear()
            try:
                outcome = self._analyzer.analyze_checkpoint(task_id, checkpoint_id)
                self.completed.append((task_id, checkpoint_id, outcome.status))
                _LOG.info(
                    "background analysis %s for checkpoint %s: %s (%d event(s))",
                    outcome.analysis_id,
                    checkpoint_id,
                    outcome.status,
                    len(outcome.events),
                )
            except PelError as exc:
                detail = f"{type(exc).__name__}: {exc}"
                self.failures.append(f"{checkpoint_id}: {detail}")
                _LOG.warning(
                    "background analysis failed for checkpoint %s (kept in pending): %s",
                    checkpoint_id,
                    detail,
                )
            except Exception:  # noqa: BLE001 - worker 线程绝不崩溃进程
                self.failures.append(f"{checkpoint_id}: unexpected analyzer crash")
                _LOG.exception("unexpected analyzer crash for checkpoint %s", checkpoint_id)
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    self._idle.set()
