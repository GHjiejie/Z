知道，而且如果你的目标是做一个“**可以完整发挥 DeepAgents 能力的 LLM 平台**”，那它不应该只是：

```python
agent = create_deep_agent(...)
agent.invoke(...)
```

外面套一层 HTTP API。

更合理的定位是：

> **DeepAgents 是 Agent Runtime / Harness；你的平台负责把 Model、Agent、Tool、MCP、Skill、Memory、SubAgent、Sandbox、HITL、Checkpoint、Streaming 等能力配置化、持久化、可视化和多租户化。**

当前 DeepAgents 本身构建在 LangChain Agent + LangGraph Runtime 之上，LangGraph 提供 durable execution、checkpoint、streaming、interrupt 等运行时能力，DeepAgents 再提供 filesystem、context management、subagent、skills 等完整 agent harness。([GitHub][1])

---

# 一、先确定我们这个平台到底是什么

我建议把项目定义为：

```text
DeepAgent Platform
```

它不是“聊天系统”，而是：

```text
               LLM / Agent Development Platform
```

用户可以在平台中创建：

```text
Research Agent
Coding Agent
Data Analysis Agent
Customer Service Agent
DevOps Agent
K8s Agent
Frontend Agent
Security Review Agent
Multi-Agent Supervisor
......
```

而每个 Agent 都只是一个配置：

```text
Agent Definition
       ↓
Agent Compiler
       ↓
create_deep_agent(...)
       ↓
CompiledStateGraph
       ↓
LangGraph Runtime
```

这会成为整个系统最核心的抽象。

---

# 二、DeepAgents 当前有哪些东西必须被平台化

截至现在的官方文档，DeepAgents 可以概括成四大块：

| DeepAgents 能力      | 平台对应能力                               |
| ------------------ | ------------------------------------ |
| Model              | Model Registry / Model Gateway       |
| System Prompt      | Prompt Management                    |
| Tool               | Tool Registry                        |
| MCP                | MCP Server Registry                  |
| Filesystem         | Workspace                            |
| Backend            | Backend Manager                      |
| Permissions        | Permission Policy                    |
| Sandbox            | Sandbox Manager                      |
| `execute`          | Shell Runtime                        |
| Interpreter        | Code Interpreter                     |
| Skills             | Skill Registry                       |
| Memory             | Memory Service                       |
| Retrieval          | Knowledge Base / RAG                 |
| Summarization      | Context Manager                      |
| Context Offloading | Artifact / Context Storage           |
| Prompt Cache       | Model Optimization                   |
| Todo Planning      | Task / Plan UI                       |
| Sync SubAgent      | SubAgent Registry                    |
| CompiledSubAgent   | Graph Agent Registry                 |
| Dynamic SubAgent   | Dynamic Agent Runtime                |
| Async SubAgent     | Async Task Runtime                   |
| HITL               | Approval Center                      |
| Checkpointer       | Conversation / Execution Persistence |
| Store              | Long-term Storage                    |
| Streaming          | Event Bus                            |
| Structured Output  | Output Schema                        |
| Middleware         | Middleware Registry                  |
| HarnessProfile     | Agent Runtime Profile                |
| ProviderProfile    | Model Provider Profile               |
| `state_schema`     | Agent State Definition               |
| `context_schema`   | Runtime Context                      |
| LangSmith          | Observability                        |
| ACP                | Agent Client Protocol Gateway        |
| A2A                | Agent-to-Agent Gateway               |
| Grading Rubrics    | Evaluation / Judge System            |

这里尤其有几个很容易按照旧资料设计错的点。

DeepAgents 当前的 filesystem 内置 `ls/read_file/write_file/edit_file/delete/glob/grep`；如果 backend 支持 sandbox，还会出现 `execute`；同步 SubAgent 提供 `task`。而 `write_todos` 已经不是默认工具，从 v0.7 开始需要显式加入 `TodoListMiddleware`。([Docs by LangChain][2])

同步 `general-purpose` subagent 默认存在，而 Async SubAgent 是另一套非阻塞机制，会提供 `start_async_task/check_async_task/update_async_task/cancel_async_task/list_async_tasks`。([Docs by LangChain][3])

---

# 三、我建议的整体架构

平台不要做成一个大 FastAPI 服务。

应该分成：

