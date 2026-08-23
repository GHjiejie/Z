# DeepAgent Platform 最终架构设计方案

**文档版本：** v1.0
**文档状态：** 最终架构评审版
**设计日期：** 2026-08-24
**适用范围：** 多租户 LLM / Agent 开发、发布、运行与治理平台

---

## 1. 执行摘要

DeepAgent Platform 不是一个在 `create_deep_agent()` 外面包装 HTTP 接口的聊天系统，而是一个完整的：

> **LLM / Agent 开发、发布、运行、治理与可观测平台。**

原始方案中，将 Model、Tool、MCP、Skill、Memory、SubAgent、Sandbox、HITL、Checkpoint、Streaming 等能力进行配置化、持久化、可视化和多租户化的方向保持不变。

最终架构对原方案做出一个关键修正：

```text
Deep Agents ≠ Runtime

Deep Agents = Agent Harness
LangChain   = Agent Abstraction
LangGraph   = Execution Runtime
```

官方架构同样将三层关系定义为：

```text
Deep Agents    → opinionated harness
LangChain      → model + tools + middleware → agent loop
LangGraph      → state + checkpoint + streaming + interrupt
```

`create_deep_agent()` 最终仍然通过 LangChain 构建 Runnable Graph，并由 LangGraph 执行。

因此，本平台的核心链路最终确定为：

```text
AgentDraft
    ↓
AgentRevision
    ↓
ResolvedExecutionPlan
    ↓
AgentDeployment
    ↓
Thread / Run
    ↓
Run Orchestrator
    ↓
Runtime Worker
    ↓
DeepAgentsHarnessAdapter
    ↓
LangGraph Runtime
```

Deep Agents 是平台支持的第一个 Harness，而不是平台本身，也不是与 LangGraph 并列的运行时。

---

# 2. 平台目标与非目标

## 2.1 平台目标

平台需要实现以下完整闭环：

| 能力          | 平台职责                                             |
| ----------- | ------------------------------------------------ |
| Agent 创建    | 通过控制台或 API 配置 Agent                              |
| Agent 版本    | 不可变 Revision、依赖快照、回滚                             |
| Agent 发布    | 校验、编译、评测、部署                                      |
| Agent 运行    | 长任务、流式输出、并发控制                                    |
| Deep Agents | Filesystem、Skills、Memory、SubAgent、HITL、Sandbox 等 |
| 模型治理        | 多 Provider、多 Endpoint、路由、限流、计费                   |
| 工具治理        | Tool、MCP、数据库、内部 API 统一调用                         |
| 多租户         | Tenant、Project、Environment、Namespace 隔离          |
| 安全治理        | 权限、审批、凭据、Sandbox、审计                              |
| 故障恢复        | Checkpoint、重试、Worker 接管、Run 恢复                   |
| 可观测性        | Trace、Event、日志、指标、成本、Token                       |
| 协议兼容        | Native API、OpenAI、Anthropic、ACP、A2A              |

## 2.2 非目标

第一阶段不追求：

1. 将平台直接做成任意业务流程的通用低代码 Workflow 引擎；
2. 对外部副作用承诺绝对的 Exactly Once；
3. 允许用户在平台 UI 中上传并执行任意 Python 代码；
4. 一开始就兼容所有 Agent 框架；
5. 将所有模块拆成大量独立微服务；
6. 让模型自身承担安全边界。

---

# 3. 核心架构原则

## 3.1 Control Plane 与 Runtime Plane 分离

```text
Control Plane
= Agent 是什么、允许做什么、发布哪个版本

Runtime Plane
= Agent 在哪里运行、如何恢复、如何流式输出
```

任何业务 Controller 都不允许直接调用：

```python
create_deep_agent(...)
```

所有 Agent 必须经过统一的：

```text
Revision
→ Compile
→ Plan
→ Deploy
→ Orchestrate
→ Execute
```

## 3.2 配置不可变

以下对象发布后都不可原地修改：

```text
AgentRevision
PromptRevision
ToolRevision
SkillVersion
MemoryRevision
PolicyRevision
SandboxProfileRevision
ResolvedExecutionPlan
```

修改配置必须产生新 Revision。

## 3.3 静态编译与动态绑定分离

发布阶段只解析和锁定配置，不创建 Sandbox、不连接 MCP、不注入真实密钥。

运行阶段再根据 Run Context 动态绑定：

```text
Credential
MCP Session
Sandbox Instance
Checkpointer
Store Namespace
Model Endpoint
```

## 3.4 外部副作用必须幂等

平台采用：

```text
At-least-once dispatch
+
Worker Lease
+
Checkpoint recovery
+
Idempotency Key
```

而不是对外宣称无法可靠保证的 Exactly Once。

## 3.5 平台事件协议不能直接等于 Deep Agents SDK 事件

Deep Agents 事件先通过 Adapter 转换为稳定的平台事件：

```text
Deep Agents Event
        ↓
DeepAgentsEventAdapter
        ↓
Platform RuntimeEvent
        ↓
Event Store / SSE / WebSocket
```

## 3.6 安全边界落在工具和运行环境

模型是否“听话”不是安全机制。

真正的安全边界必须包括：

```text
Tool Visibility
Authorization
HITL
Credential Broker
Tool Gateway
Sandbox
Network Policy
Audit
```

---

# 4. 总体系统架构

```text
┌──────────────────────────────────────────────────────────────────────┐
│                         CLIENT / EXPERIENCE                          │
│                                                                      │
│ Web Console     Agent SDK     CLI     IDE Plugin     External Client │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       API & PROTOCOL LAYER                           │
│                                                                      │
│ Native Agent API                                                     │
│ OpenAI Protocol Adapter                                              │
│ Anthropic Protocol Adapter                                           │
│ ACP Adapter                                                          │
│ A2A Adapter                                                          │
│ Webhook / SSE / WebSocket                                            │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                 ┌──────────────┴──────────────┐
                 │                             │
                 ▼                             ▼
┌──────────────────────────────┐   ┌───────────────────────────────────┐
│        CONTROL PLANE         │   │       EXECUTION CONTROL          │
│                              │   │                                   │
│ Tenant / Project             │   │ Run Orchestrator                  │
│ Agent Registry               │   │ Dispatch Queue                    │
│ Model Deployment Registry    │   │ Worker Lease                      │
│ Prompt Registry              │   │ Retry / Cancel / Resume           │
│ Tool Registry                │   │ Quota Reservation                 │
│ MCP Registry                 │   │ Run Scheduling                    │
│ Skill Registry               │   │ RunAttempt Management             │
│ Memory Registry              │   │                                   │
│ Knowledge Registry           │   └─────────────────┬─────────────────┘
│ Policy Registry              │                     │
│ Sandbox Profiles             │                     ▼
│ Evaluation / Release Gate    │   ┌───────────────────────────────────┐
└───────────────┬──────────────┘   │          RUNTIME WORKER           │
                │                  │                                   │
                ▼                  │ RuntimeBinder                     │
┌──────────────────────────────┐   │ ├── Credential Resolver           │
│       COMPILE & RELEASE      │   │ ├── MCP Session Resolver          │
│                              │   │ ├── Sandbox Resolver              │
│ AgentPlanCompiler            │   │ ├── Store / Checkpointer          │
│ Reference Resolver           │   │ └── Runtime Context               │
│ Capability Validator         │   │                                   │
│ Policy Validator             │   │ Executable Builder                │
│ Dependency Locker            │   │ ├── DeepAgentsHarnessAdapter      │
│ Plan Hasher                  │   │ ├── LangChainAgentAdapter         │
│                              │   │ └── CustomLangGraphAdapter        │
│ ResolvedExecutionPlan        │   │                 │                 │
└──────────────────────────────┘   │                 ▼                 │
                                   │          LangGraph Runtime        │
                                   └───────┬─────────┬─────────┬───────┘
                                           │         │         │
                         ┌─────────────────┘         │         └──────────────┐
                         ▼                           ▼                        ▼
              ┌────────────────────┐      ┌────────────────────┐   ┌──────────────────┐
              │   MODEL GATEWAY    │      │    TOOL GATEWAY    │   │ SANDBOX MANAGER  │
              │                    │      │                    │   │                  │
              │ Routing            │      │ Policy Enforcement │   │ K8s Pod / VM     │
              │ Load Balancing     │      │ Credential         │   │ Lifecycle / TTL  │
              │ Fallback           │      │ Tool / MCP Execute │   │ Resource Limits  │
              │ Usage              │      │ Audit / Redaction  │   │ Network Policy   │
              └─────────┬──────────┘      └─────────┬──────────┘   └─────────┬────────┘
                        │                           │                        │
                        ▼                           ▼                        ▼
              Model Providers             MCP / API / DB / SaaS        Sandbox Pods

┌──────────────────────────────────────────────────────────────────────┐
│                             DATA PLANE                               │
│                                                                      │
│ PostgreSQL                                                           │
│ ├── Control Plane Metadata                                           │
│ ├── Run / Attempt / Event / Interrupt                                │
│ └── PostgreSQL Checkpointer / Store                                  │
│                                                                      │
│ Redis                                                                │
│ ├── Cache / Lock / Rate Limit                                        │
│ └── SSE Fan-out / Ephemeral State                                    │
│                                                                      │
│ Durable Queue                                                        │
│ ├── PostgreSQL Queue / Kafka / NATS / Redis Streams Adapter          │
│ └── Run Dispatch                                                     │
│                                                                      │
│ Object Storage       Vector/Search Index       Secret Manager / KMS  │
│ Artifacts / Skills   Knowledge / Embeddings    Credentials           │
└──────────────────────────────────────────────────────────────────────┘
```

