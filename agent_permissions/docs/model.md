# 项目独立模型配置

`agent_permissions` 的模型客户端位于 [chat_model.py](../chat_model.py)，固定使用 Responses API。模型配置与凭据保存在本目录自己的 `.env`，使用独立的 `AGENT_USERS_LLM_` 变量。

对外使用 OpenAI SDK 调用时，参见 [OpenAI 兼容 Chat 接口](chat-api.md)。该入口默认运行能够调用业务工具的 Agent。

Chat 的 Agent、模型和工具调用可接入 [LangSmith 追踪](langsmith.md)，其独立凭据也在本模块 `.env` 配置。

配置样例见 [agent_permissions/.env.example](../.env.example)：

```dotenv
AGENT_USERS_LLM_BASE_URL=https://api.wylon.cn/v1
AGENT_USERS_LLM_API_KEY=replace_with_your_api_key
AGENT_USERS_LLM_MODEL=ZhipuAI/GLM-5.3
```

`agent_permissions/.env` 已被仓库 Git 规则忽略。本机密钥文件的权限设置为 `0600`。进程中同名的 `AGENT_USERS_LLM_` 变量优先于项目文件；根目录 `.env` 和通用的 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL` 不参与这个客户端的配置。读取配置不会把文件变量写入进程环境。

在仓库根目录运行时，使用：

```python
from agent_permissions.chat_model import create_chat_model

chat_model = create_chat_model()
response = chat_model.invoke("请只回复 OK。")
print(response.text)
```

工厂在实际构建 Agent 时才校验模型配置。用户管理 API 和工具目录可以在没有模型密钥时独立启动。默认参数为温度 `0`、流式响应、60 秒超时、SDK 自动重试次数 `0`，以及 `reasoning={"effort": "medium", "summary": "auto"}`。Chat 执行图可以对尚未输出的首轮模型超时重试一次，详见[请求参数与用量](chat-api.md)。

接入生成的 API 工具时，在 HTTP client 的生命周期内构建和调用 Agent：

```python
import httpx
from langchain.agents import create_agent

from agent_permissions.chat_model import create_chat_model
from api_tools.client import AgentToolClient


async def run_agent(session_token: str, message: str):
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        trust_env=False,
        timeout=30,
    ) as http:
        adapter = AgentToolClient(http, token_provider=lambda: session_token)
        tools = await adapter.langchain_tools()
        agent = create_agent(model=create_chat_model(), tools=tools)
        return await agent.ainvoke({"messages": [{"role": "user", "content": message}]})
```

供应商模型密钥与业务 API 的 `session_token` 是两个独立凭据：前者由本项目的模型工厂读取，后者由可信宿主注入工具客户端。工具执行仍使用业务 API 的当前会话权限。

配置隔离测试可通过 `uv run python -m unittest agent_permissions.test_chat_model -v` 运行，测试使用临时配置和假密钥，不调用外部模型。