```text
                         ┌────────────────────────────┐
                         │       Web Console          │
                         │                            │
                         │ Agent / Skill / Tool / MCP │
                         │ Model / Memory / Sandbox   │
                         │ Thread / Run / HITL        │
                         └─────────────┬──────────────┘
                                       │
                                       ▼
┌────────────────────────────────────────────────────────────────┐
│                         API Gateway                             │
│                                                                │
│   Native Agent API     OpenAI Compatible API     ACP / A2A     │
└───────────────────────────────┬────────────────────────────────┘
                                │
              ┌─────────────────┴────────────────┐
              │                                  │
              ▼                                  ▼
┌─────────────────────────┐          ┌───────────────────────────┐
│      CONTROL PLANE      │          │       RUNTIME PLANE       │
│                         │          │                           │
│ Agent Registry          │          │ Agent Compiler            │
│ Model Registry          │          │ Agent Runtime             │
│ Tool Registry           │          │ LangGraph Runtime         │
│ MCP Registry            │          │ DeepAgents                │
│ Skill Registry          │          │ Streaming Runtime         │
│ Prompt Registry         │          │ HITL Runtime              │
│ SubAgent Registry       │          │ SubAgent Runtime          │
│ Sandbox Profiles        │          │ Sandbox Runtime           │
│ Permission Policies     │          │ Context Runtime           │
└─────────────┬───────────┘          └────────────┬──────────────┘
              │                                   │
              └────────────────┬──────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────────┐
│                          DATA PLANE                             │
│                                                                │
│ PostgreSQL       Redis        Object Storage       Vector DB    │
│                                                                │
│ Agent Config     Events       Files/Artifacts      Knowledge   │
│ Threads          Locks        Skill Resources      Embeddings  │
│ Runs             Cache        Sandbox Files                   │
└────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────┐
│                       EXTERNAL SYSTEMS                         │
│                                                                │
│ OpenAI-compatible Models / Anthropic-compatible Models         │
│ MCP Servers / Internal APIs / Databases / Git / K8s / SaaS    │
└────────────────────────────────────────────────────────────────┘
```

最重要的设计是：

> **Control Plane 管“Agent 是什么”；Runtime Plane 管“Agent 怎么跑”。**

千万不要把 `create_deep_agent()` 散落到各种业务 Service 里面。

---

# 四、系统最核心的数据结构：AgentDefinition

整个系统应该围绕：

```python
AgentDefinition
```

设计。

例如：

```python
class AgentDefinition(BaseModel):
    id: str
    name: str
    version: int

    model: ModelRef

    system_prompt: str

    tools: list[ToolRef] = []
    mcp_servers: list[MCPServerRef] = []

    skills: list[SkillRef] = []
    memory: list[MemoryRef] = []

    subagents: list[SubAgentRef] = []

    backend: BackendConfig

    permissions: list[PermissionRule] = []

    planning: PlanningConfig

    interpreter: InterpreterConfig

    hitl: HITLConfig

    context: ContextConfig

    response_schema: dict | None = None

    middleware: list[MiddlewareRef] = []

    runtime_profile: str | None = None
```

以后用户在前端配置：

```text
Agent
```

实际上就是编辑这个对象。

---

# 五、Agent Compiler 是平台真正的核心

这层特别重要。

不要：

```text
Controller
   ↓
create_deep_agent()
```

而应该：

```text
AgentDefinition
      ↓
AgentCompiler
      ├── ModelResolver
      ├── ToolResolver
      ├── MCPResolver
      ├── SkillResolver
      ├── MemoryResolver
      ├── BackendResolver
      ├── SubAgentResolver
      ├── MiddlewareResolver
      ├── PermissionResolver
      └── RuntimeResolver
               ↓
        create_deep_agent()
               ↓
       CompiledStateGraph
```

一个简化实现：

```python
class AgentCompiler:

    async def compile(
        self,
        definition: AgentDefinition,
    ):
        model = await self.model_resolver.resolve(
            definition.model
        )

        tools = await self.tool_resolver.resolve(
            definition.tools
        )

        mcp_tools = await self.mcp_resolver.resolve(
            definition.mcp_servers
        )

        backend = await self.backend_resolver.resolve(
            definition.backend
        )

        subagents = await self.subagent_resolver.resolve(
            definition.subagents
        )

        middleware = await self.middleware_resolver.resolve(
            definition
        )

        return create_deep_agent(
            name=definition.name,

            model=model,

            system_prompt=definition.system_prompt,

            tools=[
                *tools,
                *mcp_tools,
            ],

            skills=self.skill_resolver.paths(
                definition.skills
            ),

            memory=self.memory_resolver.paths(
                definition.memory
            ),

            subagents=subagents,

            backend=backend,

            permissions=self.permission_resolver.resolve(
                definition.permissions
            ),

            middleware=middleware,

            interrupt_on=self.hitl_resolver.resolve(
                definition.hitl
            ),

            response_format=definition.response_schema,

            context_schema=RuntimeContext,

            checkpointer=self.checkpointer,

            store=self.store,
        )
```

