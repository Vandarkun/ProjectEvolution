# Project Evolution Ledger (PEL)

面向 Coding Agent 的项目工程演进记录系统。回答一个核心问题：
**这个项目是怎样一步一步变成现在这样的？**

当前实现（Phase 01 ~ Phase 08）：

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
  续接、Checkpoint index 在 Task 内严格递增；Canonical 状态由 RawEvent Ledger
  投影重建，`.runtime/tasks/` journal 只是恢复用镜像。
- **Git 变化分析**（`project_evolution.git`）：只读 GitAdapter（子命令白名单 +
  超时 + 参数数组，`.project-evolution/` 从一切采集永久排除）、逐文件内容快照 +
  sha256 blob 去重、快照间增量 diff（不做 patch 相减；迭代空间为两份快照
  `files` 的并集 **并集上 HEAD 区间 diff 的路径**——Checkpoint 之间被 commit
  的改动同样可见，06 §4）、rename 识别（hash 精确 / 内容相似度 + find-renames
  交叉验证）、diff evidence（patch 落 `evidence/diffs/`，secret 只记 path+hash
  不落内容）、checkpoint 间的新提交生成归属该 Task/Checkpoint 的
  `git_commit` RawEvents（06 §9，只动 `.project-evolution/` 的提交被排除）、
  采集用 pathspec 限定在项目子树（项目目录嵌套在外层仓库内时不串入父仓库
  变更，06 §5）、non-Git 明确降级。
- **ChangeDigest**（`project_evolution.evolution`）：发往模型的确定性变更摘要
  （changed/added/deleted/renamed、diff_stat、预算内 hunk 选取、truncated），
  privacy filter 硬编码排除 secret 与 PEL 自身目录，generated/binary/lock
  可追溯地记入 excluded；附 evidence 清单（模型 `evidence_ids` 的唯一合法来源）。
- **Evolution Event Ledger**（Phase 05）：`evolution.jsonl` 与 Raw Ledger 同一套
  可靠追加/重放保证（共用 `BaseJsonlLedger`）；每条事件携带
  `analysis_batch_id` / `analyzer_version` / `prompt_version` / `analysis_key`；
  同批次整批提交，`analysis_key` 跨批次重复在存储层被拒绝。
- **Evolution Analyzer**（Phase 05）：`Checkpoint -> ChangeDigest -> Provider ->
  Validator -> Dedup -> evolution.jsonl` 完整闭环。
  - Provider 协议 + `MockProvider` / `OpenAICompatibleProvider` / `OllamaProvider`
    （stdlib transport 可注入，失败统一 `ProviderError`）；
  - Prompt 版本文件 `prompts/evolution-v1.md` 随包分发（只输出 JSON、不确定即
    `events=[]`、无证据 reason 置空、refactor ≠ feature、临时变量 ≠ parameter）；
  - Validator：非法类型 / 缺 Evidence / Evidence 不存在 / 不在本 Checkpoint
    清单内 / parameter before=after 无解释 / 空 title / 超长字段 / 重复
    analysis key 八类拒绝 + 类型级 Schema 校验，全部出结构化诊断；
  - 幂等：`analysis_key = project|task|checkpoint|analyzer_version|digest_hash`，
    同键返回已有 batch 不再调用模型；NOOP 结果入 runtime 缓存；
  - 去重：normalized key + exact before/after + title Jaccard；
  - Pending：模型失败转 `.runtime/pending/<ANL-id>.json`，Raw Ledger 不回滚，
    `retry_pending()` 成功清除、失败累加 attempts；
  - 默认异步：`BackgroundAnalysisWorker` 后台线程执行语义分析，
    checkpoint 事实落盘即返回，Analyzer 失败不影响开发任务。
- **评估基线**（Phase 05）：70 个人工标注离线样例
  （10 feature + 20 parameter + 10 decision + 10 bug_fix + 20 NOOP），
  `OfflineRuleProvider` 确定性提取，Precision 门槛 0.85（当前基线
  Precision 1.0 / Recall 1.0 / NOOP 零违例）。
- **SQLite 投影与查询层**（Phase 06，`project_evolution.index`）：
  - `rebuild_projection()`：按 seq 重放 raw.jsonl + evolution.jsonl 构造全部
    投影表（tasks/sessions/checkpoints/raw_events/evolution_events/evidence/
    parameter_changes/feature_changes/decisions/bug_fixes/file_changes/
    projection_meta），integrity check + WAL checkpoint 后切换指针；
  - Windows 兼容切换协议：`.runtime/evolution-<n>.db` 版本化文件 +
    `current-db.txt` 指针（tmp + `os.replace` 带重试），重建不替换旧文件，
    旧连接在切换期间持续可读，旧版本文件延迟清理；
  - `update_projection()`：水位（raw_seq/evo_seq）增量追平，与 Ledger 写锁
    同一串行化策略；SQLite 失败只标记 stale，绝不回滚 Canonical Ledger；
    full rebuild 与 incremental 结果逐表一致（测试保证）；
  - `QueryService`：`get_task_history` / `get_parameter_history` /
    `get_feature_history` / `get_decision_history` / `get_bug_history` /
    `get_file_history`（rename 链追踪）/ `get_project_state`；Current State
    全部由事件归约（Parameter 取最新有效 after、Decision 应用 supersede、
    Feature 按 added/modified/removed），查询只读 SQLite 不调用任何模型；
  - 性能：17k raw + 3k 语义事件 rebuild 约 4s，常规查询 < 120ms。
