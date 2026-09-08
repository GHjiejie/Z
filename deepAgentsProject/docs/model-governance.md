# 模型注册、发布绑定与停用

Schema 13 将模型配置从进程默认网关解耦。普通和 Coding Run 按各自发布计划解析模型；意图分类及知识 Embedding 仍使用独立服务配置，均纳入统一计量。生产 Run 不接受未绑定的旧目录模型。

## 运维配置

运维通过 `DEEPAGENT_MODEL_PROFILES_FILE` 挂载 JSON 数组。项目管理员只能选择当前租户/项目获批的 profile，不能提交任意端点、凭据值或文件路径。配置不包含密钥值；生产不得允许非可信用户写入该文件。

以下仅为格式示例；端点、模型名、价格和所属项目必须改为组织批准的实际配置，不能将示例费率用于真实结算：

```json
[
  {
    "id": "primary-v1",
    "name": "Approved primary model",
    "tenant_id": "tenant_example",
    "project_id": "project_example",
    "model": "approved-model-version",
    "base_url": "https://model.example.com/v1",
    "credential_env": "DEEPAGENT_MODEL_KEY_PRIMARY",
    "api_style": "chat_completions",
    "max_completion_tokens": 4096,
    "context_window_tokens": 131072,
    "capabilities": ["streaming", "tool_calling"],
    "input_per_million": "1",
    "output_per_million": "2"
  }
]
```

设置 `DEEPAGENT_MODEL_KEY_PRIMARY_FILE=/run/secrets/model-primary-key`，密钥文件沿用生产权限检查（仅所有者可访问）；同时将端点来源加入 `DEEPAGENT_MODEL_ALLOWED_ORIGINS`。生产只支持 HTTPS，禁止 URL 内嵌凭据、查询串、重定向和非批准来源。

profile 支持 Chat Completions、Responses 和 Anthropic Messages 协议，以及对应的认证样式、超时、输出上限和推理参数。能力声明由运维负责真实性，仍必须做实际提供方验收。

## 注册与发布

1. `GET /api/v1/model-profiles` 查询当前项目获批配置。
2. `POST /api/v1/model-deployments`，提交 `profile_id`、可选 `name` 和 `reason`，创建新的模型目录版本。每次注册使用新 ID，不覆盖原版本。
3. 将返回 ID 填入 Agent 草稿的 `model_deployment_id`，按正常流程发布 Revision、评测和部署。
4. 执行计划保存 profile ID、配置摘要、提供方/完整端点/模型标识和价格快照，不保存密钥值或凭据文件路径。
5. 新 Run 准入与 Worker 执行分别核验配置摘要、目录状态和价格。普通执行器与原生 Coding 使用相同绑定，不改变进程默认网关。

管理端点要求 `model.manage`，默认只有 owner/admin/tenant_admin 和平台超级管理员具备。注册与停用写入治理审计。

## 变更、轮换、停用

- 修改模型、端点、价格、调用参数或凭据引用：保留旧 profile，增加新 ID，注册新模型，重新发布/评测 Agent。原位修改 profile 会导致旧计划摘要不匹配并拒绝新执行。
- API 和 Worker 必须挂载同一套批准版本；不同节点缺失 profile 或摘要不一致时失败关闭，不能回退到默认模型。
- 只轮换同一引用的密钥值：通过密钥管理系统更新文件，滚动重启 API/Worker。原生模型客户端可能缓存密钥，不承诺热轮换。
- `PUT /api/v1/model-deployments/{id}/status`，携带 `version`、`enabled`、`reason`。竞争更新返回 409。停用阻止新 Run，运行中账号/权限监控也会发现停用；不保证撤回已被第三方接受的请求或费用。
- 模型不可硬删除，避免破坏历史 Run 和审计引用。重新启用必须携带最新版本与原因。

## 既有数据

迁移只增加绑定字段，不替用户选择端点、生成凭据或改写旧计划。生产旧计划必须注册模型后重新发布。开发旧计划仅在模型名称匹配进程配置时兼容；测试显式注入的网关可使用未绑定计划，但该例外在生产被拒绝。

价格来源分为两类：Run 对话模型锁定 profile 的发布价格；分类/Embedding 使用项目的版本化价格策略。辅助调用配置及对账见 [model-budget.md](model-budget.md)。注册和计费管理界面尚未完成，目前通过 API 管理。

## 验证边界

`tests/test_model_bindings.py` 在 SQLite/PostgreSQL 下验证同名模型不同路径与密钥的实际 HTTP 请求分流、各自费用、跨项目拒绝、配置漂移、价格篡改、停用与版本竞争、原生 Coding 模型实例绑定和资源关闭。提供方使用受控 HTTP transport；这不是对真实模型、真实费用和生产部署的验收。