---

# 5. 平台核心模块

## 5.1 Experience Layer

前端不应该只提供一个聊天框，而应该包含：

```text
Agent Builder
Model Management
Tool / MCP Management
Skill Management
Memory Management
Knowledge Base
Run Console
SubAgent Tree
Todo / Plan
HITL Approval Center
Sandbox File Browser
Artifact Viewer
Trace / Cost / Token
Evaluation Dashboard
```

Deep Agents 当前前端能力本身也强调将 Coordinator、SubAgent、Todo、Tool Call 和 Sandbox Artifact 分开呈现，而不是把所有内容压缩成一条聊天记录。

## 5.2 API & Protocol Layer

协议层分成两类。

### Native Agent API

用于完整表达：

```text
Thread
Run
RunAttempt
SubAgent
Todo
Interrupt
Decision
Artifact
Sandbox
Usage
Event
```

### Compatibility Adapter

用于兼容第三方客户端：

```text
OpenAI Chat Completions
OpenAI Responses
Anthropic Messages
ACP
A2A
```

兼容协议只是转换层，不允许协议对象进入核心 Domain Model。

## 5.3 Control Plane

Control Plane 负责：

```text
配置
版本
权限
发布
评测
治理
```

它不直接执行 Agent。

## 5.4 Compile & Release Plane

Compile Plane 将用户可编辑配置解析成一个不可变、可以执行的计划。

```text
AgentRevision
    ↓
Schema Validation
    ↓
Reference Resolution
    ↓
Capability Validation
    ↓
Policy Validation
    ↓
Dependency Lock
    ↓
Plan Hash
    ↓
ResolvedExecutionPlan
```

## 5.5 Execution Control Plane

负责长任务调度：

```text
创建 Run
预留配额
生成 RunAttempt
写入 Dispatch Outbox
发布任务
分配 Worker
维护 Lease
检测心跳
处理取消
处理重试
处理恢复
```

## 5.6 Runtime Worker

Runtime Worker 才是真正运行 Agent 的进程。

它不负责 Agent 配置管理，只接收：

```text
run_id
attempt_id
resolved_plan_id
runtime_context
```

## 5.7 Model Gateway

统一处理：

```text
Provider Protocol
Endpoint Routing
Load Balancing
Fallback
Timeout
Retry
Token Accounting
Cost Accounting
Rate Limit
Content Filtering
Trace
```

## 5.8 Tool Gateway

所有高风险 Tool、MCP、数据库和外部写操作均通过 Tool Gateway。

```text
Tool Call
   ↓
Visibility Check
   ↓
Authorization
   ↓
Risk Classification
   ↓
HITL Decision
   ↓
Credential Injection
   ↓
Execution
   ↓
Output Inspection
   ↓
Redaction
   ↓
Audit
```

## 5.9 Sandbox Manager

负责 Sandbox 的：

```text
创建
恢复
复用
冻结
销毁
TTL
资源限制
文件上传下载
网络策略
镜像白名单
```

---

# 6. 核心领域模型

## 6.1 Agent 生命周期

```text
Agent
  │
  ├── AgentDraft
  │       可编辑
  │
  ├── AgentRevision
  │       发布后不可变
  │
  ├── ResolvedExecutionPlan
  │       完整依赖锁定
  │
  └── AgentDeployment
          dev / staging / production
```

运行时：

```text
AgentDeployment
        ↓
Thread
        ↓
Run
        ↓
RunAttempt
        ↓
RunSnapshot
```

## 6.2 AgentRevisionSpec

```python
from typing import Literal
from pydantic import BaseModel, Field


class RevisionRef(BaseModel):
    resource_id: str
    revision_id: str


class HarnessRef(BaseModel):
    type: Literal[
        "deepagents",
        "langchain_agent",
        "custom_langgraph",
    ]
    profile_revision_id: str


class CapabilityBindings(BaseModel):
    tools: list[RevisionRef] = Field(default_factory=list)
    mcp_servers: list[RevisionRef] = Field(default_factory=list)
    skills: list[RevisionRef] = Field(default_factory=list)
    memories: list[RevisionRef] = Field(default_factory=list)
    knowledge_bases: list[RevisionRef] = Field(default_factory=list)
    subagents: list[RevisionRef] = Field(default_factory=list)
    middleware: list[RevisionRef] = Field(default_factory=list)


class PolicyBindings(BaseModel):
    permission_policy: RevisionRef
    approval_policy: RevisionRef | None = None
    data_policy: RevisionRef | None = None
    network_policy: RevisionRef | None = None


class RunLimits(BaseModel):
    max_duration_seconds: int
    max_model_calls: int
    max_tool_calls: int
    max_subagent_depth: int
    max_subagent_concurrency: int
    max_sandbox_cpu_seconds: int
    max_output_bytes: int
    max_cost: float | None = None


class AgentRevisionSpec(BaseModel):
    harness: HarnessRef

    model_deployment: RevisionRef
    prompt: RevisionRef

    capabilities: CapabilityBindings = Field(
        default_factory=CapabilityBindings
    )

    workspace_profile: RevisionRef | None = None
    execution_environment: RevisionRef | None = None

    policies: PolicyBindings
    limits: RunLimits

    output_contract: RevisionRef | None = None
    state_schema: RevisionRef | None = None
    context_schema: RevisionRef | None = None

    engine_config: dict[str, object] = Field(
        default_factory=dict
    )
```

## 6.3 不允许直接暴露 Python Class

以下字段不能让用户在 UI 中填写 Python 导入路径：

```text
state_schema
context_schema
middleware
tool implementation
```

平台应提供：

```text
Schema Registry
Plugin Registry
JSON Schema
Signed Code Artifact
Version
Artifact Hash
```

只允许使用已经审核和注册的代码插件。

---

# 7. ResolvedExecutionPlan

