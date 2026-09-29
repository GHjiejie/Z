# Agent Platform

Python / FastAPI、React / TypeScript、LangGraph 与 LiteLLM 组成的多租户 Agent 平台。一个账号可以加入多个组织，组织成员权限与平台运营权限分开；Agent 角色与供应商模型解耦。

## 文档

- [多租户实现说明](docs/multi-tenant-implementation.md)：当前架构、权限、数据库、网关、账务和交付状态。
- [多租户运行手册](docs/multi-tenant-operations.md)：部署、密钥、切库、备份、关闭与恢复。
- [迭代设计方案](docs/multi-tenant-iteration-design.md)：设计依据和正式验收矩阵。
- [历史第一版记录](docs/implementation-status.md)：旧版本验证记录，不作为多租户上线依据。

当前功能已实现并在本地迁移运行。**PostgreSQL 隔离、真实网关控制操作、故障注入与容量验收尚未完成，当前不声明已达到 SaaS 正式上线条件。**

## 已实现功能

| 领域 | 当前能力 |
| --- | --- |
| 身份 | 全局账号、多组织成员、邀请、撤销、所有权转移、按标签页切换组织 |
| 权限 | owner、tenant_admin、member、finance_viewer；独立平台管理/财务/支持角色 |
| 内容 | 会话和运行正文本人私有；支持访问需 owner 限时审批并留审计 |
| Agent | 8 个内置助手、草稿和不可变发布、受控工具、多轮会话 |
| 模型 | 组织模型授权、默认及候选顺序、部署与显示别名分离、价格快照 |
| 网关 | 每组织加密凭据、代际轮换、Outbox、未知外部写入核查、独立同步进程 |
| 执行 | 租户轮转调度、持久任务和 SSE、取消、租约/fencing、有限批次恢复 |
| 账务 | 平台财务入账/补证/核销/解冻；精确钱包、预占、结算、未知费用冻结 |
| 容量 | 套餐硬上限、内部配额、成员/Agent/队列/运行/SSE/导出限制 |
| 数据 | 分页接口、私有异步导出、关闭组织、保留期、独立持久删除记录 |
| 部署 | PostgreSQL FORCE RLS、独立迁移/运行角色、四种独立进程、就绪检查 |

未配置可用模型、组织凭据或额度时会明确拒绝调用，不生成模拟答案。

## 本地启动

前置依赖：`uv`、Node.js/npm、`make`、Docker。Python 共用根目录的 `pyproject.toml`、`uv.lock` 和 `.venv`。

```bash
make -C agent_platform start PORT=8010
make -C agent_platform status
make -C agent_platform stop
```

启动器依次准备依赖和网关、构建前端、执行数据库迁移、显式初始化，然后启动 API、Worker、gateway-sync 和 maintenance。保持已有本地数据库及网关持久卷。启动在前台运行，Ctrl+C 或 `make stop` 停止本实例。

访问 [本地控制台](http://127.0.0.1:8010)。本地默认账号为 `admin@example.com`，密码在 `agent_platform/.env` 的 `PLATFORM_ADMIN_PASSWORD`。初始密码只用于开户，修改配置不会重置已存在账号。

首次配置时：

```dotenv
PLATFORM_OPERATOR_EMAILS=admin@example.com
```

显式初始化会为该邮箱引导平台管理和财务角色；普通组织管理员不会自动取得平台角色。已撤销的角色也不会被重启恢复。新增用户由平台开通全局账号，再由组织发出邀请；接受邀请需要登录相同邮箱。

`PLATFORM_SECRET_ENCRYPTION_KEY` 缺失时，启动器生成并持久化到被 Git 忽略的 `.env`，权限 `0600`。必须另行保管此密钥，不能随重启重新生成。

### 模型与 LiteLLM

默认 managed 启动方式使用根 `.env` 的 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和 `MODEL` 配置 LiteLLM 上游。平台运行进程不会收到上游密钥。已有 `UPSTREAM_API_KEY` 时使用显式 `UPSTREAM_*` 配置。

LiteLLM 管理页默认是 [本地 LiteLLM](http://127.0.0.1:4000/ui)，登录配置保存在 `.data/local/gateway/.env.control`。平台与 LiteLLM 使用不同登录会话；配置文件不提交 Git。只有 gateway-sync 收到控制面 master key。

首次历史单组织本地部署可采用旧受限 Key；**新增第二个组织之前，先在平台运营的网关面板将原组织纳管**，再分别为新组织授权模型和同步凭据。多租户运行没有全局 Key 回退。

使用外部 LiteLLM 时：

```bash
make -C agent_platform start PORT=8010 GATEWAY=external
```

显式填写 `PLATFORM_LITELLM_URL` 和受限 `PLATFORM_LITELLM_KEY`；组织网关同步另需配置 `PLATFORM_GATEWAY_CONTROL_URL/KEY`，只传入同步器。外部模式不把根 `OPENAI_API_KEY` 当作平台运行 Key。

## 开通组织并运行

1. 平台运营开通组织并指定 owner；会自动安装 8 个已发布内置 Agent，钱包初始为零。
2. 在 LiteLLM 配置上游模型，然后在平台登记对应部署路由。
3. 向组织授权模型，指定平台报价及上下文/输出限制。
4. 在组织网关面板发起同步，待状态变为就绪。
5. 平台财务显式入账；组织按需配置更严格的内部配额。
6. 切换进入该组织，在对话实验室运行 Agent。

内置角色包括写作润色、翻译与本地化、长文总结、会议纪要、任务规划、代码排错、数据解读、客服回复。角色定义只包含指令和示例，不包含模型名称、供应商或密钥。

模型解析顺序为本次指定 → Agent 偏好 → 组织默认/候选/其他可用模型。明确指定的模型不可用时失败，不偷偷替换。运行创建后冻结模型、价格和部署，调用过程中不自动换模型或重试产生新消费。

## 运维

- SaaS 必须使用 PostgreSQL、非 owner 运行角色、HTTPS secure Cookie 和持久加密密钥。
- 迁移命令与 `init` 使用迁移身份；服务启动只核对版本，不修改 schema。
- `GET /api/v1/health` 检查存活；`GET /api/v1/ready` 检查后台心跳及数据库/Redis条件。
- 未结费用必须有证据确认或明确核销，不能通过清空预占恢复服务。
- 关闭组织的删除记录放在独立持久卷，数据库恢复前必须重放；不随 PostgreSQL 回滚。

本地启动参数、离线 SQLite → PostgreSQL 工具和恢复步骤见[运行手册](docs/multi-tenant-operations.md)。本次仅完成静态检查、构建、现有本地库迁移和启动；未运行新增自动化或付费模型验收。
