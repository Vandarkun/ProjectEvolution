# ProjectEvolution 开发规则

本仓库是 PEL（Project Evolution Ledger）本体，开发工作用 PEL 记录（自举）。

## PEL 记录纪律

- 开始一个明确的开发任务（功能/修复/重构）时，调用 `start_task`（MCP 工具
  `mcp__pel__start_task` 或 CLI `pel task start`）。
- 完成一个子阶段、做出关键设计取舍、关键测试首次通过、或即将压缩/切换
  上下文时，调用 `checkpoint_task`，摘要只写事实（改了什么、为什么、测试结果）。
- 任务结束时调用 `complete_task`。
- 动手改设计前，先用 `get_project_state` / `search_events` / `get_history`
  查一下相关参数与决策的来龙去脉。
- 永远不要手改 `.project-evolution/events/` 下的任何文件。
- 语义分析模型走 DeepSeek（`deepseek-chat`），key 在环境变量
  `DEEPSEEK_API_KEY`（只引用变量名，不落明文）。

不要每轮都调用这些工具——只在上述节点调用。

## 工程约束

- 严格遵守 `project-evolution-ledger-docs/docs/05-implementation/` 各阶段文档，
  包括"明确不做"清单；文档修订的约束不得绕过。
- 测试未通过不 commit；发现前序阶段问题先单独 `fix(前阶段)` 提交。
- 每阶段验收通过后打 `{阶段}-done` tag。
