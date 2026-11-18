# AG-UI 协议与会话执行过程

Console 使用 AG-UI 1.0 的 `RunAgentInput` 和 SSE 事件，后端通过官方 Python `ag-ui-protocol` 编码，前端使用官方 `@ag-ui/core` 类型和运行时 Schema 校验。模型仍通过本项目的 Responses API 客户端调用，LangGraph 的流接口使用 `version="v3"`。

协议依据：[Run Input](https://docs.ag-ui.com/spec/1.0/basic/run-input)、[事件模型](https://github.com/ag-ui-protocol/ag-ui/blob/main/docs/spec/1.0/basic/index.mdx)、[工具调用](https://github.com/ag-ui-protocol/ag-ui/blob/main/docs/spec/1.0/events/tool-calls.mdx)。

## 入口与身份

`POST /api/v1/ag-ui` 接收 JSON，返回 `Content-Type: text/event-stream`。使用原业务令牌 `Authorization: Bearer ...`，不接受模型供应商密钥作为业务登录凭据。该入口不暴露为 Agent 业务工具。

首次请求示例：

```json
{
  "protocolVersion": "1.0",
  "threadId": "conversation-001",
  "runId": "request-001",
  "messages": [
    { "id": "human-001", "role": "user", "content": "查看我的账号信息" }
  ],
  "state": {},
  "tools": [],
  "context": [],
  "forwardedProps": { "model": "ZhipuAI/GLM-5.3" }
}
```

模型名称以 `GET /v1/models` 为准。可在 `forwardedProps` 中传递现有 `temperature`、`top_p`、`max_tokens`、`max_completion_tokens` 和 `tool_choice` 参数。`context` 的描述和值作为用户提供的参考上下文进入模型；不支持的多媒体内容部分跳过，文本部分继续处理。

三个标识分别表示：

| 标识 | 含义 |
| --- | --- |
| `threadId` | 浏览器会话 ID，对应服务端 `conversation_id` |
| AG-UI `runId` | 本次 HTTP 执行段的请求 ID；开始和结束事件原样返回此 ID |
| `snapshot.run.id` | 服务端持久化任务 ID；暂停、恢复、执行去重均沿用此 ID |

同一 `runId` 重复发起相同初始请求会返回已有状态。每次确认或恢复使用新的 AG-UI `runId`，不重新创建原业务任务。服务端验证任务所有者与 `threadId`；客户端 `state`、`context`、消息和 `forwardedProps` 均不能代替身份、RBAC 或用户工具授权。

业务工具由服务端实时目录提供。`tools` 代表 AG-UI 的客户端工具，此应用暂不执行这类工具；非空列表返回 `AGUI_FRONTEND_TOOLS_UNSUPPORTED`，避免客户端自定义函数绕过现有执行门禁。

## 事件与页面显示

| AG-UI 事件 | 含义与页面行为 |
| --- | --- |
| `RUN_STARTED` | 执行段开始，包含协议版本、会话和请求 ID |
| `STEP_STARTED` / `STEP_FINISHED` | 真实节点开始和结束：检查工具权限、模型响应、工具处理 |
| `REASONING_START` / `REASONING_END` | 模型公开返回的思考摘要区间 |
| `REASONING_MESSAGE_START` / `CONTENT` / `END` | 思考摘要流，与最终回复分开显示 |
| `TOOL_CALL_START` / `ARGS` / `END` | 校验后即将执行的真实工具及最终脱敏参数；`END` 只表示参数传输结束 |
| `TOOL_CALL_RESULT` | 工具执行结果，包含成功或业务错误，不把调用声明当成执行成功 |
| `TEXT_MESSAGE_START` / `CONTENT` / `END` | 助手 Markdown 回复流 |
| `ACTIVITY_SNAPSHOT` | `activityType=workflow`，更新单个步骤的公开展示状态 |
| `STATE_SNAPSHOT` | `snapshot.run` 为权威任务快照，包含工作流、HITL 卡片、用量和执行记录 |
| `RUN_FINISHED` | 正常结束、取消或人工中断，通过 `outcome` 区分 |
| `RUN_ERROR` | 本执行段失败，返回安全错误；前端可读取任务状态后恢复 |

每条助手消息包含“执行过程”：步骤按实际发生顺序排列，显示待执行、进行中、等待用户、成功、失败、取消和中断。工具条目显示业务名称与真实函数名，可展开查看参数和结果。模型未提供摘要时，页面明确说明未返回可展示内容；不会生成一段文字冒充模型思考。

工具选择和等待用户阶段通过工作流活动展示；实际进入工具执行或返回拒绝结果时才发送工具调用事件。人工补参不会被提前记成一次已执行调用，后续 `TOOL_CALL_ARGS` 使用最终确认的参数。

模型摘要来自公开响应内容块或 `reasoning_content`，支持流式更新。当前 Responses 客户端已请求 `reasoning.summary=auto`；实际是否返回及其内容由供应商决定。私有系统提示词、加密 reasoning 值、供应商原始异常、登录令牌和表单密码不会放入工作流事件。

`ACTIVITY_SNAPSHOT` 是标准 AG-UI 事件，其 `content` 使用应用展示结构：`id`、`kind`、`title`、`status`、`detail`、`started_at`、`updated_at`，以及可选的 `reasoning`、`tool_name`、`operation_id`、`arguments`、`result`。前端按 ID 合并更新；终态快照负责最终对齐。模型重试产生新的步骤条目，已提交工具的执行回执继续去重。

服务进程中断或执行租约过期后，恢复任务会先把上一轮未完成的步骤记为中断；仍需人工处理的工具恢复为“等待用户”，避免页面同时显示旧模型仍在运行或已恢复的表单仍然中断。

## 参数填写、确认与恢复

需要人工处理时，服务端先保存交互记录，发送含卡片的 `STATE_SNAPSHOT`，再以原生 AG-UI 中断结束本执行段：

```json
{
  "type": "RUN_FINISHED",
  "threadId": "conversation-001",
  "runId": "request-001",
  "outcome": {
    "type": "interrupt",
    "interrupts": [
      { "id": "int_example", "reason": "tool_interaction", "message": "新增用户" }
    ]
  }
}
```

确认时对同一入口发送新的 `RunAgentInput`。以下为字段示例；`messages` 使用当前会话累计消息，`state` 使用上一次 `STATE_SNAPSHOT.snapshot`，不能以空数组、空对象替代实际的会话状态：

```json
{
  "protocolVersion": "1.0",
  "threadId": "conversation-001",
  "runId": "submission-001",
  "messages": [{ "id": "human-001", "role": "user", "content": "新增用户" }],
  "state": { "run": { "id": "run_example", "status": "waiting" } },
  "tools": [],
  "context": [],
  "forwardedProps": { "action": "resume", "businessRunId": "run_example" },
  "resume": [
    {
      "interruptId": "int_example",
      "status": "resolved",
      "payload": {
        "expected_version": 1,
        "values": { "/body/display_name": "示例用户" },
        "sensitive_values": {},
        "omit": []
      }
    }
  ]
}
```

`state.run` 的示例仅列出标识，实际应原样携带完整快照；`interruptId`、版本和字段均以当前卡片为准，示例不是完整的创建用户参数。`runId` 同时作为本次提交的去重 ID；重复提交相同 ID 返回已有状态，变更参数后使用新 ID。敏感字段只在 `sensitive_values` 中提交。`RUN_STARTED` 不回显输入，密码不进入模型、工作流、checkpoint 或浏览器持久存储。

Console 的“取消操作”发送原生 `resume[0].status=cancelled`，携带当前卡片的 `interruptId` 和 `expected_version`。服务端接受后返回已取消的任务快照。同一 `runId` 可以重试这次决定。另一窗口已确认或更换卡片时，旧卡片会提示读取最新任务状态。会话的“停止生成”仍通过任务级取消接口停止整个任务。

断流后先读取原任务状态；需继续图执行时发送 `forwardedProps={"action":"recover","businessRunId":"run_example"}`，使用新 `runId`、相同 `threadId`、空 `resume`。后端恢复原 checkpoint，读取已提交的执行回执，不重放写入。角色或工具授权被撤回、版本冲突、交互过期时继续由原 HITL 逻辑拒绝，不因换用 AG-UI 放宽。

## 存储与兼容

schema 5 新增 `agent_workflow_entries`。步骤在运行时更新，任务快照、刷新与恢复可重新读取。已有 schema 4 的等待任务保持有效，恢复执行时补充新的过程记录；不会伪造升级前未记录的历史。每任务最多 256 个条目，单条思考文本最多 64,000 字符，参数和结果超过展示限制时给出截断预览。

工作流采用原任务所有权和保留时间。任务正文清理时一并删除服务端过程记录；浏览器按账号保存已收到的公开历史，历史存储失败时沿用现有提示。SQLite 和 PostgreSQL 均在事务内升级，迁移器支持 schema 2、3、4、5，并迁移 schema 5 的工作流记录。升级前备份业务数据库及 HITL 摘要密钥。

原 `/api/v1/chat/runs` 命名 SSE 与任务管理接口继续可用，增加可忽略的 `workflow.updated` 事件。`/v1/chat/completions` 保持 OpenAI SDK 格式，不混入 AG-UI 事件；两种协议共用同一执行引擎、检查点和权限门禁。

## 验证

```bash
make -C agent_permissions test-python
cd agent_permissions/console
npm run test:e2e
```

`test_agui.py` 使用官方 Python SDK 校验所有输出事件，并验证实际读取、原生中断/恢复、去重、撤权、秘密隔离和旧任务升级。`tests/agui.spec.ts` 验证字节拆包、身份和生命周期、断流/取消，以及真实页面中的实时思考、工具结果、窄屏和刷新恢复。模型使用确定性测试实现，业务接口和临时数据库真实运行。PostgreSQL 集成测试还复用相同 AG-UI 契约。
