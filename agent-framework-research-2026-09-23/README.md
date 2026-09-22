# Agent 框架：从用户输入到模型输出

对 **Codex、Gemini CLI、Claude Code** 的源码与官方资料进行深入比较，覆盖输入预处理、上下文组装、模型请求、流式事件、工具执行、续轮、压缩、恢复与取消。调研日期：**2026-09-23**。

**推荐直接打开 [离线图文阅读版 index.html](index.html)**。其中包含三套架构图、三套时序图、横向对比图、生命周期图与完整中文说明；架构图支持缩放，也可以独立打开 SVG。

## 先看这三个差异

| 框架 | 本次源码 / 资料显示的主要边界 | 可验证程度 |
|---|---|---|
| **Codex** | 当前 TUI 与 exec 对话入口汇入 app-server；core Session 的 `run_turn` 持有模型与工具循环 | Rust 主链路可追踪到固定源码 |
| **Gemini CLI** | 默认由 UI/headless 宿主协调工具续轮；core 分离模型会话、事件转换和 Scheduler；新 AgentSession 路径由开关控制 | TypeScript 主链路与默认开关可逐行核对 |
| **Claude Code** | CLI 内部持有循环；公开 Python SDK 通过子进程 NDJSON 和双向控制协议集成 | SDK 可验证；CLI 核心只能按官方文档说明，图中明确标记未知 |

**不是一次输入对应一次模型请求。** 一次用户任务可能产生多次模型请求、多个内容块和多次工具执行。收到一段文字、一个流结束事件或一个工具结果，也不必代表整个任务结束。详细证据见下面三篇实现文档。

## 阅读导航

| 文档 | 内容 |
|---|---|
| [00 调研方法与证据标准](docs/00-methodology.md) | 范围、版本口径、源码/文档/推断/未知的区分 |
| [01 Codex 详细实现](docs/01-codex.md) | app-server、TurnInput、StepContext、Responses/lite、工具与流事件 |
| [02 Gemini CLI 详细实现](docs/02-gemini-cli.md) | 默认和可选架构、分层上下文、GeminiChat、provider、Scheduler |
| [03 Claude Code 详细实现与边界](docs/03-claude-code.md) | CLI 与 SDK 边界、预处理、控制协议、块级流、后台任务生命周期 |
| [04 三者横向比较](docs/04-comparison.md) | 逐阶段矩阵、同题路径、协议形状、常见过时结论和设计启示 |

## 图形索引

| 图 | 可直接查看 | 可编辑原稿 |
|---|---|---|
| Codex 架构 | [SVG](diagrams/codex-architecture.svg) | [Mermaid](diagrams/codex-architecture.mmd) |
| Codex 时序 | [SVG](diagrams/codex-sequence.svg) | [Mermaid](diagrams/codex-sequence.mmd) |
| Gemini 架构 | [SVG](diagrams/gemini-architecture.svg) | [Mermaid](diagrams/gemini-architecture.mmd) |
| Gemini 时序 | [SVG](diagrams/gemini-sequence.svg) | [Mermaid](diagrams/gemini-sequence.mmd) |
| Claude Code 架构 | [SVG](diagrams/claude-architecture.svg) | [Mermaid](diagrams/claude-architecture.mmd) |
| Claude Code 时序 | [SVG](diagrams/claude-sequence.svg) | [Mermaid](diagrams/claude-sequence.mmd) |
| 三者对照 | [SVG](diagrams/comparison-dataflow.svg) | [Mermaid](diagrams/comparison-dataflow.mmd) |
| 研究范围 | [SVG](diagrams/harness-scope.svg) | [Mermaid](diagrams/harness-scope.mmd) |
| 生命周期边界 | [SVG](diagrams/lifecycle-boundaries.svg) | [Mermaid](diagrams/lifecycle-boundaries.mmd) |

每张图均为本次研究绘制；原稿也内嵌在对应 Markdown 文档中，方便支持 Mermaid 的阅读器直接显示。

## 固定源码快照

| 官方仓库 | 本次提交 | 版本备注 |
|---|---|---|
| [openai/codex](https://github.com/openai/codex/tree/2c087a0cc2ef1d731086a1d0cb0081bdd792cc4d) | `2c087a0cc2ef1d731086a1d0cb0081bdd792cc4d` | main 中 Cargo 为开发占位 `0.0.0` |
| [google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli/tree/d5b3e3accb26000d273abf16e0f1dd83aa5428a9) | `d5b3e3accb26000d273abf16e0f1dd83aa5428a9` | 包版本 `0.62.0-nightly.20260918.g9450ade79` |
| [anthropics/claude-code](https://github.com/anthropics/claude-code/tree/b486776a2eef0d3f39abaebaa3c03e378c7480b8) | `b486776a2eef0d3f39abaebaa3c03e378c7480b8` | 产品资料仓库，CHANGELOG 顶部 `2.1.278` |
| [anthropics/claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python/tree/f7547d7233527739ece8b12ed28c57be96c966b5) | `f7547d7233527739ece8b12ed28c57be96c966b5` | SDK `0.2.156`；捆绑 CLI 声明存在资料差异，详见第 03 篇 |

可机读证据：[Codex](sources/codex.json)、[Gemini](sources/gemini.json)、[Claude](sources/claude.json)。所有源码结论限定于该快照；官网是滚动文档。没有把 main 当 stable，没有把 API 文档冒充 Claude Code 内部实现，也没有进行模型质量、延迟和费用排名。

## 目录与再生成

```text
agent-framework-research-2026-09-23/
  README.md                 阅读入口
  index.html                离线完整阅读版，内嵌 SVG
  docs/                     方法、三篇实现、横向比较
  diagrams/                 Mermaid 原稿与 SVG 成品
  sources/                  版本、证据、验证记录
  scripts/                  渲染与静态完整性检查
  .cache/                   忽略提交的源码快照与渲染依赖
```

当前环境已配置渲染依赖。修改 Markdown / Mermaid 后，从仓库根目录运行：

```bash
uv run python agent-framework-research-2026-09-23/scripts/build_report.py
uv run python agent-framework-research-2026-09-23/scripts/verify_report.py
```

重新配置渲染环境时，在报告的 `.cache/render` 中安装固定版本 `@mermaid-js/mermaid-cli@11.17.0`，使用 Python `markdown-it-py` 和本地 Chrome；可通过 `CHROME_EXECUTABLE` 指定 Chrome 路径。检查脚本需要 `.cache/` 中存在上述固定源码快照。无需启动三个 agent 或调用模型。

验证结果见 [validation.json](sources/validation.json) 和 [页面检查记录](sources/visual-qa.json)。验证覆盖文档围栏、本地链接、源码链接的文件与行号范围，以及图形可渲染性；语义结论另经过源码阅读和交叉复核。未构建或运行三个产品的完整端到端测试。
