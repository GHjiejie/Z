# DeepAgent Platform

这是 `DESGIN.md` 架构方案的可运行 Phase 1 实现。项目采用模块化单仓库：FastAPI 提供控制面、执行控制面和 Native API，React 提供可视化控制台；SQLite 是本地参考仓储，可通过 `Database` 接口替换成 PostgreSQL。

## 已实现功能

- Agent Draft → immutable Revision → ResolvedExecutionPlan → Deployment 完整发布链路；
- Prompt、模型部署、能力、策略、预算和 Runtime Image 的依赖锁定与 Plan Hash；
- Thread、Run、RunAttempt、Worker Lease、Checkpoint 和持久 RuntimeEvent；
- SSE 流式事件及 `after_sequence` / `Last-Event-ID` 断线续传；
- HITL 暂停、Approve / Edit / Reject / Respond、乐观锁与幂等决策；
- 审批后创建新 Attempt，并使用原 Plan Hash 从 Checkpoint 恢复；
- Artifact、Usage Ledger、模型/工具/SubAgent/审批事件和租户隔离；
- React 控制台：Overview、Agent Builder、Playground、Runs & Traces、Approvals、Resources；
- 无模型密钥即可运行的确定性 Reference Harness，便于验证平台契约；
- Deep Agents Harness Adapter 与 Runtime Binder 边界，真实 SDK 接入不会侵入 Controller。
- 内置 Plugin / Skill Registry：启动发现、幂等注册、版本锁定、Artifact Hash 校验与运行时加载。
- Knowledge/RAG：内置 `builtin_rag` Agent 自动判断知识检索或模型直答，支持 OSS 直传授权、持久摄取、不可变索引、混合检索、ACL 与可验证 Citation。
- Coding Agent：启动时幂等预置并部署 `Built-in Coding Agent`，使用真实 `create_deep_agent()` / LangGraph Tool Loop、内容寻址源码快照、Thread-scoped Workspace、Docker Sandbox、HITL、平台重算 Patch/Diff/Verification 和 Coding Workbench。

## 快速启动

```bash
cd /Users/zhengjie/Github/Z/deepAgentsProject
make install
make build
make api
```

Coding Agent 还要求本机 Docker daemon 可用。第一次发布默认 Docker Profile 时，控制面会构建（或复用）`deepagent/coding-runtime:0.1.0`，解析真实 OCI image digest，并把它锁入 Execution Plan；运行时若镜像摘要不匹配会拒绝启动。

