# LangSmith 调用链追踪

Chat 接口已接入 LangSmith。每个 `POST /v1/chat/completions` 请求生成一条独立的 `agent_permissions.chat` 根调用链，记录 LangGraph 执行、内部模型调用和实际执行的业务工具。普通响应、SSE 和 `tool_choice="none"` 都使用同一入口。

实现位于 [tracing.py](../tracing.py)。通过独立的 `Client` 和请求范围的 `tracing_context` 配置追踪，不把配置写入全局进程环境；这种方式见 [LangSmith 程序化追踪配置](https://docs.langchain.com/langsmith/trace-without-env-vars)。Agent 内的模型与工具层级由 LangChain 回调关联。

## 配置

在本项目的 `agent_permissions/.env` 中配置，示例文件为 [`.env.example`](../.env.example)：

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=replace_with_your_langsmith_key
LANGSMITH_PROJECT=agent-permissions
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_WORKSPACE_ID=
LANGSMITH_HIDE_INPUTS=false
LANGSMITH_HIDE_OUTPUTS=false
```

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `LANGSMITH_TRACING` | `false` | 是否为本模块 Chat 开启追踪 |
| `LANGSMITH_API_KEY` | 空 | LangSmith 凭据，与模型供应商密钥、业务登录令牌分别配置 |
| `LANGSMITH_PROJECT` | `agent-permissions` | 追踪所属 Project |
| `LANGSMITH_ENDPOINT` | `https://api.smith.langchain.com` | LangSmith API 地址，可按账号区域或自托管服务调整 |
| `LANGSMITH_WORKSPACE_ID` | 空 | 组织或多个工作区关联的密钥可指定工作区 |
| `LANGSMITH_HIDE_INPUTS` | `false` | 完全隐藏追踪输入 |
| `LANGSMITH_HIDE_OUTPUTS` | `false` | 完全隐藏追踪输出 |

同名进程环境变量优先于本模块文件；运行时代码不读取仓库根目录 `.env`，也不使用旧的 `LANGCHAIN_*` 变量作为配置回退。配置在应用创建时读取，修改后需重启服务。模板默认关闭追踪，填好密钥后显式设为 `true`。

启动方式保持不变：

```bash
make -C agent_permissions run
```

## 查看调用链

1. 在 Console 的 `/console/#chat` 发送消息，或使用 OpenAI SDK 调用 Chat 接口。
2. 登录 [LangSmith](https://smith.langchain.com/)，打开配置的 Project。
3. 查看名为 `agent_permissions.chat` 的根调用链，展开模型节点和工具节点。
4. 使用 `metadata.request_id` 与接口响应的 `X-Request-ID` 对应，关联服务日志和业务审计。

每条根调用链的 metadata 包含 `request_id`、`model`、`stream`、`tool_count` 和 `tool_names`。工具数量来自本次按权限过滤、并排除登录工具后的最终列表。例如默认全权限管理员为 20 个。`tool_names` 表示本次可用工具，实际调用的工具由子调用链体现。

SSE 追踪上下文持续到生成结束或取消；结束时恢复上下文，避免并发请求互相关联。LangGraph 保持使用 `astream_events(version="v3")`。

## 正文与凭据处理

默认记录输入和输出，便于查看对话与业务工具结果。上传前处理结构化的密码、会话令牌、认证头等字段，包括工具消息中序列化为 JSON 字符串的内容；SDK 的 secret anonymizer 处理常见密钥格式和错误文本。该处理不会修改实际传给模型或业务接口的数据。

字段规则和格式规则不能识别所有自然语言中的秘密。需要完整隐藏正文时，将 `LANGSMITH_HIDE_INPUTS`、`LANGSMITH_HIDE_OUTPUTS` 设为 `true`；调用结构、工具名称、时序与关联 metadata 仍可用于排查。Client 的隐藏与脱敏能力见 [LangSmith 输入输出处理](https://docs.langchain.com/langsmith/mask-inputs-outputs)。

配置缺失或格式错误时关闭追踪并记录安全提示；客户端初始化或追踪上传异常不自动重试业务操作。SDK 在后台批量发送追踪，服务关闭时最多等待 3 秒清理客户端；网络异常或强制结束进程时可能丢失尚未上传的数据。

## 验证

```bash
uv run python -m unittest agent_permissions.test_tracing -v
make -C agent_permissions test-python
```

离线测试使用真实 LangSmith SDK、真实 Agent 和临时 SQLite，并通过内存 HTTP transport 接收上传数据。覆盖普通响应、SSE、取消、并发隔离、零工具请求、模型错误、配置隔离、正文隐藏与凭据脱敏。测试显式关闭真实外部追踪，避免因本机 `.env` 已启用 LangSmith 而上传测试数据。
