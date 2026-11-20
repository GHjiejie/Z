# FastAPI、OpenAPI 与 MCP 示例

FastAPI 发布任务接口和 `/openapi.json`。MCP 服务创建 `httpx.AsyncClient`，读取并解析 OpenAPI 规格，再通过 `FastMCP.from_openapi()` 生成工具。同一个 HTTP 客户端在运行时把工具调用转发给 FastAPI。

Agent 连接 `/mcp`，通过 `tools/list` 获取工具定义。模型请求工具时，Agent 发送 `tools/call`，再按模型提供的 `call_id` 把工具结果交回模型，直到得到最终回答。

## 运行

需要 Python 3.13 和 `uv`。Agent 从 `fastmcp_demo/.env` 读取 `AGENT_USERS_LLM_BASE_URL`、`AGENT_USERS_LLM_API_KEY` 和 `AGENT_USERS_LLM_MODEL`。进程中的同名变量优先。配置的供应商需要支持 OpenAI 兼容的 Responses API 和函数工具调用。

在仓库根目录依次启动 FastAPI 和 MCP 服务，然后选择 SDK 或命令行发起请求。

1. 启动 FastAPI。

   ```bash
   uv run --locked --group mcp uvicorn fastmcp_demo.api:app --host 127.0.0.1 --port 8022
   ```

   OpenAPI JSON 在 `http://127.0.0.1:8022/openapi.json`，交互式文档在 `http://127.0.0.1:8022/docs`。

2. 启动 MCP 服务。

   ```bash
   uv run --locked --group mcp python -m fastmcp_demo.server
   ```

   服务通过 Streamable HTTP 监听 `http://127.0.0.1:8020/mcp`。可用 `--api-url` 指定 FastAPI 地址。

### 用 OpenAI SDK 调用 Agent

FastAPI 提供 `POST /v1/responses`，返回 OpenAI SDK 的 `Response` 对象。SDK 的 `base_url` 使用 FastAPI 地址加 `/v1`。`local-demo` 是本地 SDK 初始化占位值，模型供应商的 API Key 由服务端读取。

```python
from openai import OpenAI

with OpenAI(base_url="http://127.0.0.1:8022/v1", api_key="local-demo") as client:
    response = client.responses.create(
        input="查询正在进行的任务，再读取对应的任务详情。请给出任务编号和具体说明。"
    )
    print(response.output_text)
```

接口支持非流式请求。`input` 接受字符串或 Responses 消息列表，可选传入 `model`、`instructions` 和 `max_output_tokens`。省略 `model` 时使用 `.env` 中配置的模型。FastAPI 默认连接 `http://127.0.0.1:8020/mcp`，可通过进程环境变量 `FASTMCP_DEMO_MCP_URL` 修改。

`/v1/responses` 会出现在 `/openapi.json` 中。FastMCP 的 `RouteMap` 将它排除在工具列表之外，避免 Agent 递归调用自己的入口。

### 用命令行调用 Agent

```bash
uv run --locked --group mcp python -m fastmcp_demo.client \
  "查询正在进行的任务，再读取对应的任务详情。请给出任务编号和具体说明。"
```

工具发现和调用状态输出到 stderr，最终回答输出到 stdout。可用 `--url` 覆盖 MCP 地址。

## MCP 工具

| OpenAPI 操作 | MCP 工具 | 输入 |
| --- | --- | --- |
| `GET /tasks` | `list_tasks` | 可选查询参数 `status`，支持 `todo`、`in_progress`、`done` |
| `GET /tasks/summary` | `summarize_tasks` | 无参数 |
| `GET /tasks/{task_id}` | `get_task` | 必填路径参数 `task_id`，如 `TASK-102` |
