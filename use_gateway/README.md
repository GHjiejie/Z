# LiteLLM AI Gateway demo

这个示例启动一个 OpenAI-compatible 的 LiteLLM Proxy，把请求路由到真实的 OpenAI-compatible 模型服务。它复用仓库其他 demo 使用的 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `MODEL` 配置；后续可以只调整 `config.yaml` 来增加模型、fallback、限流和观测能力，客户端代码不需要变化。

## 运行

项目使用 `uv` 管理依赖。需要 Python 3.13+，并准备一个真实模型服务的 API key：

```bash
cd use_gateway
uv sync

# 仓库根目录已有 .env 时可以跳过下面两行；也可以创建独立配置
# cp .env.example .env
# 编辑 .env，至少填写 OPENAI_API_KEY、OPENAI_BASE_URL 和 MODEL
```

在第一个终端启动 Gateway：

```bash
# 使用仓库根目录 .env
LITELLM_MASTER_KEY=sk-demo-master-key uv run --env-file ../.env litellm --config config.yaml --port 4000

# 如果使用 use_gateway/.env，则改为：
# uv run --env-file .env litellm --config config.yaml --port 4000
```

在第二个终端调用 Gateway：

```bash
uv run python client.py
```

也可以直接用 curl 验证 OpenAI 兼容接口：

```bash
curl http://localhost:4000/v1/chat/completions \\
  -H 'Content-Type: application/json' \\
  -H 'Authorization: Bearer sk-demo-master-key' \\
  -d '{"model":"gateway-model","messages":[{"role":"user","content":"你好"}]}'
```

## 配置说明

- `model_name: gateway-model` 是对客户端暴露的稳定逻辑模型名。
- `litellm_params.model` 使用 `openai` provider；真实模型名从 `MODEL` 传给上游服务。
- `api_base` 从 `OPENAI_BASE_URL` 读取，支持 OpenAI 官方地址和第三方 OpenAI-compatible 服务。
- 生产环境请通过环境变量或密钥管理系统注入 key，不要把 `.env` 提交到 Git；`.env.example` 只包含占位值。
- 可通过 `LITELLM_BASE_URL`、`LITELLM_MODEL` 覆盖客户端默认地址和 Gateway 暴露的模型别名。

## 使用真实模型

仓库根目录已有 `.env` 时，按上面的启动命令即可直接使用其中的真实配置。当前仓库配置的模型是 `MODEL` 指定的模型，请求会发送到 `OPENAI_BASE_URL` 指定的 endpoint。

如果在 `use_gateway/.env` 单独配置，请填写：

```dotenv
OPENAI_API_KEY=你的真实模型服务密钥
OPENAI_BASE_URL=https://你的兼容服务/v1
MODEL=你的模型名称
LITELLM_MASTER_KEY=sk-demo-master-key
```
