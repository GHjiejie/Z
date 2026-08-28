# LangChain `create_agent` 核心概念 Demo

当前 LangChain 官方文档将 `create_agent` 分为 5 个核心组件：

| 核心组件 | `create_agent` 参数 | Demo |
| --- | --- | --- |
| Model | `model` | `model_demo.py` |
| Tools | `tools` | `tools_demo.py` |
| System prompt | `system_prompt` | `system_prompt_demo.py` |
| Structured output | `response_format` | `structured_output_demo.py` |
| Agent state | `state_schema` | `agent_state_demo.py` |

所有 demo 都复用 `../chat_models/chat.py` 中的 `ChatOpenAI`，会调用 `.env`
配置的真实模型，不包含 fake model 或 mock。调用使用异步 `astream()`，不使用
等待完整回答的 `invoke()`。

流会被统一转换为前端友好的 SSE 事件：

| 事件 | 含义 |
| --- | --- |
| `token` | 模型生成的增量文本 |
| `tool_call` | 模型决定调用工具 |
| `tool_result` | 工具执行结果 |
| `structured_output` | 通过 schema 校验的结构化结果 |
| `done` | 本次 Agent 执行结束 |

在当前目录运行：

```bash
../.venv/bin/python concept.py
../.venv/bin/python main.py model
../.venv/bin/python main.py tools
../.venv/bin/python main.py system_prompt
../.venv/bin/python main.py structured_output
../.venv/bin/python main.py agent_state
```

也可以直接运行任一 `*_demo.py` 文件。

在 FastAPI 中可以复用 `concept.py` 的 `stream_agent_events()`；如果使用浏览器
`EventSource`，可通过 `to_sse()` 将每个事件编码为 `text/event-stream` 数据。

参考：[LangChain Agents 官方文档](https://docs.langchain.com/oss/python/langchain/agents)
