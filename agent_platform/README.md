# Agent Platform

基于 **Python / FastAPI + React / TypeScript + LiteLLM Proxy** 的可自部署 Agent 平台。已实现用户管理、Agent 对话与工具运行、模型目录、调用统计、额度计费、分布式限流和人工对账的第一版业务闭环。

Python 使用仓库根目录的 `pyproject.toml`、`uv.lock` 和 `.venv`；没有创建第二套 Python 环境。正式部署组合使用 PostgreSQL、Redis、独立 API/Worker 和 LiteLLM；本地也可以使用 SQLite 文件数据库运行。

## 文档

- [当前实现架构图](docs/architecture-diagrams.md)：系统组件、本地/容器部署拓扑、Agent 调用时序与计费状态。
- [架构设计方案](docs/architecture.md)：长期架构与责任边界。
- [第一版实现契约](docs/implementation-contract.md)：API、运行协议与模块接口。
- [实际完成范围](docs/implementation-status.md)：当前功能、设计差异和验证记录。
- [运行与部署](docs/deployment.md)：本地启动、Compose、网关受限密钥、迁移与测试。
- [前端说明](apps/web/README.md)：开发服务器、页面和前端验证。

## 已实现

| 功能 | 当前实现 |
| --- | --- |
| 用户管理 | 管理员创建用户、角色、启停、自助改密、会话撤销、登录限流、CSRF、租户隔离 |
| Agent | 配置草稿、不可变发布版本、工具白名单、多轮会话、每次运行的配置与价格快照 |
| 执行 | LangGraph v3、计算器/当前时间工具、独立 Worker、持久 Run/SSE、重连重放、取消、步数/时间/费用上限 |
| 模型 | LiteLLM 别名、启停、上下文与输出限制、输入/输出定价、服务器端密钥 |
| 统计 | 调用明细、Token、费用、每日趋势、模型分布、成功率、CSV 导出 |
| 计费 | USD 钱包、人工入账、并发额度预占、精确小数账本、幂等结算、价格快照、待确认费用冻结 |
| 对账 | 管理员根据证据确认 Token 或明确核销、审计记录、异常超额冻结与人工解封 |
| 限流 | 租户/用户/模型 RPM、TPM、并发；累计预算；Redis 原子控制和数据库一致性检查 |
| 部署 | 根依赖锁、前端生产构建、Docker Compose、数据库迁移、独立数据库角色、受限网关 key 引导 |

没有配置模型服务时，平台显示未连接并拒绝模型请求，不会自动生成模拟答案。新组织钱包余额为零，管理员显式入账后才能发起付费调用。

## 一键启动

先安装 `uv`、Node.js / npm、`make`，并启动 Docker，然后在本目录执行：

```bash
make start
```

直接执行 `make` 或 `make run` 也会启动。从仓库根目录可以使用 `make -C agent_platform start`。

启动命令会同步依赖，启动独立的 LiteLLM Proxy、PostgreSQL 和 Redis，生成或验证受限模型调用密钥，再构建 React、迁移平台数据库、初始化管理员并启动 API / Worker。平台继续使用原有本地数据库，LiteLLM 使用独立数据库；已有配置、账号和数据不会被覆盖。缺少 `.env` 时自动创建并生成随机平台管理员密码。

默认启动方式中，根目录 `.env` 的 `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL` 用于配置 LiteLLM 的上游模型，平台通过本机 LiteLLM 和独立受限密钥调用。上游密钥不会复制到平台配置或打印到日志，也不会传入平台 API / Worker。已有显式 `UPSTREAM_API_KEY` 时使用 `UPSTREAM_*` 上游配置。