## 7.1 为什么 AgentRevision 还不够

AgentRevision 可能只保存：

```text
model = qwen-prod
tool = github
skill = frontend-review
policy = production-default
```

如果这些资源后来被原地更新，历史 AgentRevision 仍然无法复现。

因此发布后必须产生完整依赖快照。

## 7.2 结构设计

```python
class ResolvedExecutionPlan(BaseModel):
    id: str

    agent_revision_id: str

    harness_type: str
    harness_adapter_version: str
    harness_profile_revision_id: str

    runtime_image_digest: str

    model_deployment_revision_id: str
    prompt_revision_id: str
    prompt_hash: str

    tool_bindings: list[dict[str, object]]
    mcp_bindings: list[dict[str, object]]
    skill_versions: list[dict[str, object]]
    memory_versions: list[dict[str, object]]
    knowledge_bindings: list[dict[str, object]]
    subagent_bindings: list[dict[str, object]]

    workspace_profile_revision_id: str | None
    sandbox_profile_revision_id: str | None

    permission_policy_revision_id: str
    approval_policy_revision_id: str | None

    middleware_plan: list[dict[str, object]]

    output_schema_revision_id: str | None
    state_schema_revision_id: str | None
    context_schema_revision_id: str | None

    limits: RunLimits

    plan_hash: str
```

## 7.3 必须锁定的内容

```text
Deep Agents 版本
LangChain 版本
LangGraph 版本
Runtime 镜像 Digest
Harness Adapter 版本
Model Deployment 版本
Prompt 内容与 Hash
Tool Schema 与实现版本
MCP Tool Discovery Snapshot
Skill Artifact Hash
Memory Revision
SubAgent Revision
Middleware 顺序
Backend Profile
Sandbox Profile
Permission Policy
Output Schema
```

## 7.4 RunSnapshot

每次运行还需要记录真正使用的动态信息：

```text
resolved_plan_id
resolved_plan_hash
runtime_image_digest
worker_pool
worker_id
model_endpoint_id
model_route
fallback 是否发生
credential version
sandbox_instance_id
mcp_session_id
feature flags
started_at
```

需要注意：即使平台完整记录了配置，如果外部模型服务商在相同模型别名下替换了模型权重，也无法保证语义结果完全相同。因此平台能够承诺的是：

```text
配置级可追溯
代码级可追溯
资源级可追溯
调用级可审计
```

而不是绝对确定性的模型输出复现。

---

# 8. 两阶段编译体系

## 8.1 发布阶段：AgentPlanCompiler

```text
AgentRevision
       ↓
AgentPlanCompiler
       ├── ModelResolver
       ├── PromptResolver
       ├── ToolResolver
       ├── MCPResolver
       ├── SkillResolver
       ├── MemoryResolver
       ├── KnowledgeResolver
       ├── SubAgentResolver
       ├── WorkspaceResolver
       ├── PolicyResolver
       ├── SchemaResolver
       └── CompatibilityValidator
                 ↓
       ResolvedExecutionPlan
```

发布阶段只产生不可变计划，不允许：

```text
创建 Sandbox
建立 MCP 长连接
读取真实 API Key
创建数据库连接池
创建用户级 Store Namespace
启动 Agent Run
```

## 8.2 运行阶段：RuntimeBinder

```text
ResolvedExecutionPlan
        ↓
RuntimeBinder
        ├── 构建 RuntimeContext
        ├── 获取短期 Credential
        ├── 创建或复用 MCP Session
        ├── 创建或复用 Sandbox
        ├── 解析 Store Namespace
        ├── 绑定 Checkpointer
        ├── 绑定 Model Endpoint
        └── 加载 Feature Flags
                 ↓
AgentGraphFactory
                 ↓
Executable Graph
```

创建 Sandbox、连接 MCP 等操作涉及网络资源与异步生命周期，官方生产建议也采用异步 Graph Factory 处理这些运行时资源。

## 8.3 Harness Adapter

```python
class HarnessAdapter(Protocol):
    async def build_factory(
        self,
        plan: ResolvedExecutionPlan,
    ) -> "AgentGraphFactory":
        ...


class DeepAgentsHarnessAdapter:
    async def build_factory(
        self,
        plan: ResolvedExecutionPlan,
    ) -> "AgentGraphFactory":
        ...
```

DeepAgents Adapter 内部负责：

```text
create_deep_agent()
Middleware 映射
Backend 映射
SubAgent 映射
Memory / Skills 映射
Permission 映射
HITL 映射
Event 映射
```

平台其他模块不直接依赖 Deep Agents 的 Middleware 类和内部顺序。

---

# 9. Harness Compatibility Profile

Deep Agents 的部分能力和 API 会随版本变化，因此必须建立兼容性描述。

```python
class HarnessCompatibilityProfile(BaseModel):
    harness_type: str
    package_version: str
    adapter_version: str

    supported_features: set[str]
    preview_features: set[str]

    config_schema_version: str
    event_adapter_version: str

    runtime_image_digest: str
```

例如：

```json
{
  "harness_type": "deepagents",
  "package_version": "0.x",
  "adapter_version": "1.0.0",
  "supported_features": [
    "filesystem",
    "skills",
    "memory",
    "sync_subagents",
    "hitl",
    "sandbox"
  ],
  "preview_features": [
    "interpreter",
    "dynamic_subagents",
    "async_subagents",
    "event_streaming"
  ]
}
```

截至当前官方文档，Interpreter 和 Dynamic SubAgent 仍标记为 Beta；Async SubAgent 被标记为 Preview，因此这些能力必须通过 Feature Gate 控制，不能直接成为平台永久领域契约。

---

# 10. Model Deployment Registry

## 10.1 模型对象拆分

不能只使用一个简单的 `model` 表。

```text
ModelProvider
    ↓
ModelEndpoint
    ↓
ModelCatalogEntry
    ↓
ModelDeployment
    ↓
ModelDeploymentRevision
    ↓
ModelRoutingPolicy
```

### ModelProvider

```text
OpenAI Compatible
Anthropic Compatible
Google
Ollama
vLLM
自定义 Provider
```

### ModelEndpoint

```text
base_url
region
credential_ref
network_zone
health_status
```

### ModelCatalogEntry

```text
模型名称
上下文长度
支持模态
价格规则
Token 规则
```

### ModelDeployment

表示真正可调用的部署：

```text
qwen-prod-tokyo
qwen-prod-singapore
qwen-cheap
qwen-high-performance
qwen-canary
```

Agent 必须绑定：

```text
model_deployment_revision_id
```

而不是只绑定一个会变化的模型别名。

## 10.2 能力探测

模型能力不能只依赖管理员手填。

```text
Tool Calling Probe
Parallel Tool Call Probe
Structured Output Probe
Streaming Probe
Vision Probe
Audio Probe
Context Window Probe
Reasoning Probe
```

记录：

```text
declared_capabilities
verified_capabilities
last_verified_at
verification_result
```

发布 Agent 时检查：

```text
Agent 要求 Structured Output
             +
Model Deployment 不支持
             ↓
           发布失败
```

## 10.3 Model Gateway

Model Gateway 负责：

```text
协议转换
负载均衡
Endpoint 健康检查
Provider 限流
租户限流
Fallback
熔断
Token 计量
成本计算
Trace 关联
```

一旦发生 Fallback，必须写入 RunSnapshot 和 RunEvent。

---

# 11. Tool 与 MCP 架构

## 11.1 控制面不完全合并

在 Agent Runtime 中，Tool 和 MCP 最终都可以转换成 Agent 可调用能力。

但在控制面中，它们具有不同生命周期。

