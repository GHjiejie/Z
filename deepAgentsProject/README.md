# DeepAgent Platform

这是 `DESGIN.md` 架构方案的可运行实现，正在进行企业化加固。项目采用模块化单仓库：FastAPI 提供控制面和 Native API，React 提供可视化控制台；本地使用 SQLite，生产适配器使用 PostgreSQL、持久队列、独立 Worker 与远程沙箱服务。当前尚未完成全部生产验收，部署边界及待验证项见[生产隔离说明](docs/enterprise-isolation.md)。

生产镜像、带哈希的依赖锁、严格 CI 门禁、部署预检和 Worker 任务监督见[生产交付说明](docs/production-delivery.md)。Schema 18 增加[独立取消收尾](docs/cancellation-finalization.md)，原取消产物缺失问题已有修复与专项验证；完整生产镜像、目标 Linux 及外部依赖验收仍未完成，不应直接上线。

Schema 19 新增请求与持久任务追踪关联，配套脱敏 JSON 日志、受独立凭据保护的指标、OTLP 导出及告警规则。生产新增配置与迁移要求见[可观测性说明](docs/observability.md)；采集平台、实际通知送达和防篡改审计仍待部署验收。

新增[加密备份与隔离恢复工具](docs/disaster-recovery.md)：同快照备份 PostgreSQL/Checkpoint 与固定版本对象，恢复只新建隔离库并复制到不同 bucket；不覆盖业务库，也不自动接管旧任务。异地调度、密钥托管、生产激活和业务 RPO/RTO 演练仍待完成。

Schema 20 增加[上传与扫描容量治理](docs/upload-governance.md)：上传意图/逻辑保留字节额度、过期处理、OSS 精确长度签名及跨进程摄取并发限制。文件仅在持久 Worker 中扫描；上传完成返回 202 表示排队校验，不表示已经安全。物理对象版本回收、真实 OSS 链路和生产容量验收仍待完成。

[仓库网络边界](docs/repository-network.md)增加实际连接的 IP/origin 固定、TLS/主机密钥校验、Git 后续操作禁网及进程退出清理。生产远程仓库需显式批准 origin；公共 CA/SSH 主机公钥可通过受限 overlay 提供，不加载宿主凭据。快照授权、事务、去重与回收仍需继续完善。

## 已实现功能

