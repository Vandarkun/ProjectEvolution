# Project Evolution Ledger (PEL)

给 Coding Agent 用的**项目工程演进账本**——回答一个 Git Log 和聊天记录都答不了的问题：

> **这个项目是怎么一步一步变成现在这样的？**

三个月后你问「透明度步长为什么是 5%？」「双击取内容为什么不选快捷键？」「那个中键卡死的 bug 当时怎么修的？」——Git 里没有答案（只记了改了什么，没记为什么），聊天记录早就随会话蒸发了。PEL 在 Agent 干活的过程中自动把**事实与工程语义**记成结构化、可查询、带证据的历史。

---

## 它是怎么工作的

```
   Coding Agent（ZCode / Codex / Claude Code / 任何 MCP 或 shell 客户端）
        │
        │  开任务/阶段/收工时调用（MCP 工具 或 CLI）
        ▼
┌─────────────────────────────────────────────────────────┐
│                     PEL 三层结构                          │
│                                                          │
│  ① 事实层（毫秒落盘，永不丢失，模型碰不到）               │
│     raw.jsonl        只追加的事件账本                     │
│     evidence/*.patch diff 证据文件（每条语义事件的收据）   │
│                                                          │
│  ② 语义层（后台异步，失败不阻塞开发）                     │
│     digest(预算内变更摘要) → LLM 提案 → 证据校验 → 去重   │
│     → evolution.jsonl（四类事件，见下）                   │
│                                                          │
│  ③ 查询层（可丢弃派生层，随时从账本重建）                 │
│     SQLite 投影 + 向量索引                               │
└─────────────────────────────────────────────────────────┘
```

**Agent 的日常只有三步**，其余全自动：

```
start_task      开工：拍 git 基线
  │  写代码、跑测试
checkpoint      阶段：diff/测试结果/事实摘要 毫秒落盘
  │             └─ 后台：LLM 蒸馏出语义事件（异步，断网转 pending 可重试）
complete_task   收工：收尾 checkpoint + 关闭任务
```

## 记下来的是什么

四类语义事件，每条**强制挂证据 ID**（模型编造的证据会被整批拒绝）：

| 类型 | 记什么 | 真实示例 |
|---|---|---|
| **parameter_change** | 参数从什么变成什么、为什么 | `OpacityMath.Step: 0.1 → 0.05`，理由"10% 太粗，对齐参考图时不好微调" |
| **decision** | 选了什么、备选是什么、为什么 | 「取内容触发选双击左键」，弃快捷键（贴图窗口收不到键盘） |
| **bug_fix** | 现象 → 根因 → 修法 → 验证 | 「暂停残留吞放标志 → 恢复后中键卡死 → 补一行重置 → 复现测试通过」 |
| **feature_change** | 能力边界的变化 | 「取字手势升级：QR 优先、OCR 兜底」 |

一条记录长这样（查询「透明度步长的历史」的返回）：

```
滚轮透明度步长 10% → 5%
  参数    OpacityMath.Step: 0.1 → 0.05（透明度比例/每格滚轮）
  理由    10% 太粗，对齐参考图时不好微调
  时间    2026-10-09T08:34:55（UTC，微秒级；另有单调 seq 保证严格顺序）
  证据    EVD-01M4FTBYPNHMGQKAWA23PJ9PNC → 磁盘上的 diff 原文，可核对
```

## 功能一览

- **任务生命周期**：start / checkpoint / complete，跨会话续接（context compact 后新会话接着干）
- **语义分析**：DeepSeek / OpenAI 兼容 / Ollama 任选；prompt 固定版本可重放；同 checkpoint 重试幂等
- **查询四件套**：
  - `get_project_state`——当前全部参数/生效决策/近期 bug（开场恢复上下文用）
  - `get_history`——按任务/文件/参数/决策精确查历史
  - `search_events`——**语义检索，中英跨语言**（中文问题命中英文事件；本地向量模型，离线可用）
  - `get_evidence`——按证据 ID 下钻 diff 原文