官方目前的 `create_deep_agent()` 本身已经公开了 model、tools、system_prompt、middleware、subagents、skills、memory、permissions、backend、interrupt_on、response_format、state_schema、context_schema、checkpointer、store、cache 等完整入口，因此这个 Compiler 架构与 DeepAgents 本身的扩展模型是高度吻合的。([Docs by LangChain][4])

---

# 六、Model Registry

你的 Model 层不要让 Agent 直接知道：

```text
OpenAI
Anthropic
Gemini
vLLM
Ollama
DeepSeek
Qwen
```

Agent 只知道：

```text
model_id
```

比如：

```json
{
  "id": "qwen3-prod",
  "provider": "openai_compatible",
  "model": "qwen3-235b",
  "base_url": "https://llm.example.com/v1",
  "credential_id": "credential-123",
  "capabilities": {
    "tool_calling": true,
    "vision": true,
    "structured_output": true,
    "reasoning": true
  }
}
```

然后：

```text
Agent
  ↓
ModelRef
  ↓
ModelRegistry
  ↓
ModelFactory
  ↓
BaseChatModel
  ↓
DeepAgents
```

DeepAgents 本身是 model-agnostic，只要求底层 LangChain chat model 提供 Agent 所需能力，尤其是 tool calling。([Docs by LangChain][5])

这样以后接 vLLM：

```text
DeepAgent
    ↓
LangChain
    ↓
ChatOpenAI
    ↓
OpenAI Compatible API
    ↓
vLLM
```

完全不用改变 Agent。

这其实特别适合你现在已有的 OpenAI / Anthropic 协议兼容模型层。

---

# 七、Tools 和 MCP 必须统一成 Tool Registry

我不会设计两个完全分离的系统：

```text
Tools
MCP
```

对于 Agent Runtime 来说最终都是：

```text
BaseTool[]
```

所以平台应该是：

```text
Tool Registry
     │
     ├── Python Tool
     ├── HTTP Tool
     ├── Internal API Tool
     ├── Database Tool
     └── MCP Tool
```

Agent：

```text
Agent A
 ├── search
 ├── github
 ├── kubernetes
 └── mysql
```

用户创建 Agent 时只需要勾选。

DeepAgents 原生可以接受 callable、LangChain tools 以及 MCP Server 暴露的 tools。([Docs by LangChain][2])

---

# 八、Filesystem 不能简单理解成“文件”

这是 DeepAgents 非常重要的思想。

Filesystem 其实是：

```text
Agent Context Storage Interface
```

官方 Backend 已经包括：

```text
StateBackend
FilesystemBackend
LocalShellBackend
StoreBackend
ContextHubBackend
CompositeBackend
Sandbox Backend
Custom Backend
```

还可以自己实现 `BackendProtocol`；如果要提供 shell，则实现相应 sandbox backend protocol。([Docs by LangChain][6])

所以平台应该提供：

```text
Backend Profile
```

例如：

```json
{
  "type": "composite",

  "routes": {
    "/workspace/": {
      "type": "filesystem"
    },

    "/memory/": {
      "type": "store",
      "scope": "user"
    },

    "/knowledge/": {
      "type": "knowledge"
    }
  },

  "default": {
    "type": "state"
  }
}
```

这个思想非常漂亮：

```text
Agent 看到：

/
├── workspace/
├── memory/
├── skills/
├── knowledge/
├── conversation_history/
└── large_tool_results/
```

但是这些路径背后实际上可能是：

```text
workspace → Sandbox
memory    → PostgreSQL / LangGraph Store
skills    → Object Storage
knowledge → S3
temp      → LangGraph State
```

DeepAgents 的 `CompositeBackend` 本身就是通过路径前缀把不同文件操作路由到不同 backend。([Docs by LangChain][6])

---

