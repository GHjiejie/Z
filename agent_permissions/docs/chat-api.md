# OpenAI 兼容的 Agent Chat 接口

服务通过 `POST /v1/chat/completions` 对外提供 Agent 对话，可直接使用 Python OpenAI SDK 的 `client.chat.completions.create()`。Agent 自动发现当前用户已允许且实时 RBAC 允许的业务工具，完成账号查询、用户管理和角色权限操作。

首次使用或从旧版升级后，先在用户管理页的“Agent 工具授权”中为目标账号开启所需工具，具体规则见[用户级授权说明](user-agent-tool-grants.md)。角色变化保留设置；没有可用业务工具时仍可普通聊天。每次实际调用重新检查授权，旧对话缓存的工具不能绕过撤权。

对外协议为 Chat Completions；内部仍使用 [本项目独立模型客户端](model.md)和供应商的 Responses API。客户端提供业务会话令牌，供应商密钥保存在服务端 `agent_permissions/.env`。

Console 通过 [AG-UI 1.0 接口](ag-ui.md)接入同一执行引擎：完成[前端构建和服务启动](running.md#3-启动-http-服务)后，登录并打开 `/console/#chat` 即可使用 SSE 聊天，实时查看步骤、模型提供的思考摘要与工具执行结果。支持多会话、停止生成及草稿恢复，历史按账号保存在当前浏览器；交互和存储边界见 [Console 使用说明](../console/README.md#使用会话)。OpenAI SDK 继续使用本页的标准 Chat Completions 接口。

普通和 SSE 对话均可通过 [LangSmith](langsmith.md) 查看 Agent、模型及业务工具调用链。每条根调用链携带 `request_id`、持久化任务的 `run_id` 与本次可用工具数量，可与响应中的 `X-Request-ID` 对应。

## 1. 服务入口与认证

| 接口 | 用途 |
| --- | --- |
| `POST /api/v1/auth/login` | 使用业务账号登录，获得 `session_token` |
| `GET /v1/models` | 获取本服务配置的模型名称 |
| `POST /v1/chat/completions` | 发起普通或 SSE 流式对话 |

OpenAI SDK 配置为 `base_url="http://服务地址:8000/v1"`，`api_key` 填写登录接口返回的 `session_token`。模型供应商密钥由服务端单独使用。会话过期、退出、账号禁用或删除后，Chat 接口返回标准格式的 `401` 错误。

配置好本项目的模型后，可在仓库根目录启动：

```bash
uv run python -m agent_permissions.tools build
uv run uvicorn agent_permissions.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers --env-file agent_permissions/deploy/.env
```

`0.0.0.0` 允许通过本机网络地址访问。只供本机使用时改为 `127.0.0.1`。公网部署应由 HTTPS 反向代理接入，按实际网络配置受信代理；会话及模型配置沿用既有项目设置。

## 2. 直接使用 OpenAI SDK

以下示例完成业务登录、发现模型并发起普通对话。示例只查询个人信息：

```python
import getpass

import httpx
from openai import OpenAI

origin = "http://127.0.0.1:8000"
login = httpx.post(
    f"{origin}/api/v1/auth/login",
    json={"username": "admin", "password": getpass.getpass("业务账号密码：")},
    trust_env=False,
)
login.raise_for_status()
session_token = login.json()["data"]["session_token"]

with OpenAI(
    base_url=f"{origin}/v1",
    api_key=session_token,
    max_retries=0,
    timeout=150,
) as client:
    model = client.models.list().data[0].id
    result = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "请查询并告诉我当前登录账号的信息"}],
        max_completion_tokens=2048,
    )
    print(result.choices[0].message.content)
```

`max_retries=0` 避免客户端自动重放可能包含写操作的完整 Agent 任务。请求中断或超时后，应先查询结果再决定后续操作。

普通响应包含标准的 `id`、`object="chat.completion"`、`created`、`model`、`choices` 和有上游依据时的 `usage`。业务工具调用由服务端完成。需要补参或确认时返回待处理说明与 `run_...` 标识；可在同账号 Console“会话记录 → 打开已有任务”继续，或接入[交互运行 API](agent-hitl.md#运行-api)。写操作在确认前不会执行，不能通过标准 SDK 绕过确认。

## 3. 流式对话

同一个 SDK 客户端可使用：

```python
stream = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "请查询当前登录账号的信息"}],
    stream=True,
    stream_options={"include_usage": True},
)
with stream:
    for chunk in stream:
        if chunk.choices:
            print(chunk.choices[0].delta.content or "", end="", flush=True)
        elif chunk.usage:
            print("\n总用量：", chunk.usage.total_tokens)
```

流使用 `text/event-stream`：首块声明助手角色，后续块提供公开文本增量，结束块提供 `finish_reason`，可选用量块的 `choices` 为空，最后发送 `data: [DONE]`。内部工具调用和推理内容保持在服务端；公开文本可包含执行期间的助手说明。

Agent 流通过 LangGraph `astream_events(version="v3")` 消费。客户端断开时关闭图运行与 HTTP 工具客户端；已经进入业务事务的操作仍需通过查询确认结果。

如果流开始后发生错误，服务发送标准 `error` 数据事件并终止，不发送成功结束标记。OpenAI SDK 会将其报告为 `APIError`。

## 4. 异步 SDK

```python
from openai import AsyncOpenAI


async def chat(session_token: str):
    async with AsyncOpenAI(
        base_url="http://127.0.0.1:8000/v1",
        api_key=session_token,
        max_retries=0,
        timeout=150,
    ) as client:
        model = (await client.models.list()).data[0].id
        return await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "查看我的信息"}],
        )
```

## 5. 支持的请求参数

| 参数 | 行为 |
| --- | --- |
| `model` | 必填，使用 `/v1/models` 返回的配置模型名称 |
| `messages` | 必填，支持 system、developer、user、assistant 的文本历史 |
| `messages[].content` | 字符串或 `type="text"` 的文本块数组 |
| `stream` | 默认 `false`，设为 `true` 使用 SSE |
| `stream_options.include_usage` | 流结束前返回有依据的累计模型用量 |
| `temperature`、`top_p` | 传给本次 Agent 的模型调用 |
| `max_tokens`、`max_completion_tokens` | 二选一，限制每次内部模型调用的最大输出 token |
| `n` | 当前支持 `1` |
| `tool_choice` | 默认 `auto`，自动使用业务工具；`none` 只进行模型对话 |

每个 Chat 请求创建独立的持久化运行，客户端通过 `messages` 维护多轮历史；等待确认的任务通过扩展接口恢复同一图。工具集合在每个请求开始时重新发现，执行时再检查当前会话与权限。服务不提供登录工具给已认证的 Agent；其余业务工具按用户权限选择。

`usage` 累计当前请求内所有模型调用，包括工具规划及最终回复。缺少上游用量时不虚构 token 数。单个任务最多执行 24 次模型规划，每个活跃执行段超时为 120 秒，等待用户填参不计入计算时间，单次模型调用超时为 60 秒。

模型客户端在首次访问模型目录或发起对话时加载，并复用至服务退出；修改模型配置后需重启服务。

本接口实现面向业务 Agent 的 Chat Completions 核心能力。客户端自定义 `tools`、`response_format`、多候选、图像、音频、tool 角色历史等未实现参数会明确返回 `400`。对外 `/v1/responses` 不在本次兼容范围内；上游模型继续使用 Responses API。

## 6. 错误与验证

错误格式为 `{"error":{"message":"...","type":"...","param":null,"code":"..."}}`，请求标识放在 `X-Request-ID`。普通业务 API 继续使用原有错误格式。

| HTTP 状态 | 情况 |
| --- | --- |
| 400 | 请求参数无效或参数未支持 |
| 401 | 缺少或失效的业务会话令牌 |
| 404 | 模型未配置为请求的名称 |
| 429 | 上游模型限流 |
| 502 | 上游不可用或 Agent 结果不完整 |
| 503 | 服务端模型配置不可用 |
| 504 | 任务超时，需先核对部分操作是否已执行 |

模型错误、密码和供应商响应详情不会直接进入错误返回。工具权限拒绝会作为工具结果交给 Agent，Agent 可据此解释当前用户无法完成的操作，不能扩大权限执行。

离线验证使用真实 OpenAI SDK、真实 LangGraph Agent 和临时 SQLite，模型由受控假实现替代：

```bash
uv run python -m unittest agent_permissions.test_chat_api agent_permissions.test_chat_model -v
```