打开 [http://localhost:8000](http://localhost:8000)。API 文档位于 [http://localhost:8000/docs](http://localhost:8000/docs)。

如果本机没有认证代理，仅用于本地演示时以 `DEEPAGENT_ALLOW_DEMO_IDENTITY=true make api` 启动；生产环境不得启用该开关。

开发模式可分别启动：

```bash
make api
make web
```

React 开发服务器为 [http://localhost:5173](http://localhost:5173)，请求会代理到 `:8000`。

## 演示核心闭环

1. 在 **Agents** 修改 Draft，执行 Validate、Publish；系统创建不可变 Revision、Plan 和 Development Deployment。
2. 在 **Playground** 运行普通分析任务，观察 Model、Tool、SubAgent、Todo、Artifact 和 Usage 事件。
3. 输入含有 `deploy to production` 或“部署到生产”的任务，Run 会进入 `WAITING_FOR_APPROVAL`。
4. 在 **Approvals** 批准或拒绝。批准会产生第二个 RunAttempt，从持久 Checkpoint 恢复并完成。
5. 在 **Runs & traces** 查看 Plan Pin、Attempt、完整事件序列和成本。
6. 在 **Knowledge** 创建知识库、上传文件、等待索引完成并测试带 Citation 的检索；把生效的 Knowledge Revision 绑定到 Agent 后，内置 RAG Agent 会对事实型请求检索，对创作等无需知识库的请求自动走模型直答。
7. 启动后直接进入 **Coding** 使用已发布、已部署的 `Built-in Coding Agent`；注册允许范围内的 Repository、选择 Base Ref 并启动任务。也可以在 **Agents** 用 `Coding Agent starter` 创建自定义实例。Workbench 会展示只读源码、实时事件、命令证据、Diff、验证结果、审批和 Patch。

## 测试与构建

```bash
make verify
```

集成测试覆盖：

- Revision 不可变与 Plan Hash；
- Run 创建幂等；
- RuntimeEvent 序列和断点读取；
- HITL Checkpoint、幂等决策和多 Attempt 恢复；
- Tenant / Project 数据隔离。
- Knowledge 上传、文件完整性校验、摄取、版本发布、检索、下载与 Agent Runtime Citation；
- Knowledge 的 Tenant / Project / Role 隔离和错误上传拒绝。
- 真实 Deep Agents 文件/命令 Tool Loop、受保护路径审批与 Checkpoint 恢复；
- Docker 非 root、只读根文件系统、默认禁网、密钥隔离、超时/输出/磁盘限制和软链接逃逸；
- Run 取消终止容器命令、Sandbox 丢失恢复、Patch 防篡改、平台重算文件 Hash，以及宿主工作区不被修改。

## 代码结构

```text
apps/
├── platform_api/          FastAPI Native API 与进程装配
└── web/                   React + TypeScript 控制台
packages/
├── domain/                稳定领域模型
├── application/           Agent 发布与审批用例
├── compiler/              静态验证、依赖锁定、Plan Hash
├── runtime/               Orchestrator、Lease、Binder、Executor、Event
├── knowledge/             OSS、摄取、解析、分块、Embedding、检索与 Citation
├── repositories/          Repository 注册与不可变源码快照
├── sandbox/               Docker/Fake Provider、策略、恢复与生命周期
├── coding/                Workspace、Verification、ChangeSet 与 API 用例
├── adapters/harness/      Deep Agents 稳定适配边界
└── persistence/           SQLite 参考仓储与 Schema
tests/                      端到端平台契约测试
```

## Reference Harness 与真实 Deep Agents

普通 Agent 继续保留确定性 Reference Harness，目的是让发布、调度、流式、HITL、审计和计费在无外部凭据时可验证。`coding-agent-v1` 已接入锁定版本的真实 `create_deep_agent()`、持久 LangGraph Checkpointer 和事件适配器；文件与命令结果来自 Sandbox，Diff、Verification 和 Artifact 由平台重新计算，不能由模型自行声明。

真实 Credential 禁止进入 Revision、Plan、Checkpoint、Event 或 Prompt。Coding MVP 为 `patch_only`，明确禁止 Commit、Push、PR 和 Deploy，也不会把宿主目录读写挂载进容器。

## 内置 Plugin 与 Skill

项目自带 `builtin_plugins/deepagent-core` 声明式插件包，启动 API 时会自动发现并注册以下 Skills：

- `task-planning`：复杂任务拆解、状态维护与交付验证；
- `evidence-research`：证据收集、冲突处理与可追溯引用；
- `release-safety`：发布检查、生产变更审批与回滚意识。

默认 Agent 会绑定 `task-planning` 和 `release-safety`。发布 Agent 时，Skill 引用会被解析为不可变的 `skill_version`，其版本、说明、完整指令和 SHA-256 Artifact Hash 一同锁入 `ResolvedExecutionPlan`；Worker 构建 Harness 时会再次校验 Hash 并加载指令，同时产生 `skill.loaded` RuntimeEvent。

插件格式如下：

```text
my-plugin/
├── plugin.json
└── skills/
    └── my-skill/
        └── SKILL.md
```

`plugin.json` 只声明元数据和 Skill 文件路径。Phase 1 不会从插件目录导入或执行代码。需要加载额外的本地插件目录时，在 `.env` 中配置 `DEEPAGENT_PLUGIN_PATHS`（多个目录使用操作系统 path separator 分隔）。同一 Skill 版本的内容不可原地修改；内容变化必须提升版本号。

可通过以下接口检查启动结果：

```text
GET /health
GET /api/v1/plugins
GET /api/v1/skills
GET /api/v1/skills/{slug-or-version}
```

## Knowledge / RAG 与阿里云 OSS

开发环境默认使用 `KNOWLEDGE_OBJECT_STORE=local`，保留与 OSS 相同的上传授权、完成确认和索引契约。生产环境设置：

```dotenv
KNOWLEDGE_OBJECT_STORE=oss
ALIYUN_OSS_BUCKET=jie-agent-file
ALIYUN_OSS_REGION=cn-beijing
ALIYUN_OSS_USE_INTERNAL_ENDPOINT=true
```

服务使用阿里云默认凭据链，支持 RAM Role、OIDC、STS 和环境凭据；不会把 AK/SK 写入数据库、Agent Revision、Execution Plan、Checkpoint 或事件。数据库保存 `bucket`、`region`、`object_key`、`version_id` 和稳定 `oss://` URI，上传和下载 URL 均按需短期签名。

Knowledge API 的主要流程：

```text
POST /api/v1/knowledge-bases
POST /api/v1/knowledge-bases/{id}/documents:prepare-upload
PUT  <returned upload URL>
POST /api/v1/knowledge-document-versions/{id}:complete
GET  /api/v1/knowledge-ingestion-jobs/{id}
POST /api/v1/knowledge:search
```

`KNOWLEDGE_EMBEDDING_PROVIDER=hash` 是无需外部凭据的确定性参考实现。生产环境应配置 `openai_compatible` 并通过模型网关提供 Embedding endpoint、模型与维度；这些参数会被锁入 Knowledge Revision。

上传准备请求必须携带文件 SHA-256。每个新 Knowledge Revision 的 `index_hash` 覆盖文档摘要、Chunk 内容摘要和向量摘要；运行时会校验 Plan 中锁定的模型、维度、Retrieval Profile 与索引哈希。旧版不可验证索引必须重新摄取后才能使用。

运行时身份不会从用户提交的 Run metadata 读取。默认部署既拒绝匿名 demo owner，也拒绝调用者自带的 `X-Tenant-ID`、`X-Project-ID`、`X-User-ID` 和 `X-Roles`；只有在受信认证代理会清理并完整重新注入这些头时，才可显式设置 `DEEPAGENT_TRUST_IDENTITY_HEADERS=true`。纯本地演示可单独设置 `DEEPAGENT_ALLOW_DEMO_IDENTITY=true`，不得用于生产环境。

## 真实对话模型

Playground 通过统一模型适配层调用真实模型，支持 OpenAI-compatible Chat Completions、OpenAI Responses 和 Anthropic Messages 三种流式接口。服务启动时按“进程环境变量 → 显式 `DEEPAGENT_ENV_FILE` → 项目 `.env` → 工作区上级 `.env`”的优先关系解析以下配置，密钥只保留在运行时内存中，不写入数据库、Execution Plan、事件或 Artifact：

```dotenv
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_API_KEY=your-secret-key
MODEL=your-model-id
MODEL_API_STYLE=chat_completions
MODEL_MAX_COMPLETION_TOKENS=4096
MODEL_REASONING_SPLIT=true
MODEL_REASONING_SUMMARY=auto
MODEL_ANTHROPIC_THINKING_MODE=enabled
MODEL_ANTHROPIC_THINKING_BUDGET_TOKENS=2048
```

`MODEL_API_STYLE` 可设为 `chat_completions`、`responses` 或 `anthropic_messages`。标准 Anthropic 地址会自动使用 `x-api-key`，兼容网关默认使用 Bearer；特殊网关可通过 `MODEL_AUTH_STYLE=bearer|anthropic` 显式覆盖。完整参数见 `.env.example`。

每个会话会把同一 Thread 中受字符预算约束的最近成功轮次、当前 Agent system prompt、锁定的 Skill 指令和可用的 Knowledge 引用一起发送给模型。知识引用以低权限、不可信 JSON 数据传入，最终 Citation 会与本次召回 ID 校验。适配层把 `reasoning_details` / `reasoning_content`、Responses reasoning 事件和 Anthropic `thinking_delta` 统一映射为 `model.reasoning.started`、`model.reasoning.delta`、`model.reasoning.completed`，普通回答映射为 `model.delta`。Playground 会实时 Markdown 渲染思考面板和最终回答，完整原始事件仍可在 Live Execution 中查看。供应商返回的 token 使用量会写入 Usage Ledger。正式运行缺少模型配置时会拒绝启动，不会静默回退到固定模拟回复。

## 当前边界

本实现交付文档定义的 Phase 1 核心运行骨架、真实 OpenAI-compatible 对话模型、Knowledge/RAG 链路、阿里云 OSS 适配，以及 Coding Agent 的 Docker `patch_only` MVP。Kubernetes Sandbox、受控 Git Credential/Commit/Push/PR、真实 MCP Session、PostgreSQL/pgvector 大规模索引和 Dynamic/Async 写入型 SubAgent 保留在后续阶段；对应领域边界已预留，不会用不安全的宿主 Shell 或读写 HostPath 冒充实现。