# 九、Memory 和 Skill 一定要分开

这是做平台时特别容易混掉的。

## Memory

例如：

```text
用户偏好
项目规则
编码规范
架构信息
长期上下文
```

对应：

```text
AGENTS.md
```

Memory 在 Agent 启动时加载。

DeepAgents 的 Memory 是 persistent context，并且可以结合 `StoreBackend` 实现跨 thread 存储。([Docs by LangChain][7])

## Skill

例如：

```text
Kubernetes Deployment Review
Frontend Code Review
Create PPT
SQL Optimization
PR Review
```

对应：

```text
skills/
└── k8s-review/
    ├── SKILL.md
    ├── scripts/
    ├── references/
    └── templates/
```

Skill 是按需加载：

```text
startup
   ↓
只加载 metadata
   ↓
模型发现任务需要 skill
   ↓
read SKILL.md
   ↓
必要时读取 references/scripts
```

这是 progressive disclosure。([Docs by LangChain][8])

所以 UI 也应该完全分开：

```text
Memory
Skills
```

不要搞成一个“知识库”。

---

# 十、再单独提供 Knowledge Base / RAG

第三种东西是：

```text
Knowledge Base
```

例如：

```text
公司文档
API 文档
产品 PRD
Wiki
PDF
数据库
Confluence
Git repository
```

然后通过：

```text
Retriever Tool
```

暴露给 Agent。

关系应该是：

```text
Memory
   = 我是谁 / 用户是谁 / 项目有什么约定

Skill
   = 我该怎么完成某种任务

Knowledge
   = 完成任务需要查询什么事实
```

这是三个不同概念。

DeepAgents 可以通过 Agentic RAG，让模型自行决定何时使用 retrieval tool。([Docs by LangChain][9])

---

# 十一、Planning 要成为独立的平台能力

配置：

```json
{
  "planning": {
    "enabled": true
  }
}
```

AgentCompiler：

```python
from langchain.agents.middleware import TodoListMiddleware

if definition.planning.enabled:
    middleware.append(
        TodoListMiddleware()
    )
```

于是 Agent 获得：

```text
write_todos
```

状态：

```text
pending
in_progress
completed
```

前端可以直接显示：

```text
正在处理任务

✓ 分析代码结构
✓ 查找 Authentication 实现
● Review token refresh
○ 检查安全风险
○ 生成报告
```

这会比只有：

```text
Thinking...
```

高级很多。

官方现在明确将 task planning 做成 opt-in，并且 Todo 状态直接存在 agent state 中，非常适合拿来做运行进度 UI。([Docs by LangChain][7])

---

# 十二、SubAgent 是这个平台最值得做好的部分

我建议平台支持四种 Agent：

```text
Main Agent
Sync SubAgent
Async SubAgent
Compiled SubAgent
```

再加：

```text
Dynamic SubAgent execution
```

## Sync SubAgent

例如：

```text
Main Agent
      │
      ├── task("researcher")
      │
      ├── task("coder")
      │
      └── task("reviewer")
```

每个 SubAgent 有：

```text
独立 context
独立 prompt
独立 tools
可独立 model
独立 skills
独立 filesystem permissions
```

最后：

```text
SubAgent
   ↓
final result
   ↓
Main Agent
```

这样大量工具结果不会污染 Main Agent context。([Docs by LangChain][3])

---

# 十三、CompiledSubAgent

这就更强了。

SubAgent 不一定是：

```python
create_deep_agent()
```

也可以是：

```text
CompiledStateGraph
```

也就是说：

```text
DeepAgent
   ↓
task
   ↓
一个完整 LangGraph Workflow
```

例如：

```text
Supervisor DeepAgent

      │
      ├── Research DeepAgent
      │
      ├── Code DeepAgent
      │
      └── Release Workflow
                  │
                  ↓
             LangGraph
          ┌──────────────┐
          │ build        │
          │ test         │
          │ approval     │
          │ deployment   │
          └──────────────┘
```

官方直接支持将预编译的 LangGraph runnable 作为 `CompiledSubAgent`。([Docs by LangChain][3])

这意味着：

> 你的平台千万不要把 Agent 和 Workflow 做成两个互不相干的产品。

应该允许：

```text
Agent → Agent
Agent → Workflow
Workflow → Agent
```

---

# 十四、Dynamic SubAgent

如果加入：

```python
CodeInterpreterMiddleware()
```

