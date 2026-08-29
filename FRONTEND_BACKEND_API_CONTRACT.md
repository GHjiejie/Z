# 前后端统一 API 契约

## 1. 目标

本仓库允许在不同 Demo 目录中使用不同技术方案实现后端，但这些后端对前端暴露的 API 必须遵守同一份契约。

目标是：**前端切换后端 Demo 时，只修改 `proxyTarget`，不修改请求路径、请求参数、响应解析、错误处理或流式事件处理代码。**

> 仅仅让接口“功能相近”不算兼容。HTTP 方法、URL、Header、请求体、响应体、状态码和流式事件中任意一项不同，都会破坏前端兼容性。

本文使用以下约束词：

- **必须（MUST）**：所有供统一前端使用的 Demo 都要实现。
- **禁止（MUST NOT）**：实现中不得出现。
- **应该（SHOULD）**：没有明确理由时必须遵守。

## 2. 总体原则

### 2.1 唯一契约，多个实现

- 前端 API 契约只有一份。各 Demo 是这份契约的不同实现，不得自行设计一套仅供自己使用的对外接口。
- 新增 Demo 前，应先确认已有契约是否能够表达需求。能表达时直接实现，不能表达时先评审并更新统一契约，再修改前端和所有受影响的 Demo。
- Demo 内部的模块、Agent 状态、数据库结构、模型供应商和事件名称可以不同，但不得泄漏到对外 API。
- Demo 专属的调试接口必须放在 `/internal/*` 下，统一前端不得依赖这些接口。

### 2.2 固定路径，地址由代理切换

- 统一前缀为 `/api/v1`。
- 前端必须使用相对路径，例如 `fetch("/api/v1/health")`，禁止在业务代码中写后端协议、域名或端口。
- 后端路由中禁止包含 Demo 名称，例如禁止 `/api/v1/checkpoint-demo/sessions`。
- 不同环境或 Demo 的地址差异只能由开发代理或网关的 `proxyTarget` 处理。

推荐的 Vite 配置：

```ts
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxyTarget = env.VITE_PROXY_TARGET || "http://127.0.0.1:8000";

  return {
    server: {
      proxy: {
        "/api": {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
  };
});
```

切换 Demo 时只允许修改：

```dotenv
VITE_PROXY_TARGET=http://127.0.0.1:8001
```

代理不得重写 `/api/v1` 后的业务路径，否则会掩盖后端实现不兼容的问题。

## 3. 接口不变量

统一前端依赖的每个接口都必须固定以下内容：

1. HTTP 方法和完整路径。
2. Path、Query、Header 参数的名称、类型、是否必填及默认值。
3. 请求体字段的名称、类型、是否必填、默认值及 `null` 语义。
4. 每个状态码对应的响应体结构。
5. 枚举值、日期格式、ID 格式和分页规则。
6. SSE/WebSocket 的事件名、顺序、数据结构、结束与重连语义。
7. 幂等、取消、超时、重试和并发行为。

后端不得因为内部实现方便而执行以下操作：

- 把 `session_id` 改成 `thread_id`，或在不同 Demo 中混用两种名称。
- 把同一字段从字符串改为数字、对象或 `null`。
- 在某个 Demo 中返回裸对象，在另一个 Demo 中额外包一层 `data`。
- 用 HTTP 200 返回业务失败，同时让前端解析自定义的成功标志。
- 改变枚举值的大小写，例如混用 `RUNNING`、`running`。
- 用空字符串、空对象和 `null` 表达同一种状态。
- 让接口路径或响应字段携带具体框架、模型或 Demo 的名称。

## 4. 通用协议规范

### 4.1 HTTP 与 JSON

- API 使用 HTTPS（本地开发可以使用 HTTP）。
- 普通请求和响应使用 `application/json; charset=utf-8`。
- JSON 字段统一使用 `snake_case`。
- ID 对外统一表示为非空字符串。内部即使使用整数，对外也不得改变类型。
- 时间统一使用 UTC 的 RFC 3339 格式，例如 `2026-08-29T06:30:00Z`。
- 布尔值必须使用 JSON `true`/`false`，不得使用 `0`/`1` 或字符串。
- 未提供的可选字段与值为 `null` 是不同语义；接口文档必须明确两者行为。
- 服务端应在响应头返回 `X-Request-Id`。客户端传入该 Header 时，服务端应复用；未传入时由服务端生成。

### 4.2 成功响应

单个资源统一返回：

```json
{
  "data": {
    "id": "resource_123"
  },
  "meta": {
    "request_id": "req_123"
  }
}
```

集合统一返回，禁止直接返回裸数组：

```json
{
  "data": [
    {
      "id": "resource_123"
    }
  ],
  "meta": {
    "request_id": "req_123",
    "pagination": {
      "next_cursor": null,
      "has_more": false
    }
  }
}
```