- **MCP 集成**（Phase 07，`project_evolution.mcp`）：
  - stdio MCP Server（`pel mcp serve` / `python -m project_evolution.mcp`）：
    initialize 版本协商（2024-11-05 / 2025-03-26 / 2025-06-18）、tools/list、
    tools/call、ping；stdout 只写协议消息（handler 执行期间第三方 print 一律
    改道 stderr），logging 全部 stderr，连续 100+ 次调用不破坏协议（测试保证）；
  - 五个核心工具 + 可选下钻：`start_task` / `checkpoint_task` / `complete_task` /
    `get_project_state` / `get_history`（task|parameter|feature|decision|bug|file|
    version）/ `get_evidence`（EVD- patch 与 RAW- 事件，内容裁剪到 20k 字符）；
  - 语义分析默认异步：checkpoint 事实与 Evidence 落盘即返回
    （`checkpoint_saved=true, analysis_status=pending`），Evolution Model 失败
    绝不把工具调用整体判为 failed（partial success 语义）；
  - Project Resolution：显式 `project`（name/id/root 精确匹配）→ 当前绑定 →
    单项目 pinned（`--project`）；多项目走 `--registry` JSON 文件；
    无法唯一解析时报 `PROJECT_AMBIGUOUS` / `PROJECT_NOT_FOUND`，绝不猜测；
  - 稳定错误码（code/message/recoverable 三字段）：`PROJECT_NOT_FOUND` /
    `PROJECT_AMBIGUOUS` / `TASK_NOT_FOUND` / `INVALID_TASK_STATE` /
    `GIT_UNAVAILABLE` / `LEDGER_WRITE_FAILED` / `ANALYSIS_PENDING` /
    `INVALID_ARGUMENT` / `EVIDENCE_NOT_FOUND` / `INTERNAL_ERROR`；
    内部异常细节只写 server 日志，永不把 traceback 返回给 Agent；
  - 查询工具先做增量投影追平，响应携带 `projection.stale / in_sync` 元数据。
- **Dashboard 与导出**（Phase 08，`project_evolution.dashboard`）：
  - FastAPI read-only API（全 GET）：`/api/overview` / `/api/timeline` /
    `/api/tasks/{id}` / `/api/parameters/{name}` / `/api/features/{name}` /
    `/api/decisions/{subject}` / `/api/bugs` / `/api/evidence/{id}`，
    数据全部来自 Query Service（先增量追平投影，与 MCP 查询路径一致）；
  - 四个必做页面（无 JS / 无前端构建的服务端渲染）：Project Overview、
    Timeline（Task/Feature/Parameter/Decision/Bug/Version kind 筛选，
    Checkpoint 可折叠，File Event 不进入 Timeline）、Task Detail
    （Checkpoint / Changed Files / Tests / Evolution Events / Evidence 入口
    均可展开）、Parameter History（Before → After → Current，含按 scope
    拆分）；
  - Evidence 下钻：`/evidence/{EVD-id}` 以纯文本 `<pre>` 展示 Patch
    （裁剪到 20k 字符），元数据来自投影 evidence 表；
  - `pel export --format markdown|html [--output FILE]`：10 节结构的
    Evolution Report（总览 / 时间范围 / Tasks / Feature / Parameter /
    Decision / Bug-Fix / 关键 Changed Files / 当前状态 / Provenance 摘要），
    关键结论携带 `[EVO-*]` / `[RAW-*]` Event ID，可独立生成；
  - 安全（11 §9）：所有动态文本经统一 `html.escape` 管线（表格 / 列表 /
    details 构建函数自动转义，无 raw HTML 出口）；所有响应带
    `X-Content-Type-Options: nosniff`；恶意 commit message /
    title / patch 只显示为文本（XSS 测试保证）；本地服务默认仅监听
    loopback（`pel dashboard`，默认 `127.0.0.1:8642`）；Dashboard 只读且
    与 Core / MCP 完全解耦 —— 关闭/删除 Dashboard 不影响采集与 Ledger
    （测试保证：运行期间账本字节不变，停止后 Ledger 追加 / MCP 查询 /
    投影重建照常）；
  - Web 栈为可选 extra：`pip install 'project-evolution[dashboard]'`
    （fastapi + uvicorn）；不装时 `pel export` 与其余命令不受影响。

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

# 同步语义分析（测试/CLI 场景；产品路径默认走后台异步 worker）
pel analyze TASK-... CHK-... --project PATH [--provider mock|openai|ollama]
pel retry-pending --project PATH [--provider ...] [--json]