DeepAgent 可以在 JavaScript 中：

```javascript
const results = await Promise.all(
    files.map(file =>
        task({
            subagentType: "reviewer",
            description: `review ${file}`
        })
    )
)
```

这就是：

```text
Dynamic Fan-out
```

特别适合：

```text
100 个文件
100 个 issue
50 个 Pod
20 个服务
多个搜索主题
```

官方把这种 interpreter 驱动的 delegation 称为 dynamic subagents；Interpreter 当前属于 Beta。([Docs by LangChain][3])

因此平台配置可以有：

```json
{
  "interpreter": {
    "enabled": true,
    "mode": "thread",
    "ptc_tools": [
      "search",
      "query_database"
    ],
    "dynamic_subagents": true
  }
}
```

---

# 十五、Async SubAgent

这个又和 Sync SubAgent 完全不一样。

例如用户：

```text
帮我分析整个 Kubernetes 集群的问题
```

Main Agent：

```text
start_async_task → security
start_async_task → networking
start_async_task → resource
```

马上返回：

```text
已经启动三个分析任务。
```

用户还能继续聊天。

后台：

```text
security   running
network    running
resource   running
```

之后：

```text
check_async_task
update_async_task
cancel_async_task
list_async_tasks
```

官方 Async SubAgent 本身维护独立 thread，是 stateful 的，而同步 subagent 每次调用则是隔离、短生命周期执行。([Docs by LangChain][10])

所以前端必须有：

```text
Background Tasks

┌────────────────────────────┐
│ Kubernetes Security Review │
│ Running                    │
│                            │
│ [View] [Guide] [Cancel]    │
└────────────────────────────┘
```

---

# 十六、Context Management 不需要我们重新发明一套

这是 DeepAgents 已经做得很好的地方。

它当前有两层关键策略。

第一层：

```text
Tool Result Offloading
```

默认大 tool result 超过一定规模时会存入 filesystem，只给模型：

```text
preview + file path
```

第二层：

```text
Conversation Summarization
```

接近 context window 时：

```text
旧 messages
       ↓
SummarizationMiddleware
       ↓
structured summary
       +
recent messages
```

原始 conversation 仍可以写入 filesystem 保存。官方当前默认的 context compression 包含 tool-result offloading 与自动 summarization；文档描述默认 large-tool-result threshold 为 20,000 tokens，并在接近模型 context limit 时进行总结。([Docs by LangChain][11])

因此你的平台不要再自己写：

```python
if token > 100000:
    delete_old_messages()
```

这会和 DeepAgents/LangGraph 的 context strategy 冲突。

平台应该负责：

```text
配置
监控
可视化
```

而不是重复实现。

---

# 十七、HITL 必须设计成一等公民

比如 Agent 调：

```text
delete_database
```

运行暂停：

```text
RUNNING
   ↓
INTERRUPTED
   ↓
WAITING_FOR_APPROVAL
```

前端：

```text
Agent wants to execute:

delete_database(
    database="production"
)

[Approve]

[Edit]

[Reject]

[Respond]
```

用户操作：

```text
approve
```

然后：

```text
Command(resume=...)
```

恢复同一个 LangGraph Run。

DeepAgents 的 `interrupt_on` 可以按 tool 设置 approval/edit/reject 等决策，并要求 checkpointer 保存可恢复状态。([Docs by LangChain][12])

所以数据库应该存在：

```text
run
interrupt
approval
```

而不是只保存 message。

---

# 十八、Sandbox 一定不要和 Interpreter 混为一谈

Interpreter：

```text
QuickJS
```

适合：

```text
循环
条件
批处理
数据转换
Programmatic Tool Calling
Dynamic SubAgent
```

默认不能：

```text
shell
network
host filesystem
package install
```

Sandbox：

```text
Container / VM
```

适合：

```text
bash
git
npm
python
go
docker
pytest
build
compile
```

官方也明确区分 interpreter 和 sandbox：Interpreter 是 Agent loop 内的受限 JS runtime，Sandbox 是 OS 级执行环境。([Docs by LangChain][13])

生产平台必须：

```text
Agent Runtime Pod
        │
        │ API
        ▼
Sandbox
```

而不是：

```text
FastAPI Pod
   ↓
subprocess.run()
```

后者在多租户环境里基本是在给自己埋雷。