- Agent Draft → immutable Revision → ResolvedExecutionPlan → Deployment 完整发布链路；
- Prompt、模型部署、能力、策略、预算和 Runtime Image 的依赖锁定与 Plan Hash；
- 按不可变模型 profile 绑定普通/Coding 执行器，支持模型注册、停用和跨项目隔离；见 [模型治理](docs/model-governance.md)；
- 知识任务、事件及版本元数据继承文档权限，租约和身份快照不外传；见 [资源访问边界](docs/resource-access.md)；
- 生产发布/回滚使用显式环境授权、独立审批、原子渠道/路由切换与审计；提供发布控制台，见 [生产发布](docs/production-releases.md)；
- 生产路由配置与回滚同样要求独立审批；Settings 可申请、审核、取消及查看历史。旧路由升级后需重新审核，见 [生产路由治理](docs/production-routing.md)；
- Run、意图分类与 Embedding 统一预占/结算，租户/项目/用户/模型额度和管理员人工对账 API；见 [费用治理](docs/model-budget.md)；
- 缓存式存活/就绪检查，任务与摄取的租户/项目/用户容量保护，事务拒绝及重试内容绑定；见 [健康与容量](docs/operational-readiness.md)；
- Thread、Run、RunAttempt、Worker Lease、Checkpoint 和持久 RuntimeEvent；
- SSE 流式事件及 `after_sequence` / `Last-Event-ID` 断线续传；
- HITL 暂停、Approve / Edit / Reject / Respond、乐观锁与幂等决策；
- 审批后创建新 Attempt，并使用原 Plan Hash 从 Checkpoint 恢复；
- Coding 使用成对封存的图/文件状态恢复，每个 Attempt 隔离图会话，防止混入后续未提交的工具结果；实际保障及待验收项见 [执行恢复边界](docs/runtime-reliability.md)；
- Artifact、Usage Ledger、模型/工具/SubAgent/审批事件和租户隔离；
- React 控制台：Overview、Agent Builder、Playground、Runs & Traces、Approvals、Resources；
- 账号治理：平台超级管理员 / 租户管理员 / 普通用户三级边界、分页检索、乐观锁、带原因软删除、账号变更审计、首次及过期密码强制修改、登录锁定与限流、服务端会话查看和踢下线；
- 账号变更、会话撤销与审计同事务提交；并发登录/改密、撤权后的旧请求重新核验，权限或所属范围变化要求重新登录。见 [账号一致性契约](docs/account-consistency.md)；
- 计费、模型、评测、Agent 编辑/发布、ChangeSet 和非生产路由的敏感写入与账号撤权串行化，事务内复核当前身份，审计失败完整回滚；范围与剩余入口见 [管理写入一致性](docs/management-write-consistency.md)；
- 控制台主路由精简为 **Playground** 与 **Knowledge Base**，Overview、Agents、Coding、Runs、Approvals、Resources、Settings 和 Users 统一归入 `/advanced/*`；
- 可显式注入确定性测试网关，便于验证平台契约；生产不会自动使用模拟回复；
- Deep Agents Harness Adapter 与 Runtime Binder 边界，真实 SDK 接入不会侵入 Controller。
- 内置 Plugin / Skill Registry：启动发现、幂等注册、版本锁定、Artifact Hash 校验与运行时加载。
- Knowledge/RAG：内置 `builtin_rag` Agent 自动判断知识检索或模型直答，支持 OSS 直传授权、持久摄取、不可变索引、混合检索、ACL 与可验证 Citation。
- 知识库创建/上传准备使用原子事务，支持 `Idempotency-Key` 防止重复创建；控制台保留失败操作的重试标识，不保存上传凭据。见 [知识写入一致性](docs/knowledge-write-consistency.md)。
- Coding Agent：启动时幂等预置并部署 `Built-in Coding Agent`，使用真实 `create_deep_agent()` / LangGraph Tool Loop、内容寻址源码快照、Thread-scoped Workspace、Docker Sandbox、HITL、平台重算 Patch/Diff/Verification 和 Coding Workbench。
- Intent Router：Playground 新会话默认选择 `Auto`，仅对首条输入进行意图识别并路由到 Coding、Release、Knowledge 或 General Agent；低置信度要求确认，Coding 路由要求先绑定工作目录，后续消息固定使用 Thread 已选部署。

## 快速启动

```bash
cd /Users/zhengjie/Github/Z/deepAgentsProject
make install
make build
make api
```

Coding Agent 还要求本机 Docker daemon 可用。第一次发布默认 Docker Profile 时，控制面会构建（或复用）`deepagent/coding-runtime:0.1.0`，解析真实 OCI image digest，并把它锁入 Execution Plan；运行时若镜像摘要不匹配会拒绝启动。

本地工作目录不要求远端仓库链接，也不要求 Git。可在 `.env` 的 `DEEPAGENT_REPOSITORY_ROOTS` 中用操作系统 path separator 配置一个或多个允许选择的目录根；留空时默认限制在当前项目工作区范围内。