# SQLite 投影：按 seq 全量重放重建（删除 DB 后可完整恢复）
pel index rebuild --project PATH [--json]

# MCP stdio Server（Phase 07）
pel mcp serve --project PATH [--provider mock|openai|ollama] [--model ...] [--registry FILE]
pel mcp agent-instructions [--project NAME]   # 输出最短 AGENTS.md 规则样例

# Task 兼容入口（只支持 Shell 的 Agent；输出 JSON）
pel task start "TITLE" --project PATH [--agent codex] [--session-id SES-...]
pel task checkpoint TASK-... --project PATH --summary "..." [--test "cmd=passed"] [--provider ...]
pel task complete TASK-... --project PATH --summary "..." [--provider ...] [--no-wait]

# Evolution Report 导出（Phase 08；可独立生成，无需 Dashboard 运行）
pel export --project PATH --format markdown|html [--output FILE]

# 本地只读 Dashboard（Phase 08；默认仅监听 127.0.0.1，需 [dashboard] extra）
pel dashboard --project PATH [--host 127.0.0.1] [--port 8642]
```

`pel doctor` 检查 raw ledger 完整性（JSON 可解析、ID 重复、seq 单调、尾部损坏）；
`--repair` 只截断"未确认写入"的损坏尾部，绝不静默修改中间损坏。

`pel analyze` / `pel retry-pending` 的 Provider 选择：`--provider mock`（默认），
真实模型用 `--provider openai --model ... --base-url ... --api-key-env NAME` 或
`--provider ollama --model ...`。真实 Provider 的集成测试默认跳过，由环境变量
opt-in（见 `tests/test_providers_integration.py` 头部说明）。

## 快速上手

```python
from project_evolution.evolution import MockProvider
from project_evolution.git import create_git_aware_task_services

# Task/Checkpoint 服务 + Git 采集 + 默认异步语义分析（传 analysis_provider）
services = create_git_aware_task_services(
    "./my-project", project_id="PRJ-...", analysis_provider=MockProvider()
)

task = services.tasks.start_task("PRJ-...", "实现外部窗口透明度", agent="codex")

# ... 编码 ...

# 阶段 Checkpoint：事实与证据先落盘并立即返回；语义分析在后台执行
cp = services.checkpoints.create_checkpoint(task.id, summary="核心逻辑完成")

services.analysis_worker.wait_idle()   # 可选：等待后台分析完成
services.analysis_worker.stop()

from project_evolution.ledger import iter_evolution_events
for event in iter_evolution_events("./my-project"):  # 严格按 seq 顺序
    print(event.type, event.title, event.evidence_ids)

services.tasks.complete_task(task.id, summary="透明度完成")

# SQLite 投影与查询（Phase 06）
from project_evolution.index import QueryService, rebuild_projection, update_projection

rebuild_projection("./my-project")          # 或：pel index rebuild
update_projection("./my-project")           # 增量追平（无 DB 时自动 rebuild）

query = QueryService("./my-project")
state = query.get_project_state()           # 当前参数 / 活跃决策 / Feature
history = query.get_parameter_history("OpacityMin")
print(state.current_parameters, history.current)

# Timeline 与 Dashboard（Phase 08）
timeline = query.get_timeline(["parameter", "decision"], limit=50)
for item in timeline:
    print(item.timestamp, item.kind, item.title, f"[{item.event_id}]")

# pel dashboard → http://127.0.0.1:8642（Overview / Timeline / Task Detail /
# Parameter History / Evidence 下钻；全部只读）
```

设计文档见 `project-evolution-ledger-docs/docs/`。

## MCP Server 接入 Coding Agent

单项目 pinned（Codex / Claude Code / 任意 MCP 客户端的 stdio 配置）：

```json
{
  "mcpServers": {
    "pel": {
      "command": "pel",
      "args": ["mcp", "serve", "--project", "D:/code/MyProject"]
    }
  }
}
```

多项目 registry（`registry.json`，`name` 唯一；工具调用必须显式传 `project`）：

```json
{"projects": [{"name": "MyProject", "root": "D:/code/MyProject"}]}
```

```json
{"args": ["mcp", "serve", "--registry", "D:/code/registry.json"]}
```

真实语义分析（默认 mock，不联网）：`--provider openai --model ... --api-key-env NAME`
或 `--provider ollama --model ...`。

Agent 侧最短规则（`pel mcp agent-instructions` 输出，可直接粘贴进 AGENTS.md）：

```text
This project uses the Project Evolution Ledger (PEL) MCP tools.

- Call `start_task` when you begin a well-defined development task
  (feature, bugfix, refactor).
- Call `checkpoint_task` when a significant subgoal is done, an important
  design choice was made, key tests first pass, or you are about to
  compact/switch context.
- Call `complete_task` when the task is finished.
- Call `get_history` / `get_project_state` when you need past designs,
  parameters, decisions, or bug fixes.
- Never edit files under `.project-evolution/events` by hand.

Do not call these tools every turn — only at the moments above.
```