DeepAgents 官方 sandbox 当前支持 LangSmith、Daytona、E2B、Modal、Runloop、Vercel 等集成，也允许 backend protocol 扩展。([Docs by LangChain][14])

你本身有 Kubernetes 环境的话，完全可以进一步实现：

```text
KubernetesSandboxBackend
```

创建隔离 Pod：

```text
deepagent-runtime
      │
      ▼
sandbox-manager
      │
      ▼
Kubernetes API
      │
      ▼
sandbox-xxxx Pod
```

这会很适合你现在的基础设施。

---

# 十九、Streaming 不能只实现 token SSE

传统 LLM：

```text
token
token
token
```

DeepAgent 的 stream 是：

```text
Main Agent message
Tool call
Tool result
Todo update
SubAgent started
SubAgent message
SubAgent tool call
SubAgent completed
HITL interrupt
Artifact created
Final answer
```

当前 DeepAgents Event Streaming 可以分别观察：

```text
messages
tool_calls
values
subagents
output
```

而 subagent 本身又可以拥有自己的 messages、tools、nested subagents 和 output。([Docs by LangChain][15])

所以你的 Runtime Event 应统一成：

```json
{
  "event": "subagent.started",
  "run_id": "run_xxx",
  "agent_id": "researcher",
  "parent_agent_id": "main",
  "timestamp": "..."
}
```

前端才能真正做成：

```text
Main Agent
 │
 ├─ thinking...
 │
 ├─ search()
 │
 ├─ researcher
 │    ├─ search()
 │    └─ finished
 │
 ├─ coder
 │    ├─ read_file()
 │    └─ finished
 │
 └─ answer
```

而不是一个黑盒聊天框。

---

# 二十、Native API 和 OpenAI API 要同时存在

这是我非常建议你的一个设计。

## OpenAI-compatible API

继续支持：

```http
POST /v1/chat/completions
```

方便普通客户端接入。

但是它只能表达：

```text
message
tool_calls
stream token
```

对于：

```text
SubAgent
Todo
AsyncTask
HITL
Artifact
Sandbox
Filesystem
```

表达能力远远不够。

因此平台同时必须有：

```http
POST /api/v1/agents/{agent_id}/threads

POST /api/v1/threads/{thread_id}/runs

GET /api/v1/runs/{run_id}

GET /api/v1/runs/{run_id}/events

POST /api/v1/runs/{run_id}/resume

GET /api/v1/runs/{run_id}/tasks

POST /api/v1/tasks/{task_id}/cancel

POST /api/v1/tasks/{task_id}/update
```

Streaming：

```http
GET /api/v1/runs/{run_id}/stream
Accept: text/event-stream
```

以后复杂前端走：

```text
Native Agent API
```

普通第三方走：

```text
OpenAI Compatibility API
```

---

# 二十一、数据库核心模型

我建议至少有：

```text
model_provider
model

agent
agent_version

prompt

tool
agent_tool

mcp_server
agent_mcp_server

skill
skill_version
agent_skill

memory
agent_memory

subagent
agent_subagent

backend_profile
sandbox_profile
permission_policy

knowledge_base

thread
message

run
run_event
checkpoint

interrupt
approval

async_task

artifact

evaluation
trace
```

特别注意：

```text
Agent
```

不要直接更新。

应该：

```text
agent
   ↓
agent_version
```

例如：

```text
Agent: frontend-reviewer

v1
v2
v3
v4
```

Run 必须绑定：

```text
agent_version = v3
```

否则以后 Agent Prompt 改了，你根本无法复现历史运行。

---

# 二十二、最终代码架构

我建议 Python 后端项目最终长这样：