```text
Capability Catalog
│
├── ToolDefinition
│   ├── Python Tool
│   ├── HTTP Tool
│   ├── Internal API Tool
│   ├── Database Tool
│   └── Remote Tool
│
├── MCPServerDefinition
│   ├── Transport
│   ├── Endpoint
│   ├── Credential
│   ├── Session Policy
│   └── Health Check
│
├── MCPToolSnapshot
│   ├── Name
│   ├── Description
│   ├── Input Schema
│   ├── Output Schema
│   └── Schema Hash
│
└── AgentCapabilityBinding
    ├── Source Type
    ├── Source Revision
    ├── Alias
    ├── Timeout
    ├── Retry
    ├── Risk Level
    └── Approval Policy
```

## 11.2 运行态统一

```text
ToolDefinitionRevision ─┐
                        ├── RuntimeCapabilityResolver
MCPToolSnapshot ────────┘
                                ↓
                         BoundTool / BaseTool
```

## 11.3 MCP Session Manager

MCP Session Policy 支持：

```text
per_call
per_run
per_thread
pooled
```

需要管理：

```text
连接建立
认证
Session 恢复
Tool Discovery
Schema 变更
健康检查
连接池
超时
断线重连
```

## 11.4 Tool Gateway

Tool Gateway 必须实现：

```text
Tool Visibility
RBAC / ABAC
HITL
Credential Injection
Timeout
Retry
Rate Limit
Circuit Breaker
Input Validation
Output Validation
PII Redaction
Audit
Idempotency
```

---

# 12. 统一安全策略

Deep Agents 原生 `permissions=` 只覆盖内置 Filesystem Tool，不覆盖自定义 Tool、MCP Tool，也不覆盖 Sandbox 的 `execute`。

因此平台必须提供独立的统一安全链路。

## 12.1 四层安全模型

### Visibility

```text
模型是否能够看到这个 Tool
```

不可见 Tool 不进入模型 Tool Schema。

### Authorization

```text
当前 Tenant / User / Project 是否允许调用
```

### Approval

```text
调用是否必须人工审批
```

### Isolation

```text
即使调用已经获批，执行环境实际能够做什么
```

## 12.2 Policy Enforcement Chain

```text
Agent Tool Call
      ↓
Capability Visibility Policy
      ↓
Tenant / User Authorization
      ↓
Risk Classification
      ↓
Approval Policy
      ↓
Credential Broker
      ↓
Tool / MCP / Sandbox
      ↓
Output Inspection
      ↓
Audit Event
```

## 12.3 默认策略

平台建议采用：

```text
默认不可见
默认拒绝
显式允许
高风险写操作默认审批
生产环境强制审计
```

这比 Deep Agents Filesystem Permission 的宽松默认更适合多租户平台。

---

# 13. Workspace、Backend 与 Execution Environment

## 13.1 分成两个概念

原始 Backend Profile 应拆分为：

```text
WorkspaceMountProfile
ExecutionEnvironmentProfile
```

### WorkspaceMountProfile

描述文件路径映射和存储范围。

```python
class MountSpec(BaseModel):
    mount_path: str

    source_type: Literal[
        "state",
        "store",
        "object_storage",
        "sandbox",
        "knowledge_result",
    ]

    source_revision_id: str | None

    scope: Literal[
        "run",
        "thread",
        "agent",
        "user",
        "project",
        "tenant",
    ]

    access: Literal[
        "read_only",
        "read_write",
    ]

    quota_bytes: int | None
    retention_policy: str
```

### ExecutionEnvironmentProfile

描述 OS 级执行环境。

```python
class ExecutionEnvironmentProfile(BaseModel):
    provider: str
    image_digest: str

    cpu_limit: str
    memory_limit: str
    disk_limit: str

    timeout_seconds: int

    network_policy_revision_id: str
    service_account_profile_id: str

    lifecycle_scope: Literal[
        "run",
        "thread",
        "agent",
    ]
```

## 13.2 推荐虚拟目录

```text
/
├── workspace/       当前代码和工作区
├── memory/          长期记忆
├── skills/          Skill 文件
├── knowledge/       检索结果
├── artifacts/       运行产物
├── conversation/    会话归档
├── tool-results/    大型 Tool Result
└── tmp/             Run 临时状态
```

路径背后可以映射为：

```text
/workspace     → Sandbox
/memory        → LangGraph Store
/skills        → Object Storage
/knowledge     → Retrieval Result Mount
/artifacts     → Object Storage
/tool-results  → State / Object Storage
/tmp           → LangGraph State
```

## 13.3 Checkpointer 与 Store 区分

```text
Checkpointer
= Thread 内执行状态、恢复、HITL、短期会话状态

Store
= 跨 Thread 的长期记忆和持久数据
```

这是 LangGraph 官方定义的两类不同持久化机制。

多租户 Store Namespace 必须至少包含：

```text
tenant_id
project_id
user_id
agent_id
resource_type
```

官方生产建议同样要求多用户场景显式配置 Namespace Factory，避免不同用户共享同一 Store 空间。

---

# 14. Memory、Skill 与 Knowledge

三者必须独立。

| 类型        | 表达内容            | 加载方式                   | 典型范围                 |
| --------- | --------------- | ---------------------- | -------------------- |
| Memory    | 用户偏好、项目约定、长期上下文 | 启动或按需                  | User / Project       |
| Skill     | 如何完成某种任务        | Progressive Disclosure | Agent / Organization |
| Knowledge | 完成任务所需事实        | Retriever Tool         | Corpus / Project     |

```text
Memory
= 我是谁、用户是谁、项目有什么约定

Skill
= 某类任务应该怎么做

Knowledge
= 当前任务需要查询哪些事实
```

Knowledge Base 不等于 Object Storage。

完整关系是：

```text
Raw Documents
      ↓
Object Storage
      ↓
Parser / Chunker
      ↓
Vector / Search Index
      ↓
Retriever Tool
      ↓
Agent
```

---

# 15. Sandbox 与 Interpreter

## 15.1 Interpreter

适合：

```text
循环
分支
排序
聚合
批处理
数据转换
Programmatic Tool Calling
Dynamic SubAgent
```

不应视为 OS Sandbox。

## 15.2 Sandbox

适合：

```text
Shell
Git
Python
Go
Node.js
Build
Test
Package Install
文件系统修改
CLI 调用
```

官方同样明确区分 Interpreter 和 Sandbox：Interpreter 是 Agent Loop 内的受限编程环境，Sandbox 是执行 Shell、依赖安装、测试和 OS 文件操作的环境。

## 15.3 Kubernetes Sandbox

推荐架构：

```text
Runtime Worker
      │
      │ Sandbox API
      ▼
Sandbox Manager
      │
      │ Kubernetes API
      ▼
sandbox-{instance-id} Pod
```

Sandbox Pod 必须满足：

```text
独立 Namespace
独立 ServiceAccount
非 Root 用户
只读 RootFS
无 HostPath
禁用 Privileged
Seccomp / AppArmor
ResourceQuota
LimitRange
NetworkPolicy
Pod TTL
镜像白名单
最大运行时间
最大磁盘
最大进程数
```

生产环境需要 Shell 或 OS 文件系统时，应使用隔离 Sandbox，而不是在 API 或 Runtime Worker 中直接执行 `subprocess.run()`。官方 Backend 安全文档也建议生产环境将文件和 Shell 操作放入 Sandbox。

## 15.4 Sandbox 生命周期

支持：

```text
run-scoped
thread-scoped
agent-scoped
```

默认推荐：

```text
普通任务      → run-scoped
连续编码会话  → thread-scoped
长期开发环境  → agent-scoped
```

官方示例通常采用 Thread Scoped Sandbox，在同一 Thread 的后续 Run 中复用，并通过 TTL 回收。

---

# 16. SubAgent 正交模型

不能再将：

```text
Sync
Async
Compiled
Dynamic
```

