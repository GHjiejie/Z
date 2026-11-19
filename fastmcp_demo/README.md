# 用 FastMCP 从 OpenAPI 生成工具

示例用通知中心的迭代任务演示 `OpenAPI → MCP Tool → HTTP API`。FastAPI 提供 `/openapi.json`，MCP 服务把 `app.openapi()` 返回的同一份规范交给 `FastMCP.from_openapi()`。FastMCP 以接口的 `operationId` 命名工具，从参数声明生成输入 Schema，并在工具调用时发出对应的 HTTP 请求。 [FastMCP 3 OpenAPI 集成](https://github.com/PrefectHQ/fastmcp/blob/v3.4.11/docs/integrations/openapi.mdx)

## 运行

在仓库根目录用 `uv` 执行。`mcp` 是根项目的依赖组，使用仓库的 Python 3.13 环境和兼容现有 MCP Python SDK 的 FastMCP 3.4 系列。

```bash
uv run --locked --group mcp python -m fastmcp_demo.client
```

客户端通过 stdio 启动 MCP 服务，打印生成的工具及输入 Schema，再调用工具筛选任务、读取详情及统计数量。示例 API 通过 `httpx.ASGITransport` 在 MCP 服务进程内处理请求，安装依赖后可离线运行。

## API 与生成的工具

| API 操作 | MCP Tool | 输入 |
| --- | --- | --- |
| `GET /tasks` | `list_tasks` | 可选查询参数 `status`，支持 `todo`、`in_progress`、`done` |
| `GET /tasks/summary` | `summarize_tasks` | 无参数，返回任务总数和各状态数量 |
| `GET /tasks/{task_id}` | `get_task` | 必填路径参数 `task_id`，例如 `TASK-102` |

示例数据和 API 定义在 [api.py](api.py)。OpenAPI 转换和额外的 Resource、Prompt 定义在 [server.py](server.py)。工具发现与调用见 [client.py](client.py)。

## 查看 OpenAPI 文档

可单独启动示例 API，然后访问 `/openapi.json` 查看规范，访问 `/docs` 查看交互式接口文档。

```bash
uv run --locked --group mcp uvicorn fastmcp_demo.api:app --host 127.0.0.1 --port 8022
```

接入已有接口时，把 OpenAPI JSON 或 YAML 解析为字典传给 `openapi_spec`，并在 HTTP 客户端上配置 API 的 `base_url` 及所需认证。Markdown 或网页里的接口说明可以先整理成 OpenAPI 规范。

## 用 OpenAI SDK 调用 Responses 接口

上面的 FastAPI 服务也提供 `POST /v1/responses`。服务端从本目录 `.env` 读取 `AGENT_USERS_LLM_BASE_URL`、`AGENT_USERS_LLM_API_KEY` 和 `AGENT_USERS_LLM_MODEL`，进程中的同名变量优先。

启动 FastAPI 服务后，用 OpenAI SDK 连接本地地址。示例中的 `local-demo` 用于初始化本地客户端，上游凭据由服务端读取。

```python
from openai import OpenAI

with OpenAI(base_url="http://127.0.0.1:8022/v1", api_key="local-demo") as client:
    response = client.responses.create(
        input="查询正在进行的迭代任务，并告诉我任务编号。"
    )
    print(response.output_text)
```

此入口支持非流式请求。`input` 可以是字符串或 Responses 消息列表，可选传入 `model`、`instructions` 和 `max_output_tokens`。省略 `model` 时使用 `.env` 中配置的模型。

每次请求先通过 MCP 发现任务工具，把它们作为函数工具传给 `AsyncOpenAI.responses.create()`。收到 `function_call` 后，服务端在本地调用 FastMCP，并按 `call_id` 把结果回传给模型，最后返回 SDK 可解析的 Response 对象。`RouteMap` 将 `/v1/responses` 排除在 MCP 工具映射之外。 [模型和工具调用代码](responses.py) · [工具路由映射](server.py)

## 通过 Streamable HTTP 连接

在一个终端启动本地服务。

```bash
uv run --locked --group mcp uvicorn fastmcp_demo.server:mcp.http_app --factory --host 127.0.0.1 --port 8020
```

在另一个终端连接同一个服务。

```bash
uv run --locked --group mcp python -m fastmcp_demo.client --url http://127.0.0.1:8020/mcp
```