- **报告导出**：`pel export` 一键生成完整演进报告（Markdown / HTML）
- **可靠性**（七项故障注入实测）：断网/模型输出非法/Git 不可用/SQLite 删除/账本截断/MCP 重启/漏 checkpoint——**事实历史零丢失**
- **隐私**：secret 文件（.env/*.pem/*.key…）内容永不进模型与证据，清单可配置

---

## 使用方法

### 1. 安装

```bash
pip install git+https://github.com/Vandarkun/ProjectEvolution
# 或克隆后本地安装
pip install -e .
```

依赖（pydantic / PyYAML / fastembed）自动装齐；首次语义检索会下载本地 embedding 模型（约 100MB，之后离线可用）。

### 2. 配置（项目根 `pel.config.yaml`）

全量配置一次看清，未写的键用内置默认值（完整模板见仓库根 [`pel.config.yaml.example`](pel.config.yaml.example)）。最少只需填分析模型：

```yaml
analyzer_provider: openai            # mock | openai | ollama
analyzer_model: deepseek-chat
analyzer_base_url: https://api.deepseek.com
analyzer_api_key: sk-xxxx            # 明文（本文件已被 gitignore）
# 或 analyzer_api_key_env: DEEPSEEK_API_KEY   # 引用环境变量

retrieval:
  model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2  # 跨语言检索

change_digest:
  max_files: 50                      # 送模型的变更摘要预算
  max_total_diff_chars: 40000
```

> 真实配置不入库（gitignore 排除 `pel.config.yaml`），仓库里提交 example 模板。
> 未配置分析模型时自动降级为 mock：任务/事实/查询全部可用，只是不产出语义事件。

### 3. 接入你的 Coding Agent

**MCP 客户端**（Claude Code / Codex / ZCode / Cursor…）——注册服务器条目（各家字段名略有差异，形式相同）：

```json
{ "mcpServers": { "pel": {
    "command": "pel",
    "args": ["mcp", "serve", "--project", "/path/to/your/project"] } } }
```

注册后 Agent 自动获得 7 个工具与使用时机指引。再把记录规则放进项目的 `AGENTS.md`（一段话即可）：

```bash
pel mcp agent-instructions >> AGENTS.md   # 生成规则片段
```

**任何能跑 shell 的 Agent**——零注册，直接用 CLI（见下）。

### 4. 日常使用（Agent 自动做，人也可以手动）

```bash
# 任务生命周期（shell Agent 的通道）
pel task start "修复登录超时" --agent zcode
# ...开发、跑测试...
pel task checkpoint TASK-xxx --summary "连接超时 30s→10s，加重试" \
    --test "pytest -q=passed"
pel task complete TASK-xxx

# 语义检索（中英跨语言）
pel search "登录超时当时为什么改"
pel search "why did login timeout change"

# 导出演进报告
pel export --format markdown --output report.md

# 运维：账本体检/修复、投影重建、失败分析重试
pel doctor [--repair]
pel index rebuild
pel retry-pending
```

---

## 数据放在哪、能信吗

```
你的项目/
├── pel.config.yaml          配置（手编，git 忽略）
└── .project-evolution/      全部生成数据（git 忽略，纯机器产物）
    ├── events/raw.jsonl         事实账本（只追加，严格顺序 seq）
    ├── events/evolution.jsonl   语义事件账本
    ├── evidence/diffs/*.patch   diff 证据文件
    └── .runtime/                SQLite 投影 + 向量索引（删了随时重建）
```

信任的三根支柱：

1. **事实与解释分离**：事实由代码确定性采集，模型只能"提案"语义事件，且每个说法必须挂本 checkpoint 证据清单内的 ID——编造即拒
2. **一切可重建**：SQLite/向量索引是派生层，`pel index rebuild` 从 JSONL 重放；账本截断可检测可修复
3. **全程可追溯**：每条事件带 analyzer 版本、prompt 版本、输入指纹，未来可重跑对比质量

## 文档与状态

- 设计文档：[`project-evolution-ledger-docs/`](project-evolution-ledger-docs/docs/)（架构 / 数据模型 / 各阶段实现）
- V0.1 真实项目验证报告（ScreenPin × 6 任务 × 23 语义事件 × 七项故障注入）：[`docs/06-validation/phase-09-validation-report.md`](project-evolution-ledger-docs/docs/06-validation/phase-09-validation-report.md)
- 状态：V0.1 验收全部通过；测试 396 项全绿
