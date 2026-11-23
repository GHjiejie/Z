# FastMCP 最小示例

FastAPI 发布 `GET /add` 和 `/openapi.json`。FastMCP 读取规范并把 `add` 暴露为 MCP 工具。客户端打印工具的描述和 Schema，再通过 MCP 调用 `add(2, 3)`，得到 `{'sum': 5}`。

在仓库根目录启动 FastAPI 和 MCP。FastAPI 监听 `8032`，MCP 监听 `8030`。按 `Ctrl+C` 停止这两个服务。

```bash
make -C fastmcp_minimal_demo start
```

另开一个终端查看并调用工具。

```bash
make -C fastmcp_minimal_demo client
```

单独调试时，在两个终端依次运行 `make -C fastmcp_minimal_demo api` 和 `make -C fastmcp_minimal_demo mcp`。

用模型选择工具时，可以让现有 Agent 连接这个 MCP 服务。模型配置沿用 `fastmcp_demo/.env`。

```bash
uv run --locked --group mcp python -m fastmcp_demo.client \
  --url http://127.0.0.1:8030/mcp '请调用 add 工具计算 2 和 3 的和。'
```

## 带访问权限的 MCP

鉴权入口监听 `http://127.0.0.1:8031/mcp`。在 `fastmcp_minimal_demo/.env` 中设置 `FASTMCP_DEMO_MCP_TOKEN` 为随机令牌，或通过同名环境变量设置。进程环境变量优先。

FastAPI 在 `8032` 运行后，从仓库根目录另开一个终端启动鉴权服务。

```bash
make -C fastmcp_minimal_demo mcp-auth
```

Agent 客户端选 Streamable HTTP，URL 填 `http://127.0.0.1:8031/mcp`。使用键值对格式的请求头输入框时，填入以下内容，并替换 `<token>`。

```text
Authorization=Bearer <token>
```

## 工具级授权与人工确认

`8037/mcp` 连接 `8032` 上的 FastAPI。`FASTMCP_DEMO_MCP_TOKEN` 可以调用已有的计算工具。`FASTMCP_DEMO_DELETE_TOKEN` 具有 `records:delete` scope，可以发起以下两种删除。

| MCP 工具 | FastAPI 接口 | 确认目标 |
| --- | --- | --- |
| `delete_record` | `DELETE /records/{record_id}` | 指定记录 |
| `delete_record_user` | `DELETE /records_user?user_id=...` | 用户 ID |

在 `.env` 或环境变量中为两个 MCP token 和 `FASTMCP_DEMO_BACKEND_DELETE_TOKEN` 配置各自的随机值。后端凭据供 FastAPI 和 `8037` 的服务间调用使用。先启动 FastAPI，再启动 MCP。

```bash
# 终端 1
make -C fastmcp_minimal_demo api
# 终端 2
make -C fastmcp_minimal_demo mcp-hitl
```

另开终端查看工具，或使用实际的记录 ID、用户 ID 发起删除。终端会展示目标，输入 `yes` 确认，回车取消。

```bash
uv run --locked --group mcp python -m fastmcp_minimal_demo.hitl_client
uv run --locked --group mcp python -m fastmcp_minimal_demo.hitl_client RECORD_ID
uv run --locked --group mcp python -m fastmcp_minimal_demo.hitl_client --user-id USER_ID
```

两个删除工具用 `auth=require_scopes("records:delete")` 校验调用权限，再以 MCP `2026-07-28` 的 `InputRequiredResult` 请求确认。确认绑定到调用者、工具和参数，10 分钟内可使用一次。服务端收到明确确认后，携带内部凭据向 FastAPI 发出 DELETE 并返回处理器的响应。

当前 `api.py` 的两个删除处理器返回固定的 `deleted` 响应。实际数据变更取决于处理器接入的业务逻辑。

HITL 依赖受信客户端向真实用户展示确认，示例使用终端。跨会话、可审计的业务审批可结合项目已有的 [Agent 交互工具流程](../agent_permissions/docs/agent-hitl.md)。
