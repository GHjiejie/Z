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