打开 [http://localhost:8000](http://localhost:8000)。API 文档位于 [http://localhost:8000/docs](http://localhost:8000/docs)。

本地首次登录使用内置超级管理员：用户名 `admin`，初始密码 `Console1@`。首次登录后系统会强制修改密码；该账号只在不存在时创建，重启不会覆盖已经修改的密码。生产环境首次启动前必须通过 `DEEPAGENT_BOOTSTRAP_ADMIN_PASSWORD_FILE` 挂载一次性高强度初始密码，并在 HTTPS 部署中设置 `DEEPAGENT_SESSION_COOKIE_SECURE=true`。

`DEEPAGENT_ALLOW_DEMO_IDENTITY` 仅保留给自动化测试或无前端的本地 API 演示；正常控制台必须登录，生产环境不得启用该开关。

开发模式可分别启动：

```bash
make api
make web
```

React 开发服务器为 [http://localhost:5173](http://localhost:5173)，请求会代理到 `:8000`。

## 演示核心闭环

1. 登录后进入 **Advanced features → Agents** 修改 Draft，执行 Validate、Publish；系统创建不可变 Revision、Plan 和 Development Deployment。
2. 在 **Playground** 保持 `Auto` 并输入首条任务。系统会展示意图、置信度和目标 Agent；需要仓库或人工确认时先弹出确认窗口，创建会话后不再自动切换 Agent。
3. 输入含有 `deploy to production` 或“部署到生产”的任务，Run 会进入 `WAITING_FOR_APPROVAL`。
4. 在 **Advanced features → Approvals** 批准或拒绝。批准会产生第二个 RunAttempt，从持久 Checkpoint 恢复并完成。
5. 在 **Advanced features → Runs & traces** 查看 Plan Pin、Attempt、完整事件序列和成本。
6. 在 **Knowledge** 创建知识库、上传文件、等待索引完成并测试带 Citation 的检索；把生效的 Knowledge Revision 绑定到 Agent 后，内置 RAG Agent 会对事实型请求检索，对创作等无需知识库的请求自动走模型直答。
7. 在 **Advanced features → Coding Workbench** 使用已发布、已部署的 `Built-in Coding Agent`；点击 `Choose folder` 在允许的本地根目录内选择任意工作目录。Git 是可选能力：Git 目录会自动识别分支，普通目录会创建内容寻址的工作树快照。也可以在 **Agents** 用 `Coding Agent starter` 创建自定义实例。Workbench 会展示只读源码、实时事件、命令证据、Diff、验证结果、审批和 Patch。

## 测试与构建

```bash
make verify
```

集成测试覆盖：

以下列出测试范围，不表示每个环境均已验收。真实 Docker 用例依赖可用的执行环境，跳过不算通过；最新验证结果和剩余工作见 [企业化实施状态](docs/enterprise-hardening-status.md)。

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
- 首轮意图分类、置信度确认、工作区要求、手动覆盖、Shadow Mode、路由版本和 Tenant / Project 隔离。
- 用户治理的三级权限边界、分页检索、乐观锁、软删除审计、密码过期与首次改密、登录锁定/限流、会话撤销及旧库版本化迁移。

## 代码结构

```text
apps/
├── platform_api/          FastAPI Native API 与进程装配
└── web/                   React + TypeScript 控制台
packages/
├── domain/                稳定领域模型
├── application/           Agent 发布与审批用例
├── auth/                  用户、密码哈希、会话与超级管理员规则
├── compiler/              静态验证、依赖锁定、Plan Hash
├── runtime/               Orchestrator、Lease、Binder、Executor、Event
├── knowledge/             OSS、摄取、解析、分块、Embedding、检索与 Citation
├── repositories/          Repository 注册与不可变源码快照
├── routing/               首轮意图分类、版本化策略与可信部署选择
├── sandbox/               Docker/Fake Provider、策略、恢复与生命周期
├── coding/                Workspace、Verification、ChangeSet 与 API 用例
├── adapters/harness/      Deep Agents 稳定适配边界
└── persistence/           SQLite 参考仓储与 Schema
tests/                      端到端平台契约测试
```

## 首轮意图识别与 Agent 路由

Playground 的 `Auto` 模式只在创建新 Thread 时识别首条用户输入。分类器先处理明确规则，再对不明确请求调用受约束的语义分类；分类调用不携带工具、资源凭据或部署选择权。分类输出只描述 `coding`、`release`、`knowledge`、`general` 或 `ambiguous` 意图，最终部署由服务端根据当前 Tenant、Project、Environment、不可变 Execution Plan 能力和生效 Router Revision 选择。

- 高置信度请求直接创建 routed run；低于自动阈值或属于 `ambiguous` 时要求用户确认。
- Coding Agent 必须绑定已注册的 Repository / Working Directory，不能在没有 Workspace 的情况下执行。
- 用户可手动选择或覆盖 Agent，覆盖结果会进入路由决策和 RuntimeEvent 审计记录。
- Thread 创建后会固定 `agent_deployment_id`；后续输入直接在该 Thread 上创建 Run，不再次分类或静默切换。
- `active` 模式执行预测结果；`shadow` 模式保留预测但执行 General Agent；`disabled` 模式跳过自动路由并使用 General Agent。
- Router 配置是不可变修订。Settings 中 owner/admin 可调整模式、阈值、决策有效期及各意图目标，普通成员只能查看。

主要接口：

```text
GET  /api/v1/intent-routing/profile
PUT  /api/v1/intent-routing/profile
POST /api/v1/intent-routing:resolve
POST /api/v1/routed-runs
GET  /api/v1/intent-routing/decisions
GET  /api/v1/intent-routing/decisions/{id}
```

每个 committed decision 会关联到 Thread 和首个 Run，并产生 `intent.classification.*`、`routing.agent.selected`、`routing.fallback`、`routing.user_overridden` 或 `routing.workspace_required` 事件。输入正文不重复写入决策表，只保存 SHA-256 用于提交时的一致性和幂等校验。模型分类超时可通过 `INTENT_CLASSIFIER_TIMEOUT_SECONDS` 调整，默认 4 秒；超时或无效输出会降级为需要确认的 General 路由。

## Reference Harness 与真实 Deep Agents

普通 Agent 使用受治理的模型/RAG 执行器；测试可显式注入确定性网关。普通执行器不再模拟 SubAgent 调用和完成记录；尚未执行的 SubAgent 绑定会明确报告不可用，并阻止其作为生产评测证据。`coding-agent-v1` 已接入锁定版本的真实 `create_deep_agent()`、持久 LangGraph Checkpointer 和事件适配器；文件与命令结果来自 Sandbox，Diff、Verification 和 Artifact 由平台重新计算，不能由模型自行声明。

真实 Credential 禁止进入 Revision、Plan、Checkpoint、Event 或 Prompt。Coding MVP 为 `patch_only`，明确禁止 Commit、Push、PR 和 Deploy，也不会把宿主目录读写挂载进容器。

## 评测与生产发布

评测接口现在读取实际 Run 证据，不再返回固定分数。管理员定义不可变评测集和项目级发布策略；发布人员按用例执行后提交 Run 映射，由服务端计算结果。生产部署及已有生产部署的新 Run 会检查最新结果、计划/评测集绑定、证据真实性和有效期。模拟结果、最新失败结果与过期样本不能用于生产。

使用流程、权限、API 和尚未完成的自动编排/语义评测边界见 [evaluation-gates.md](docs/evaluation-gates.md)。这是一项发布条件，不代表所有生产安全、运维和容量验收已经完成。

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

控制台登录使用服务端会话：浏览器只接收 `HttpOnly` Cookie，无法从前端脚本读取令牌；API 客户端也可使用登录响应中的 Bearer Token。数据库只保存 PBKDF2-SHA256 密码哈希和 SHA-256 会话摘要。退出登录、禁用账号或由管理员重置密码时，服务端会撤销相关会话。

平台超级管理员可跨租户管理用户；拥有 `tenant_admin` 角色的租户管理员只能管理本租户的非超级管理员；普通用户只能修改自己的密码和管理自己的会话。所有用户变更都要求当前 `version`，过期提交返回 `409`。账号停用必须提供原因并保留 `deleted_at`、`deleted_by` 与审计事件。主要接口：

```text
POST   /api/v1/auth/login
POST   /api/v1/auth/logout
GET    /api/v1/auth/me
PUT    /api/v1/auth/password
GET    /api/v1/auth/sessions
DELETE /api/v1/auth/sessions/{session_id}
DELETE /api/v1/auth/sessions
GET    /api/v1/users
POST   /api/v1/users
GET    /api/v1/users/audit-events
PATCH  /api/v1/users/{id}
PUT    /api/v1/users/{id}/password
DELETE /api/v1/users/{id}             # 请求体包含 version 和停用原因
GET    /api/v1/users/{id}/sessions
DELETE /api/v1/users/{id}/sessions/{session_id}
DELETE /api/v1/users/{id}/sessions
```

密码有效期、失败次数、锁定时间、限流窗口和会话活动写入频率可分别通过 `DEEPAGENT_PASSWORD_MAX_AGE_DAYS`、`DEEPAGENT_MAX_FAILED_LOGINS`、`DEEPAGENT_LOGIN_LOCKOUT_MINUTES`、`DEEPAGENT_LOGIN_RATE_WINDOW_MINUTES` 和 `DEEPAGENT_SESSION_LAST_SEEN_SECONDS` 调整。数据库启动时按 `schema_migrations` 顺序执行增量迁移。

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