- 无响应体的成功操作使用 `204 No Content`。
- 创建资源使用 `201 Created`；响应体仍遵循单资源结构。
- 分页统一使用不透明字符串游标 `cursor` 和 `limit`。默认 `limit=20`，最大值 `100`。
- 未到最后一页时，`has_more` 为 `true` 且 `next_cursor` 必须为非空字符串。

### 4.3 错误响应

所有非 2xx 响应必须使用同一结构：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "details": [
      {
        "field": "message",
        "reason": "must_not_be_empty"
      }
    ],
    "request_id": "req_123",
    "retryable": false
  }
}
```

约束如下：

- `code` 是供前端分支判断的稳定机器码，必须使用大写 `SNAKE_CASE`。
- `message` 是可展示的简短说明，但前端不得依赖其文本做逻辑判断。
- `details` 必须是数组；没有详情时返回空数组。
- `retryable` 明确前端是否可以安全重试。
- 禁止直接向前端返回 Python/JavaScript 异常、调用栈、SQL、文件路径或模型密钥等内部信息。

最低限度统一以下状态码：

| 状态码 | 使用场景 | 建议错误码 |
| --- | --- | --- |
| `400` | 请求语义错误 | `INVALID_REQUEST` |
| `401` | 未认证或凭证失效 | `UNAUTHORIZED` |
| `403` | 已认证但无权限 | `FORBIDDEN` |
| `404` | 资源不存在 | `RESOURCE_NOT_FOUND` |
| `409` | 状态或并发冲突 | `CONFLICT` |
| `422` | 字段校验失败 | `VALIDATION_ERROR` |
| `429` | 触发限流 | `RATE_LIMITED` |
| `500` | 未预期的服务端错误 | `INTERNAL_ERROR` |
| `503` | 暂时不可用 | `SERVICE_UNAVAILABLE` |
| `504` | 上游或模型调用超时 | `UPSTREAM_TIMEOUT` |

### 4.4 流式接口（SSE）

需要流式返回时统一使用 Server-Sent Events：

- 请求必须支持 `Accept: text/event-stream`。
- 响应必须使用 `Content-Type: text/event-stream; charset=utf-8` 和 `Cache-Control: no-cache`。
- 每个事件必须包含 `id`、`event` 和单行 JSON `data`。
- `id` 在同一条流中必须单调递增，并可用于断线续传。
- 心跳事件只用于保活，不得改变业务状态。
- 业务结束必须发送一次 `done`；发送后服务端关闭连接。
- HTTP 响应头发出后发生的错误通过 `error` 事件表达，并复用普通错误结构。

事件示例：

```text
id: 12
event: delta
data: {"version":"1","sequence":12,"delta":{"type":"text","content":"hello"}}

id: 13
event: done
data: {"version":"1","sequence":13,"result":{"finish_reason":"stop"}}

```

统一保留以下事件名：

| 事件 | 用途 | 要求 |
| --- | --- | --- |
| `started` | 服务端已接受任务 | 每条业务流最多一次 |
| `delta` | 增量内容 | 可出现多次，按 `sequence` 消费 |
| `state` | 可见业务状态更新 | 内容必须是完整状态或明确的 JSON Patch，不能混用 |
| `error` | 流内失败 | 必须包含 `error` 对象，之后只能发送 `done` |
| `done` | 流结束 | 必须且只能出现一次 |
| `heartbeat` | 连接保活 | 前端应忽略其业务含义 |

不同 Demo 如果产生不同的内部事件，必须在后端适配成上述公共事件，禁止要求前端识别 Demo 专属事件名。

## 5. 能力差异处理

不同 Demo 的能力可能不同，但差异必须被公共协议吸收，不能通过换接口实现。

所有 Demo 必须提供：

```http
GET /api/v1/health
GET /api/v1/capabilities
```

`GET /api/v1/health` 在服务可接受请求时返回：

```json
{
  "data": {
    "status": "ok",
    "api_version": "1.0.0"
  },
  "meta": {
    "request_id": "req_123"
  }
}
```

`GET /api/v1/capabilities` 返回稳定的能力键：

```json
{
  "data": {
    "streaming": true,
    "checkpointing": false,
    "human_approval": false,
    "file_artifacts": true
  },
  "meta": {
    "request_id": "req_123"
  }
}
```

- 能力键由统一契约维护，Demo 不得自行创建同义键。
- 前端可以依据能力值隐藏或禁用功能，但切换 Demo 时不得修改前端源码。
- 一个 Demo 声明某能力为 `true` 时，必须完整实现该能力对应的全部接口和行为。
- 必选核心能力对应的接口不允许缺失。可选能力为 `false` 时，前端不应调用其接口；误调用时后端返回 `409 CAPABILITY_NOT_SUPPORTED`，不得返回格式不一致的临时结果。

## 6. API 文档的唯一标准

对外接口必须先写契约、后写实现。统一 API 应维护一份 OpenAPI 3.1 文档作为机器可读的唯一事实来源；Markdown、Swagger UI 和代码类型定义都由它生成或与它校验。

每个接口在 OpenAPI 中必须完整描述：

- 稳定且唯一的 `operationId`。
- 方法、路径、用途和是否属于可选能力。
- 所有参数、请求体、响应体和 Header。
- 每个字段的类型、格式、必填性、默认值、枚举值和说明。
- 所有可能状态码及其响应 Schema。
- 至少一组成功示例和一组失败示例。
- 对更新类接口说明幂等性、并发和重试行为。
- 对流式接口说明事件 Schema、事件顺序、终止和断线恢复行为。

每个 Demo 还必须在固定地址暴露与统一契约一致的文档：

```http
GET /api/v1/openapi.json
```

禁止只提供框架自动生成但未经整理的文档。自动生成的字段名或错误响应只要与统一契约不一致，就必须在后端增加适配层。

### 6.1 单接口文档模板

在设计或评审新接口时，至少填写以下内容：

```md
### [接口名称]