```text
deepagent-platform/
│
├── app/
│
│   ├── api/
│   │   ├── agents/
│   │   ├── models/
│   │   ├── tools/
│   │   ├── mcp/
│   │   ├── skills/
│   │   ├── memory/
│   │   ├── threads/
│   │   ├── runs/
│   │   ├── approvals/
│   │   └── sandboxes/
│   │
│   ├── domain/
│   │   ├── agent/
│   │   ├── model/
│   │   ├── tool/
│   │   ├── skill/
│   │   ├── memory/
│   │   ├── runtime/
│   │   └── sandbox/
│   │
│   ├── runtime/
│   │   ├── compiler.py
│   │   ├── executor.py
│   │   ├── event_stream.py
│   │   │
│   │   ├── resolvers/
│   │   │   ├── model.py
│   │   │   ├── tools.py
│   │   │   ├── mcp.py
│   │   │   ├── skills.py
│   │   │   ├── memory.py
│   │   │   ├── subagents.py
│   │   │   ├── backend.py
│   │   │   └── middleware.py
│   │   │
│   │   └── deepagents/
│   │       ├── factory.py
│   │       ├── context.py
│   │       └── state.py
│   │
│   ├── models/
│   │   ├── registry.py
│   │   ├── factory.py
│   │   ├── openai.py
│   │   ├── anthropic.py
│   │   ├── ollama.py
│   │   └── vllm.py
│   │
│   ├── tools/
│   │   ├── registry.py
│   │   ├── builtin/
│   │   └── mcp/
│   │
│   ├── storage/
│   │   ├── checkpoint.py
│   │   ├── store.py
│   │   ├── object_storage.py
│   │   └── vector_store.py
│   │
│   ├── sandbox/
│   │   ├── manager.py
│   │   ├── kubernetes.py
│   │   └── lifecycle.py
│   │
│   ├── observability/
│   │   ├── tracing.py
│   │   ├── metrics.py
│   │   └── usage.py
│   │
│   └── main.py
│
├── migrations/
├── tests/
├── pyproject.toml
└── docker-compose.yml
```

---

# 二十三、运行一条用户消息时到底发生什么

最终运行链路应该是：

```text
User
 │
 ▼
POST /threads/{id}/runs
 │
 ▼
RunService
 │
 ├─ load AgentVersion
 │
 ▼
AgentCompiler
 │
 ├─ resolve Model
 ├─ resolve Tools
 ├─ resolve MCP
 ├─ resolve Skills
 ├─ resolve Memory
 ├─ resolve Backend
 ├─ resolve Sandbox
 ├─ resolve SubAgents
 ├─ resolve Middleware
 ├─ resolve HITL
 │
 ▼
create_deep_agent()
 │
 ▼
CompiledStateGraph
 │
 ▼
LangGraph Runtime
 │
 ├─ checkpoint
 ├─ stream
 ├─ interrupt
 ├─ state
 │
 ▼
DeepAgents Middleware Stack
 │
 ├─ Skills
 ├─ Filesystem
 ├─ SubAgents
 ├─ Summarization
 ├─ PatchToolCalls
 ├─ AsyncSubAgents
 ├─ Custom Middleware
 ├─ Profiles
 ├─ Prompt Cache
 ├─ Memory
 └─ HITL
 │
 ▼
Model
```

这个 Middleware 顺序不是随便画的；DeepAgents 当前自身就是按类似的固定顺序组装核心 stack，包括 Skills、Filesystem、SubAgent、Summarization、PatchToolCalls、AsyncSubAgent、自定义 middleware、profile、prompt caching、Memory 和 HITL。([Docs by LangChain][4])

---

# 二十四、我认为这个平台最重要的 5 个抽象

如果把上面几千字压缩成真正需要先写代码的东西，其实就是：

```text
AgentDefinition
       │
       ▼
AgentCompiler
       │
       ▼
AgentRuntime
       │
       ├──────── RuntimeContext
       │
       └──────── RuntimeEvent
```

再加一个：

```text
ResourceRegistry
```

于是整个系统实际上变成：

```text
                     Resource Registry
                 ┌────────┼─────────┐
                 │        │         │
               Model     Tool      Skill
                 │        │         │
               MCP     Memory    SubAgent
                 └────────┼─────────┘
                          │
                          ▼
                  AgentDefinition
                          │
                          ▼
                    AgentCompiler
                          │
                          ▼
                  create_deep_agent
                          │
                          ▼
                    AgentRuntime
                          │
               ┌──────────┼──────────┐
               ▼          ▼          ▼
             State      Events    Artifacts
```

这才是我建议你真正实现的 **DeepAgents LLM Platform v1 架构**。

---

# 二十五、还有一个非常关键的产品设计

前端创建 Agent 的页面，我不会直接暴露：

```python
create_deep_agent(...)
```

这种开发者参数。

而应该设计成：