当成同一维度。

## 16.1 SubAgentBinding

```python
class SubAgentBinding(BaseModel):
    target_kind: Literal[
        "agent_revision",
        "workflow_revision",
        "remote_agent",
    ]

    execution_mode: Literal[
        "sync",
        "async",
    ]

    orchestration_mode: Literal[
        "model_tool_call",
        "interpreter",
        "fixed_workflow",
    ]

    transport: Literal[
        "in_process",
        "asgi",
        "http",
    ]

    state_scope: Literal[
        "invocation",
        "thread",
    ]

    timeout_seconds: int
    max_concurrency: int
```

## 16.2 可表达组合

```text
DeepAgent + Sync + Model Tool Call
Compiled LangGraph + Sync
DeepAgent + Dynamic Interpreter Fan-out
Remote Agent + Async + HTTP
Co-deployed Agent + Async + ASGI
Workflow + Fixed Orchestration
```

## 16.3 Run 与 SubAgent 的关系

定义：

```text
Run
= 独立可调度的执行单元

ExecutionSpan
= Run 内部的 Agent / SubAgent / Tool / Model 执行片段
```

同步 SubAgent 默认表示为：

```text
同一个 Run
+
独立 ExecutionSpan
```

异步或远程 SubAgent 表示为：

```text
Parent Run
    ↓
RunRelation
    ↓
Child Run
```

这样不会强制每一个同步 `task()` 都变成独立调度任务。

## 16.4 动态和异步能力

Dynamic SubAgent 依赖 Interpreter；Async SubAgent 会独立维护 Thread，并通过 Agent Protocol 启动、查询、更新和取消任务。官方当前仍将这些能力标为 Beta 或 Preview。

因此平台必须配置：

```text
feature_flag
harness_capability
tenant_allowlist
environment_policy
```

才能启用。

## 16.5 预算限制

必须防止无限递归和无限 Fan-out：

```text
max_subagent_depth
max_subagent_concurrency
max_total_subagent_calls
max_duration
max_model_calls
max_tool_calls
max_cost
```

---

# 17. Run Orchestrator

## 17.1 创建 Run

```text
Client
  ↓
POST /threads/{thread_id}/runs
  ↓
Run API
  ↓
验证 AgentDeployment
  ↓
解析 ResolvedExecutionPlan
  ↓
预留 Quota
  ↓
创建 Run
  ↓
创建 RunAttempt
  ↓
写 Dispatch Outbox
  ↓
提交事务
```

## 17.2 分发任务

```text
Dispatch Relay
      ↓
Durable Queue
      ↓
Runtime Worker
      ↓
Acquire DB Lease
      ↓
Execute
```

## 17.3 Worker Lease

Lease 至少包含：

```text
attempt_id
worker_id
lease_token
acquired_at
heartbeat_at
expires_at
```

Worker 定期续租。

如果 Lease 超时：

```text
RUNNING
   ↓
ORPHANED
   ↓
创建新 RunAttempt
   ↓
新 Worker 从 Checkpoint 恢复
```

## 17.4 为什么需要 RunAttempt

```text
Run
├── Attempt 1：Worker 崩溃
├── Attempt 2：恢复后等待审批
└── Attempt 3：审批后执行完成
```

Run 表达用户级任务。

RunAttempt 表达调度和执行尝试。

---

# 18. Run 状态机

```text
CREATED
   ↓
QUEUED
   ↓
PREPARING
   ↓
RUNNING
   ├── WAITING_FOR_APPROVAL
   ├── WAITING_FOR_INPUT
   ├── PAUSED
   ├── ORPHANED
   ├── CANCELLING
   │       ↓
   │    CANCELLED
   ├── TIMED_OUT
   ├── FAILED
   └── SUCCEEDED
```

恢复审批：

```text
WAITING_FOR_APPROVAL
        ↓
Decision Accepted
        ↓
QUEUED
        ↓
RESUMING
        ↓
RUNNING
```

RunAttempt 状态：

```text
PENDING
LEASED
RUNNING
LOST
FAILED
SUCCEEDED
CANCELLED
```

---

# 19. Checkpoint、Event 与 Read Model

## 19.1 事实来源

| 数据            | 事实来源                               |
| ------------- | ---------------------------------- |
| Agent 执行状态    | LangGraph Checkpointer             |
| 调度状态          | Run / RunAttempt                   |
| 审计与流式生命周期     | RunEvent                           |
| 长期记忆          | LangGraph Store                    |
| 用户消息列表        | Checkpoint + Event 派生视图            |
| Todo          | State + Event 派生视图                 |
| Tool Call 列表  | RunEvent 派生视图                      |
| SubAgent Tree | ExecutionSpan + RunRelation        |
| Artifact      | Artifact Metadata + Object Storage |

不要让：

```text
message
checkpoint
run_event
```

分别保存一份互相独立的“完整会话事实”。

## 19.2 Deep Agents Event Adapter

Deep Agents 当前能够按 Messages、Tool Calls、Values、SubAgents 和 Output 提供类型化事件投影，每个 SubAgent 又可以拥有自己的 Message、Tool Call、Nested SubAgent 和 Output。

平台不能直接将这些 SDK 对象暴露为永久 API，而应转换为平台事件。

## 19.3 Platform RuntimeEvent

```json
{
  "event_id": "evt_01",
  "sequence": 128,
  "schema_version": "1.0",

  "type": "subagent.started",

  "tenant_id": "tenant_01",
  "project_id": "project_01",

  "thread_id": "thread_01",
  "run_id": "run_01",
  "attempt_id": "attempt_02",

  "span_id": "span_09",
  "parent_span_id": "span_01",

  "execution_path": [
    "main",
    "researcher"
  ],

  "occurred_at": "2026-08-24T01:00:00Z",

  "visibility": "user",

  "payload": {
    "agent_name": "researcher",
    "task": "分析认证流程"
  }
}
```

## 19.4 事件分类

```text
run.created
run.queued
run.started
run.resumed
run.failed
run.completed

model.started
model.delta
model.completed

tool.requested
tool.approval_required
tool.started
tool.completed
tool.failed

todo.updated

subagent.started
subagent.progress
subagent.completed
subagent.failed

interrupt.created
interrupt.resolved

artifact.created
artifact.updated

sandbox.created
sandbox.command.started
sandbox.command.completed

usage.updated
quota.exceeded
```

## 19.5 持久事件与临时事件

永久保存：

```text
Run Lifecycle
Tool Call
HITL
Artifact
SubAgent Lifecycle
Usage
Error
Security Audit
```

可配置保存：

```text
Token Delta
中间文本 Delta
详细 Sandbox stdout
```

## 19.6 SSE 重连

```http
GET /api/v1/runs/{run_id}/events?after_sequence=128
```

或者：

```http
Last-Event-ID: 128
```

客户端以：

```text
run_id + sequence
```

去重。

---

# 20. HITL 与审批中心

## 20.1 Interrupt

```text
Tool Call
   ↓
Policy Engine
   ↓
Approval Required
   ↓
LangGraph Interrupt
   ↓
Checkpoint
   ↓
Run → WAITING_FOR_APPROVAL
```

## 20.2 数据结构

```text
Interrupt
├── interrupt_id
├── run_id
├── checkpoint_id
├── version
├── policy_reason
├── status
├── expires_at
└── actions[]
```

每个 Action：

```text
action_id
tool_name
arguments
risk_level
allowed_decisions
```

## 20.3 Decision API

```http
POST /api/v1/interrupts/{interrupt_id}/decisions
Idempotency-Key: decision-xxx
If-Match: 3
```

```json
{
  "decisions": [
    {
      "action_id": "action_1",
      "type": "approve"
    },
    {
      "action_id": "action_2",
      "type": "reject",
      "message": "不允许修改生产数据库"
    }
  ]
}
```

