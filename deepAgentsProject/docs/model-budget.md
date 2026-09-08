# Run 模型预算与费用状态

平台 Schema 9/12 为 `metered_calls` 加入统一账本和完整调用归属，`usage_ledger` 是 Run 视图。普通 Agent、原生 Coding、意图分类、文档与查询 Embedding 均在调用前预占。RAG 在 Run 中调用的 Embedding 同时消耗该 Run 的费用和调用次数额度；审批恢复、Worker 恢复和重试不会清空原额度。

## 计量规则

- 发起模型调用前，持有当前 Run 租约，在事务内预占调用次数和费用；并发请求共享同一 Run 行锁。
- 费用以整数 micro-USD 计算（1 USD = 1,000,000 micro-USD）。Run 对话模型费率来自已发布计划；其提供方、完整端点和模型标识必须与绑定一致。辅助调用费率来自项目级版本化价格策略；缺少价格时拒绝付费请求。
- 预占输入额度按请求内容、工具描述及协议开销进行保守估计，输出额度采用模型的最大输出参数。该估计不是精确分词，也不能保证外部服务一定遵守其声明的计费上限。
- 收到提供方的真实用量后，将 `RESERVED` 更新为 `ACTUAL`；结算同一调用不会重复记账。
- 提供方实际用量超出 Run 预算时，先持久记录实际费用，再把 Run 标记为 `FAILED_BUDGET`，不能回滚已发生的费用。
- 请求失败、取消或缺少权威用量时标记 `UNCERTAIN` 并保留预占。旧 Worker 的迟到回调不能结算新 Attempt，也不能释放旧预占。
- API 的 `usage.unsettled_model_calls` 大于零时，`cost` 包含未结算的保守预占，不应作为最终账单；控制台会显示 pending charges。
- `MODEL_INPUT_PRICE_PER_MILLION` / `MODEL_OUTPUT_PRICE_PER_MILLION` 不再覆盖普通执行器的费用，避免两种执行器使用不同计费来源。

## 管理 API 与边界

- `GET /api/v1/billing/providers` 查看辅助调用当前使用的计价标识。`route` 包含完整 scheme、host 和 base path，旧的仅主机名价格不能沿用。
- `GET/PUT /api/v1/billing/prices` 查看/修改当前项目提供方价格，单位为 USD / 百万 tokens。修改必须携带 `version` 和至少 5 字的 `reason`，首次版本为 0，竞争更新返回 409；已预占调用保留原价格。价格在预占前发生变更时拒绝旧报价。
- `GET/PUT /api/v1/billing/quotas` 管理日/月费用、输入/输出 tokens、调用次数和并发额度。支持 tenant、project、user、model 范围，所有适用额度同时满足才准入。达到上限后拒绝新调用，返回 429，不会借降级绕过额度。
- `GET /api/v1/billing/calls?limit=50&cursor=...&status=UNCERTAIN` 按项目分页查看账本。游标不能跨身份、环境、项目或筛选条件复用；响应不包含所有权令牌摘要或请求正文。
- `POST /api/v1/billing/calls/{id}:reconcile` 人工核对未知账单，携带版本、原因、提供方凭据编号、实际 tokens 和 `actual_cost_micro_usd`。未到期的活动预占不能人工释放；已结算或重复/过期版本返回 409。账本、Run 视图、前后状态审计在同一事务内更新。

以上管理端点要求 `billing.manage`。项目 owner/admin 可管理本项目；tenant/model 全租户额度只能由租户管理员或平台超级管理员设置。普通用户无权查看他人账单或调整额度。当前组织成员模型尚未重构，跨项目用户额度将随组织治理继续完善。

生产必须配置租户级月费用和并发上限；不能通过禁用该策略或清空这两个上限移除保护。所有配额修改与预占都使用同一个租户数据库锁，因此不同 API/Worker 进程不能同时花掉同一份剩余额度。账期按 UTC 准入时间固定，晚到结算不转移到新账期。

未知调用保留费用/tokens 预占，并发占用保留到有界租约到期。租约到期只释放并发占位，不表示免费或账单已结清。实际花费超过预占会先记账再报告超额；额度不能保证第三方绝不超额计费。

意图分类无可计价配置时返回 503，不会悄悄降级执行。查询 Embedding 同理；文档摄取会进入 FAILED，管理员配置完成后显式重试。摄取保存真实发起人、角色快照及环境，每批向量请求重新核验账号与文档权限。

## 迁移

先在备份/维护窗口执行独立迁移到 Schema 13，再启动新 API/Worker。Schema 12 保留已有费用；能从原账本证明已结算的历史记录恢复 ACTUAL，无法确认的旧记录保留 LEGACY，必须人工对账。迁移不会将未知历史花费归零。

旧摄取任务没有完整身份快照，不冒充系统用户继续付费。它们会失败并要求有权限的用户重试，从重试请求记录新的可信身份。当前改造没有对用户运行中的数据库执行迁移或自动重试任务。

## 仍待完成

前端额度/账单管理台、供应商账单自动拉取与凭据验证、缓存/批处理/工具独立收费项、更精细价格维度与组织多项目成员关系尚待完善。手动填入凭据编号不是自动验证供应商账单；外部对账必须由管理员核验。模型注册和绑定契约见 [model-governance.md](model-governance.md)。

## 提供方用量依据

Embedding 从响应的 `usage.prompt_tokens` 读取用量，并核对 `total_tokens`；缺失、非法或不一致的统计保留为未知费用，而不是零用量。响应另有大小与总耗时上限，防止慢速流一直占用执行线程。[OpenAI Embeddings API](https://developers.openai.com/api/reference/resources/embeddings/methods/create)

流中断可能没有最后的 usage chunk，因此不能因为未收到统计就退回预算。[OpenAI 流式用量说明](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)

## 验证

`tests/test_budgets.py` 验证普通 Agent 零预算不调用模型、正常结算、失败调用保留费用及重试不重置额度。

`tests/test_coding_agent.py` 验证 Coding 模型的零预算和调用次数上限在提供方执行之前拦截。

`tests/test_runtime_concurrency.py` 在 SQLite 与独立 PostgreSQL Schema 中验证并发预占、跨 Attempt 恢复、旧回调拒绝和提供方超额结算。

`tests/test_metering.py` 覆盖分类/文档/查询额度拦截、发起人归属、缺少价格、未知用量、跨请求并发竞争、价格变更、人工对账与分页隔离；两种数据库均运行。
