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

## 快速启动

```bash
cd /Users/zhengjie/Github/Z/deepAgentsProject
make install
make build
make api
```

打开 [http://localhost:8000](http://localhost:8000)。API 文档位于 [http://localhost:8000/docs](http://localhost:8000/docs)。

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
├── adapters/harness/      Deep Agents 稳定适配边界
└── persistence/           SQLite 参考仓储与 Schema
tests/                      端到端平台契约测试
```

## Reference Harness 与真实 Deep Agents

当前仓库内置 Reference Harness，目的是让发布、调度、流式、HITL、恢复、审计和计费在无任何外部凭据时全部可验证。生产接入时，只需在 `packages/adapters/harness/deepagents` 中使用锁定版本的 `create_deep_agent()` 构建 LangGraph Runnable，并将 SDK 事件转换成 `Platform RuntimeEvent`；API、领域对象和控制台无需改变。

真实 Credential 禁止进入 Revision、Plan、Checkpoint、Event 或 Prompt。当前 Runtime Binder 只产生短期 opaque handle，生产实现应由 Credential Broker 在实际工具调用前兑换。

## 当前边界

本实现交付文档定义的 Phase 1 核心运行骨架。Kubernetes Sandbox、真实 MCP Session、向量知识库、外部模型路由和 Preview/Beta Dynamic/Async SubAgent 保留在后续阶段；对应领域边界已预留，但不会用不安全的本机 `subprocess` 或伪造的 SDK 调用冒充生产实现。