支持：

```text
approve
edit
reject
respond
```

## 20.4 幂等要求

LangGraph Interrupt 恢复时会重新执行发生 Interrupt 的 Node，因此 Interrupt 前的外部副作用必须幂等，或者拆到独立 Node / Task 中。

所有写操作建议使用：

```text
idempotency_key
upsert
read-before-write
outbox
external_operation_record
```

---

# 21. 多租户与数据隔离

## 21.1 租户层级

```text
Tenant
  ↓
Organization
  ↓
Project
  ↓
Environment
  ↓
AgentDeployment
```

## 21.2 隔离范围

每项资源必须具有 Scope：

```text
platform
tenant
organization
project
environment
user
agent
thread
run
```

## 21.3 必须隔离的对象

```text
Database Row
Store Namespace
Checkpoint Namespace
Object Storage Prefix
Vector Index Filter
Secret Namespace
MCP Credential
Sandbox Namespace
Queue Partition
Rate Limit
Quota
Trace
Event Stream
```

## 21.4 RuntimeContext

```python
class RuntimeContext(BaseModel):
    tenant_id: str
    organization_id: str | None
    project_id: str
    environment_id: str

    user_id: str
    roles: list[str]

    thread_id: str
    run_id: str
    attempt_id: str

    agent_deployment_id: str
    resolved_plan_id: str

    request_id: str
    trace_id: str
```

Credential 不直接存入 RuntimeContext 持久字段。

只保存：

```text
credential_ref
```

真实密钥由 Credential Broker 在调用前生成或读取，并尽量使用短期凭据。

---

# 22. Credential Broker

Credential Broker 负责：

```text
读取 Secret Manager
生成短期 Token
动态 Assume Role
按 Tool 注入 Credential
按 Tenant 隔离
自动轮换
调用后销毁
审计 Credential 使用
```

真实 Credential 禁止进入：

```text
AgentRevision
ResolvedExecutionPlan
Checkpoint
RunEvent
Message
Prompt
Trace
Artifact
```

Tool Gateway 接收到的是：

```text
credential_handle
```

而不是永久 API Key。

---

# 23. 数据库设计

## 23.1 租户与权限

```text
tenant
organization
project
environment

user
role
permission
role_binding
service_account
```

## 23.2 Agent 与资源

```text
agent
agent_draft
agent_revision
agent_deployment

resolved_execution_plan

resource
resource_revision
resource_binding

prompt
prompt_revision

model_provider
model_endpoint
model_catalog_entry
model_deployment
model_deployment_revision
model_capability_probe

tool_definition
tool_revision

mcp_server
mcp_server_revision
mcp_tool_snapshot

skill
skill_version

memory_source
memory_revision

knowledge_base
knowledge_revision

workspace_profile
workspace_profile_revision

sandbox_profile
sandbox_profile_revision

permission_policy
approval_policy
network_policy

schema_definition
schema_revision

harness_profile
harness_profile_revision
```

## 23.3 运行数据

```text
thread

run
run_attempt
run_relation
execution_span

run_event

interrupt
interrupt_action
decision

artifact
sandbox_instance
mcp_session

usage_ledger
quota_reservation

trace_link
idempotency_record
dispatch_outbox
```

## 23.4 Checkpoint

Checkpoint 使用 LangGraph Checkpointer 自己的存储结构。

平台只保存：

```text
run_id
thread_id
checkpoint_namespace
checkpoint_reference
```

不要再创建一张字段看起来相似、但无法真正被 LangGraph 恢复的“伪 checkpoint 表”。

## 23.5 关键约束

```text
agent_revision 发布后不可更新
resolved_execution_plan 不可更新
plan_hash 唯一
run_event(run_id, sequence) 唯一
idempotency_record(tenant_id, scope, key) 唯一
同一 Run 只能有一个 Active Lease
Interrupt Decision 使用乐观锁
Artifact 使用内容 Hash
Credential 只保存引用
```

---

# 24. Native API

## 24.1 Agent 管理

```http
POST   /api/v1/agents
GET    /api/v1/agents/{agent_id}
PATCH  /api/v1/agents/{agent_id}/draft

POST   /api/v1/agents/{agent_id}/revisions:publish
GET    /api/v1/agents/{agent_id}/revisions
GET    /api/v1/agent-revisions/{revision_id}

POST   /api/v1/agent-revisions/{revision_id}:validate
POST   /api/v1/agent-revisions/{revision_id}:evaluate

POST   /api/v1/agent-deployments
PATCH  /api/v1/agent-deployments/{deployment_id}
```

## 24.2 Thread 与 Run

```http
POST /api/v1/threads
GET  /api/v1/threads/{thread_id}

POST /api/v1/threads/{thread_id}/runs
GET  /api/v1/runs/{run_id}

POST /api/v1/runs/{run_id}:cancel
POST /api/v1/runs/{run_id}:retry

GET /api/v1/runs/{run_id}/events
GET /api/v1/runs/{run_id}/stream
GET /api/v1/runs/{run_id}/artifacts
GET /api/v1/runs/{run_id}/children
GET /api/v1/runs/{run_id}/spans
```

## 24.3 HITL

```http
GET  /api/v1/interrupts
GET  /api/v1/interrupts/{interrupt_id}

POST /api/v1/interrupts/{interrupt_id}/decisions
```

## 24.4 Sandbox

```http
GET  /api/v1/sandboxes/{sandbox_id}
GET  /api/v1/sandboxes/{sandbox_id}/files
GET  /api/v1/sandboxes/{sandbox_id}/files/{path}

POST /api/v1/sandboxes/{sandbox_id}:stop
POST /api/v1/sandboxes/{sandbox_id}:extend
```

## 24.5 API 通用要求

所有写请求支持：

```text
Idempotency-Key
Request-ID
Traceparent
If-Match
Actor Identity
Tenant Context
```

---

# 25. OpenAI / Anthropic 兼容层

兼容层只负责转换。

```text
OpenAI Request
      ↓
Protocol Adapter
      ↓
Native Thread / Run API
      ↓
Platform Event
      ↓
OpenAI Stream Chunk
```

兼容协议无法完整表达：

```text
Todo
SubAgent Tree
Async Task
HITL
Sandbox
Artifact
Execution Span
```

因此复杂控制台必须使用 Native API。

兼容层可以提供降级表达：

```text
SubAgent Event → Custom Metadata
HITL           → Tool Call / Requires Action
Artifact       → URL / File Reference
```

但平台内部不得依赖这些降级对象。

---

# 26. 完整运行时序

```text
User
 │
 │ POST /threads/{thread_id}/runs
 ▼
Run API
 │
 ├── 鉴权
 ├── 检查 Deployment
 ├── 获取 ResolvedExecutionPlan
 ├── 预留配额
 ├── 创建 Run
 ├── 创建 RunAttempt
 └── 写 Dispatch Outbox
 │
 ▼
Dispatch Relay
 │
 ▼
Durable Queue
 │
 ▼
Runtime Worker
 │
 ├── Acquire Lease
 ├── Load Execution Plan
 ├── Build RuntimeContext
 ├── Resolve Credential
 ├── Resolve MCP Session
 ├── Resolve Sandbox
 ├── Bind Checkpointer / Store
 └── Build AgentGraphFactory
 │
 ▼
DeepAgentsHarnessAdapter
 │
 ├── Resolve Model
 ├── Resolve Bound Tools
 ├── Resolve Skills
 ├── Resolve Memory
 ├── Resolve Backend
 ├── Resolve SubAgents
 ├── Resolve Middleware
 ├── Resolve HITL
 └── create_deep_agent()
 │
 ▼
LangGraph Runtime
 │
 ├── Model Call
 ├── Tool Call
 ├── Checkpoint
 ├── SubAgent
 ├── Interrupt
 ├── Stream
 └── State Update
 │
 ├──────────────► RunEvent Store
 ├──────────────► SSE Gateway
 ├──────────────► Artifact Storage
 ├──────────────► Usage Ledger
 └──────────────► Trace Backend
 │
 ▼
SUCCEEDED / FAILED / WAITING_FOR_APPROVAL
```