- 能力归属：core / streaming / checkpointing / ...
- Method：POST
- Path：/api/v1/...
- operationId：...
- 用途：...
- 认证：...
- 幂等性：...
- 超时与重试：...

#### Path / Query / Header 参数

| 名称 | 位置 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |

#### 请求体

[JSON Schema 和示例]

#### 成功响应

| 状态码 | Schema | 说明 |
| --- | --- | --- |

[JSON 示例]

#### 失败响应

| 状态码 | error.code | retryable | 触发条件 |
| --- | --- | --- | --- |

#### 兼容性说明

[新增字段、废弃计划、流式顺序或并发约束]
```

## 7. 版本与变更规则

- `/api/v1` 中的变更默认必须向后兼容。
- 允许：新增可选响应字段、新增可选请求字段、新增能力键、新增接口。
- 禁止：删除或重命名字段、修改字段类型或含义、把可选字段改为必填、修改默认值、删除枚举值、改变状态码或事件顺序。
- 新增响应字段时，前端必须能够忽略未知字段。
- 新增枚举值不是天然兼容变更；必须先确认前端有兜底处理。
- 破坏性变更必须创建新的主版本前缀（例如 `/api/v2`），并给出迁移期，禁止在 `/api/v1` 中静默上线。
- 字段废弃应先在 OpenAPI 中标记 `deprecated: true`，保留至少一个明确约定的迁移周期后才能在下一主版本删除。

## 8. 新 Demo 接入流程

新后端只有完成以下步骤，才能接入统一前端：

1. 读取统一 OpenAPI 契约，不从已有后端代码反向猜测接口。
2. 在 Demo 内实现 `/api/v1` 适配层，将内部模型转换为公共请求、响应和事件模型。
3. 实现 `health`、`capabilities` 和该 Demo 声明支持的所有接口。
4. 暴露 `/api/v1/openapi.json`，并与统一契约做自动差异检查。
5. 运行公共契约测试，至少覆盖成功、校验失败、资源不存在、并发冲突、服务端失败和流中断。
6. 启动统一前端，只修改 `VITE_PROXY_TARGET`，执行核心用户流程回归。
7. 确认浏览器 Network 中没有 Demo 专属 URL、字段或事件后，才可标记接入完成。

## 9. 验收清单

- [ ] 前端业务代码中不存在后端域名、端口或 Demo 名称。
- [ ] 切换后端只修改 `proxyTarget`/`VITE_PROXY_TARGET`。
- [ ] 所有公共路由都以 `/api/v1` 开头，并且代理未做业务路径重写。
- [ ] 各 Demo 的方法、路径、参数、响应 Schema、状态码完全一致。
- [ ] 单资源、集合和错误响应使用统一结构。
- [ ] ID、时间、枚举、空值和分页语义一致。
- [ ] 流式事件名称、Schema、顺序、结束及错误行为一致。
- [ ] 能力差异通过 `/api/v1/capabilities` 表达，不通过修改前端代码表达。
- [ ] `/api/v1/openapi.json` 与统一契约一致。
- [ ] 公共契约测试在所有 Demo 上通过。
- [ ] 更换至少两个 Demo 进行过“仅改代理地址”的端到端验证。

## 10. 最终判定标准

如果切换到另一个 Demo 后还需要修改以下任意内容，则该 Demo **不兼容统一前端**：

- 前端 `fetch`/HTTP Client 的 URL。
- 请求参数、请求体或 Header。
- TypeScript 类型。
- 响应解包或错误处理逻辑。
- SSE 事件解析、事件名或结束条件。
- 页面为了识别 Demo 而增加的硬编码分支。

兼容实现的唯一允许改动是代理目标，例如：

```diff
- VITE_PROXY_TARGET=http://127.0.0.1:8000
+ VITE_PROXY_TARGET=http://127.0.0.1:8001
```