LiteLLM 官方管理台默认位于 [http://127.0.0.1:4000/ui](http://127.0.0.1:4000/ui)。平台管理员可从侧栏的「LiteLLM 网关」打开它。默认用户名为 `admin`，独立登录密码保存在 `.data/local/gateway/.env.control` 的 `UI_PASSWORD`，不会打印到日志。该文件也保存本地网关管理凭据，权限为 `0600`，不提交 Git；`.env.gateway` 仅保存平台使用的受限调用密钥。两个控制台使用各自的登录会话。

`MODEL`（或显式 `PLATFORM_DEFAULT_MODEL`）会作为模型目录的默认模型，新建 Agent 时优先选择已启用的默认模型。已有模型不会被重启覆盖。首次自动添加时，需要在本目录 `.env` 同时设置 `PLATFORM_DEFAULT_MODEL_INPUT_PRICE` 与 `PLATFORM_DEFAULT_MODEL_OUTPUT_PRICE`（平台报价，USD / 百万 Token）；没有填写时，页面提示添加并预填模型名称，不会猜测供应商价格。若本地 IPv6 连接上游失败，可设置 `PLATFORM_GATEWAY_LOCAL_ADDRESS=0.0.0.0` 让模型调用使用 IPv4，仍然校验证书。

新建 Agent 的 Temperature 默认值为 `1`。当前 `k3` 上游仅接受该值；已有 Agent 的参数保持原样，需要按模型要求自行调整。

打开 [控制台](http://127.0.0.1:8000)。默认邮箱为 `admin@example.com`，初始密码位于本目录 `.env` 的 `PLATFORM_ADMIN_PASSWORD`；显式设置的环境变量优先于文件。首次初始化后更改引导密码不会重置已有账号。

```bash
make start PORT=8010   # 8000 被占用时使用其他端口
make start PORT=8010 LITELLM_PORT=4001  # 自定义 LiteLLM 端口
make status           # 在另一终端查看本脚本管理的服务
make stop             # 停止服务，保留配置和数据
make help             # 查看所有命令
```

启动在前台运行，按 `Ctrl+C` 也会停止平台进程和该实例的三个网关容器，保留数据库卷。端口冲突会明确报错。每个 `STATE_DIR` 使用独立 Compose 项目，`stop` 不影响其他项目或完整 Docker 部署。重启复用管理密码和有效运行密钥，不会重复创建；运行密钥过期或权限不符时会提示显式处理。

已有外部网关时使用 `make start GATEWAY=external`，此模式不管理 Docker，按 `PLATFORM_LITELLM_URL` / `PLATFORM_LITELLM_KEY` 配置连接，空值时保留根 `OPENAI_*` 配置回退；管理页入口可通过 `PLATFORM_LITELLM_ADMIN_URL` 单独指定。现有分步命令和完整 Docker 部署命令继续保留。

从空平台完成首次运行：

1. 在模型管理中添加与 LiteLLM `model_name` 一致的别名，填写价格（USD / 百万 Token）。
2. 在费用中心人工入账，并填写可追溯的原因。
3. 创建 Agent，选择模型，按需启用计算器和当前时间工具，发布版本。
4. 在 Playground 新建会话并输入任务。
5. 在运行记录、用量统计和费用中心核对执行、Token 与费用。

本地内嵌 Worker 默认开启。独立部署、上游配置和网关初始化步骤见[部署文档](docs/deployment.md)。每次运行默认费用上限 1 USD，可通过 `PLATFORM_MAX_RUN_COST` 修改；默认运行与排队超时均为 300 秒。

## 验证

```bash
uv run python -m unittest discover -s agent_platform/tests -v
uv run ruff check agent_platform chat_models/factory.py
uv run ruff format --check agent_platform chat_models/factory.py
npm --prefix agent_platform/apps/web run build
```

可选真实容器协议与全链路验收（需要 Docker）：

```bash
uv run python -m agent_platform.deploy.smoke_test
```

容器验收使用真实 PostgreSQL / Redis / LiteLLM / API / Worker，加一个独立的本地模拟供应商，不读取真实上游密钥，也不会产生模型费用。模拟供应商只存在于测试流程。

## 当前边界

这是已可运行和测试的第一版，不代表完整生产验收。当前平台按 **LiteLLM 网关计量** 结算；LiteLLM 可能在供应商缺失 usage 时估算 Token，平台不能把该结果等同供应商账单。外部真实模型的兼容性、供应商账单自动对账、在线支付、SSO、完整项目层级和数据库 RLS 尚未实现。

限流预算目前是累计预算，界面不称其为自然月预算；缓存/推理 Token 包含在网关总输入/输出中，第一版按两个总量费率计费，不额外叠加这些分项。Worker 失联时停止运行、保留待核实费用，不自动重放可能已发送的模型请求。详细差异见[实际完成范围](docs/implementation-status.md)。