Deep Agents 的构建阶段负责组装 Middleware、Backend、SubAgent、Skill 和 Memory，真正的运行循环则由 LangGraph 驱动。

---

# 27. Kubernetes 部署架构

## 27.1 建议部署单元

初期不拆成十几个独立代码仓库。

采用：

> **模块化单仓库 + 多进程角色 + 可独立扩容的 Deployment。**

第一阶段部署：

```text
platform-api
run-orchestrator
runtime-worker
event-gateway
sandbox-manager
```

后续按负载拆出：

```text
model-gateway
tool-gateway
mcp-session-manager
evaluation-worker
dispatch-relay
```

## 27.2 Worker Pool

```text
runtime-worker-standard
runtime-worker-sandbox
runtime-worker-high-memory
runtime-worker-restricted
runtime-worker-preview
```

通过 Run Requirements 选择：

```text
needs_sandbox
needs_interpreter
security_level
region
tenant_tier
max_duration
```

## 27.3 扩缩容指标

Runtime Worker 不应只按照 CPU 扩缩容。

主要指标：

```text
queue_depth
oldest_queued_run_age
active_run_count
available_worker_slots
sandbox_create_latency
model_concurrency
```

## 27.4 Sandbox Namespace

```text
deepagent-platform
deepagent-runtime
deepagent-sandbox
```

Sandbox Pod 与平台 API Pod 必须分开 Namespace 和 ServiceAccount。

---

# 28. 推荐代码仓库结构

```text
deepagent-platform/
│
├── apps/
│   ├── platform_api/
│   │   ├── main.py
│   │   ├── native_api/
│   │   ├── compatibility_api/
│   │   └── admin_api/
│   │
│   ├── run_orchestrator/
│   ├── runtime_worker/
│   ├── event_gateway/
│   ├── sandbox_manager/
│   └── dispatch_relay/
│
├── packages/
│   ├── domain/
│   │   ├── tenant/
│   │   ├── agent/
│   │   ├── resource/
│   │   ├── deployment/
│   │   ├── runtime/
│   │   ├── policy/
│   │   ├── sandbox/
│   │   └── usage/
│   │
│   ├── application/
│   │   ├── agent_service/
│   │   ├── publish_service/
│   │   ├── run_service/
│   │   ├── approval_service/
│   │   └── evaluation_service/
│   │
│   ├── compiler/
│   │   ├── plan_compiler.py
│   │   ├── validators/
│   │   ├── resolvers/
│   │   ├── dependency_lock.py
│   │   └── plan_hasher.py
│   │
│   ├── runtime/
│   │   ├── binder.py
│   │   ├── executor.py
│   │   ├── lease.py
│   │   ├── context.py
│   │   └── event_emitter.py
│   │
│   ├── adapters/
│   │   ├── harness/
│   │   │   ├── deepagents/
│   │   │   │   ├── adapter.py
│   │   │   │   ├── factory.py
│   │   │   │   ├── event_adapter.py
│   │   │   │   ├── backend_adapter.py
│   │   │   │   ├── subagent_adapter.py
│   │   │   │   └── compatibility.py
│   │   │   ├── langchain_agent/
│   │   │   └── custom_langgraph/
│   │   │
│   │   ├── model/
│   │   ├── tool/
│   │   ├── mcp/
│   │   ├── queue/
│   │   ├── storage/
│   │   └── sandbox/
│   │
│   ├── policy/
│   │   ├── visibility.py
│   │   ├── authorization.py
│   │   ├── approval.py
│   │   ├── credential.py
│   │   └── redaction.py
│   │
│   ├── protocols/
│   │   ├── native/
│   │   ├── openai/
│   │   ├── anthropic/
│   │   ├── acp/
│   │   └── a2a/
│   │
│   ├── persistence/
│   │   ├── repositories/
│   │   ├── checkpointer/
│   │   ├── store/
│   │   ├── event_store/
│   │   ├── object_storage/
│   │   └── outbox/
│   │
│   └── observability/
│       ├── tracing.py
│       ├── metrics.py
│       ├── logging.py
│       └── usage.py
│
├── migrations/
├── deploy/
│   ├── helm/
│   └── kubernetes/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── replay/
│   ├── security/
│   ├── failure_injection/
│   └── load/
│
├── pyproject.toml
└── README.md
```

---

# 29. 可观测性

## 29.1 Trace 层级

```text
Request Trace
  └── Run
      ├── RunAttempt
      ├── Main Agent Span
      │   ├── Model Call
      │   ├── Tool Call
      │   ├── Sync SubAgent Span
      │   └── Sandbox Command
      └── Async Child Run
```

## 29.2 核心指标

```text
run_created_total
run_succeeded_total
run_failed_total
run_duration_seconds

run_queue_delay_seconds
run_resume_latency_seconds
orphaned_run_total

model_request_total
model_latency_seconds
model_token_total
model_cost_total
model_fallback_total

tool_call_total
tool_error_total
tool_approval_total
tool_latency_seconds

subagent_call_total
subagent_depth
subagent_concurrency

sandbox_create_latency
sandbox_active_total
sandbox_failure_total

event_delivery_lag
checkpoint_write_latency
checkpoint_failure_total
```

## 29.3 日志字段

所有日志统一包含：

```text
tenant_id
project_id
thread_id
run_id
attempt_id
span_id
agent_revision_id
resolved_plan_id
worker_id
request_id
trace_id
```

---

# 30. Usage 与 Quota

## 30.1 Usage Ledger

记录：

```text
模型输入 Token
模型输出 Token
缓存 Token
模型调用次数
工具调用次数
SubAgent 调用次数
Sandbox CPU 时间
Sandbox 内存时间
存储使用量
向量检索次数
MCP 调用次数
```

## 30.2 Quota Reservation

Run 开始前预留预算：

```text
estimated_cost
max_cost
max_duration
max_model_calls
max_tool_calls
```

Run 完成后：

```text
reservation
    ↓
actual usage reconciliation
    ↓
release remaining quota
```

## 30.3 超限行为

```text
hard_stop
graceful_stop
require_approval
fallback_to_cheaper_model
```

所有行为由 Tenant Policy 决定。

---

# 31. 发布与评测流程

```text
AgentDraft
   ↓
Static Validation
   ↓
Dependency Resolution
   ↓
Capability Validation
   ↓
Security Validation
   ↓
ResolvedExecutionPlan
   ↓
Contract Test
   ↓
Evaluation Suite
   ↓
Release Gate
   ↓
AgentRevision Published
   ↓
Deployment
```

## 31.1 静态校验

```text
资源引用是否存在
Prompt 是否为空
Tool Schema 是否合法
MCP Schema 是否冲突
模型是否支持 Tool Calling
模型是否支持 Structured Output
SubAgent 是否形成循环依赖
权限规则是否冲突
Sandbox Profile 是否存在
预算是否完整
Preview Feature 是否被允许
```

## 31.2 Contract Test

```text
Model Tool Calling Test
Structured Output Test
Tool Input Validation Test
MCP Connection Test
Skill Loading Test
Memory Namespace Test
Checkpoint Resume Test
HITL Resume Test
Sandbox Health Test
```

## 31.3 Evaluation

```text
Golden Dataset
Rule-based Grader
LLM Judge
Tool Correctness
Safety Evaluation
Cost Evaluation
Latency Evaluation
Regression Evaluation
```