```text
Create Agent

Basic
────────────────────────────
Name
Description
Model
System Prompt

Capabilities
────────────────────────────
☑ Filesystem
☑ Planning
☑ Memory
☑ Skills
☑ Knowledge
☑ Interpreter
☑ Sandbox
☑ Human Approval

Tools
────────────────────────────
Github
Kubernetes
Database
Web Search

Subagents
────────────────────────────
Researcher
Coder
Reviewer

Storage
────────────────────────────
Thread Storage
Long-term Memory
Workspace

Permissions
────────────────────────────
Filesystem Rules
Tool Approval Rules

Advanced
────────────────────────────
Middleware
Structured Output
Model Profile
Context Schema
```

用户配置完：

```text
                 UI Configuration
                        ↓
                 AgentDefinition
                        ↓
                  Agent Compiler
                        ↓
                   DeepAgents
```

这样你以后即使不用 DeepAgents，换一个 Agent Runtime：

```text
DeepAgents
      ↓
Other Agent Harness
```

你的：

```text
AgentDefinition
Tool Registry
Model Registry
Skill Registry
Memory
Thread
Run
UI
```

都不用推倒重写。

这点非常重要。

---

## 最后的技术判断

**DeepAgents 不应该成为你的“LLM 平台本身”，它应该成为你平台里的第一个 Agent Runtime Engine。**

也就是：

```text
LLM Platform
│
├── Model Layer
│
├── Tool Layer
│
├── Knowledge Layer
│
├── Memory Layer
│
├── Agent Control Plane
│
└── Runtime Engine
      │
      ├── DeepAgents   ← v1
      │
      ├── LangGraph    ← 后续自定义 Workflow
      │
      └── Other        ← 将来可扩展
```

我尤其推荐这种设计，因为 DeepAgents 本身就是构建在 LangChain/LangGraph 上的 opinionated harness，而不是另一个不可拆解的运行时。这样你的平台既可以 **100% 使用 DeepAgents 的 Filesystem、Skills、Memory、Planning、Sync/Dynamic/Async SubAgent、Sandbox、Interpreter、HITL、Streaming 等特性**，又不会在架构上被 `create_deep_agent()` 锁死。([GitHub][16])

下一步真正应该做的已经不是继续讨论概念了，而是直接开始 **DeepAgent Platform v1 的后端工程设计**：我建议从 `AgentDefinition + PostgreSQL 数据模型 + AgentCompiler + FastAPI Run API + SSE Event Protocol` 这五块开始，这五块一旦跑通，后面的 Skill、MCP、SubAgent、Sandbox、HITL 都是在同一套骨架上继续加能力。

[1]: https://github.com/langchain-ai/deepagents/blob/main/openwiki/architecture/overview.md?utm_source=chatgpt.com "deepagents/openwiki/architecture/overview.md at main · langchain-ai/deepagents · GitHub"
[2]: https://docs.langchain.com/oss/python/deepagents/tools "Tools - Docs by LangChain"
[3]: https://docs.langchain.com/oss/python/deepagents/subagents "Subagents - Docs by LangChain"
[4]: https://docs.langchain.com/oss/python/deepagents/customization "Customize Deep Agents - Docs by LangChain"
[5]: https://docs.langchain.com/oss/python/deepagents/models?utm_source=chatgpt.com "Models - Docs by LangChain"
[6]: https://docs.langchain.com/oss/python/deepagents/backends "Backends - Docs by LangChain"
[7]: https://docs.langchain.com/oss/python/deepagents/overview "Deep Agents overview - Docs by LangChain"
[8]: https://docs.langchain.com/oss/python/deepagents/skills "Skills - Docs by LangChain"
[9]: https://docs.langchain.com/oss/python/deepagents/retrieval "Retrieval - Docs by LangChain"
[10]: https://docs.langchain.com/oss/python/deepagents/async-subagents "Async subagents - Docs by LangChain"
[11]: https://docs.langchain.com/oss/python/deepagents/context-engineering "Context engineering in Deep Agents - Docs by LangChain"
[12]: https://docs.langchain.com/oss/python/deepagents/human-in-the-loop "Human-in-the-loop - Docs by LangChain"
[13]: https://docs.langchain.com/oss/python/deepagents/interpreters "Interpreters - Docs by LangChain"
[14]: https://docs.langchain.com/oss/python/deepagents/sandboxes "Sandboxes - Docs by LangChain"
[15]: https://docs.langchain.com/oss/python/deepagents/event-streaming "Event streaming - Docs by LangChain"
[16]: https://github.com/langchain-ai/deepagents/blob/main/README.md?utm_source=chatgpt.com "deepagents/README.md at main · langchain-ai/deepagents · GitHub"