---

# 32. 版本升级与在途 Run

## 32.1 在途 Run 固定运行环境

已经进入以下状态的 Run：

```text
RUNNING
WAITING_FOR_APPROVAL
PAUSED
ORPHANED
```

必须继续使用原始：

```text
ResolvedExecutionPlan
Runtime Image Digest
Harness Adapter Version
Graph Definition
```

不能在审批恢复时自动切换到最新 Agent 版本。

## 32.2 Runtime Image 保留

平台需要为长时间 Interrupt 保留旧 Runtime Image。

```text
image retention period
≥
maximum interrupt retention period
```

## 32.3 显式迁移

需要升级在途 Thread 时，必须创建：

```text
ThreadMigration
```

记录：

```text
source_plan_id
target_plan_id
state_transformer
migration_result
operator
```

不允许静默升级。

---

# 33. 失败恢复策略

| 故障              | 恢复方式                          |
| --------------- | ----------------------------- |
| API Pod 重启      | Run 已持久化，不影响执行                |
| Orchestrator 重启 | Outbox 重新投递                   |
| Worker 崩溃       | Lease 过期，新 Attempt 接管         |
| Model 超时        | 按 Routing Policy 重试或 Fallback |
| MCP 断线          | 重建 Session                    |
| Sandbox 崩溃      | 按 Lifecycle Policy 恢复或重建      |
| SSE 断线          | 使用 sequence 重连                |
| HITL 长时间暂停      | Checkpoint 持久保存               |
| 外部写调用结果未知       | 使用幂等键查询或重放                    |
| Event 发布失败      | Transactional Outbox 重发       |
| Artifact 上传失败   | 保持 Run 非终态或进入补偿任务             |

LangGraph 的 Checkpoint 可以让失败、超时或 HITL 暂停后的运行从最近状态恢复，但外部副作用仍然需要平台自身的幂等和补偿机制。

---

# 34. 分阶段实施

## Phase 1：核心运行骨架

```text
Tenant / Project
Agent Draft / Revision
Prompt Registry
Model Deployment Registry
ResolvedExecutionPlan
Agent Deployment
Thread / Run / RunAttempt
Run Orchestrator
Runtime Worker
PostgreSQL Checkpointer
Platform RuntimeEvent
SSE
基本 Tool
基本 HITL
Usage / Quota
```

验收重点：

```text
可发布
可运行
可流式
可恢复
可审批
可审计
```

## Phase 2：资源平台化

```text
Tool Registry
MCP Registry
Skill Registry
Memory
Knowledge Base
Workspace Mount
Policy Engine
Credential Broker
Model Gateway
Tool Gateway
```

## Phase 3：高级执行

```text
Kubernetes Sandbox
Sync SubAgent
Compiled LangGraph SubAgent
ExecutionSpan
SubAgent Tree UI
Artifact Browser
Sandbox IDE UI
```

## Phase 4：预览与跨 Agent 能力

```text
Interpreter
Dynamic SubAgent
Async SubAgent
ACP
A2A
Remote Agent
Grading Rubrics
Advanced Evaluations
```

预览能力不得阻塞 Phase 1 的核心运行骨架。

---

# 35. V1 验收标准

平台 V1 至少满足以下要求：

1. 用户可以创建 AgentDraft 并发布不可变 AgentRevision；
2. 发布时可以生成完整 ResolvedExecutionPlan；
3. Run 始终绑定明确的 Plan 和 Runtime Image；
4. Worker 崩溃后，可以由其他 Worker 从 Checkpoint 恢复；
5. SSE 断线后，可以从指定 Sequence 继续；
6. HITL 可以暂停数小时或数天后恢复；
7. 审批请求支持 Approve、Edit、Reject；
8. Tool 写操作支持 Idempotency Key；
9. 不同 Tenant 无法访问彼此的 Store、Artifact 和 Sandbox；
10. Tool、MCP、Sandbox 均经过统一 Policy Enforcement；
11. Run 能够展示 Model、Tool、SubAgent、Artifact 和 Interrupt 事件；
12. Runtime Worker 与 Sandbox Pod 完全隔离；
13. Agent 修改后不会影响历史 Run；
14. 模型 Fallback 可以被追踪和计费；
15. 平台可以限制最大成本、最大调用次数与最大 SubAgent 并发。

---

# 36. 最终架构决策

## ADR-001

```text
Deep Agents 定位为 Harness Adapter，不定位为 Runtime。
```

## ADR-002

```text
LangGraph 是 DeepAgent Platform v1 的底层执行运行时。
```

## ADR-003

```text
Agent 使用 Draft、Revision、ResolvedExecutionPlan、Deployment 四层模型。
```

## ADR-004

```text
编译分为发布期静态解析和运行期动态绑定。
```

## ADR-005

```text
长任务通过 Run Orchestrator、Durable Queue 和 Worker Lease 调度。
```

## ADR-006

```text
执行交付语义为 At Least Once，所有外部副作用要求幂等。
```

## ADR-007

```text
Checkpoint 是执行状态事实来源，RunEvent 是审计和流式事件事实来源。
```

## ADR-008

```text
Tool 与 MCP 在运行态统一，在控制面保持独立生命周期。
```

## ADR-009

```text
平台权限系统覆盖 Tool、MCP、Sandbox，不能只依赖 Deep Agents permissions。
```

## ADR-010

```text
Memory、Skill、Knowledge 必须作为三个独立领域。
```

## ADR-011

```text
Sandbox 与 Interpreter 必须分离。
```

## ADR-012

```text
SubAgent 使用 Target、Execution、Orchestration、Transport、State Scope 正交建模。
```

## ADR-013

```text
Preview / Beta 能力通过 Harness Compatibility Profile 和 Feature Gate 管理。
```

## ADR-014

```text
平台采用模块化单仓库，但运行角色可以独立部署和扩容。
```

## ADR-015

```text
Native Agent API 是完整能力入口，OpenAI、Anthropic、ACP、A2A 是协议适配层。
```

---

# 37. 最终结论

DeepAgent Platform 最终不是：

```text
HTTP API
   ↓
create_deep_agent()
```

也不是：

```text
DeepAgents Runtime
LangGraph Runtime
Other Runtime
```

而是：

```text
                      DeepAgent Platform
                              │
           ┌──────────────────┼──────────────────┐
           │                  │                  │
           ▼                  ▼                  ▼
     Control Plane      Execution Control     Data Plane
           │                  │
           ▼                  ▼
ResolvedExecutionPlan   Run Orchestrator
                              │
                              ▼
                        Runtime Worker
                              │
                              ▼
                     Executable Builder
           ┌──────────────────┼──────────────────┐
           │                  │                  │
           ▼                  ▼                  ▼
 DeepAgentsHarness      LangChainHarness   CustomGraphAdapter
           │                  │                  │
           └──────────────────┼──────────────────┘
                              ▼
                       LangGraph Runtime
                              │
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
          Models            Tools          Sandboxes
```

这套架构既能完整使用 Deep Agents 当前提供的：

```text
Filesystem
Skills
Memory
Context Management
Planning
Sync SubAgent
Compiled SubAgent
Dynamic SubAgent
Async SubAgent
Interpreter
Sandbox
HITL
Streaming
Structured Output
Middleware
ACP
A2A
Evaluation
```

又不会把平台永久锁死在：

```python
create_deep_agent(...)
```

之上。

平台真正稳定的核心抽象最终是：

```text
AgentRevision
ResolvedExecutionPlan
AgentDeployment
Run
RunAttempt
RuntimeContext
RuntimeEvent
CapabilityBinding
Policy
Artifact
```

Deep Agents 只是这些核心抽象在当前阶段最重要、最完整的第一个实现。
