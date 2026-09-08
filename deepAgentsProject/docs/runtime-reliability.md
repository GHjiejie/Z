# 执行一致性与恢复边界

本页记录当前代码的保障及尚未完成的边界，不是生产验收证明。

## 已实现的不变量

- 同一 Thread 同时最多有一个非终态 Run；幂等重复请求核对原始内容和主体后返回原响应，相同 key 的不同内容返回 409。取消/结束后才能提交新任务。旧无请求摘要记录的处理见 [健康与容量](operational-readiness.md)。
- Run、Attempt、创建事件和 PostgreSQL 队列记录在同一事务内提交。意图路由的 Decision / Thread / Run / 事件 / 队列也在同一事务内提交。审批、补充输入和重试使用同一原则。
- 多 Worker 只能有一个原子领取当前 Attempt。写入时同时校验 Run 当前 Attempt、Worker、随机 Lease Token 及有效期；租约过期或被撤销后不能续租或提交结果。
- 运行时事件序号在 Run 行锁下分配。结果、Artifacts 和终态，以及人工审批中断点，在事务内一起提交。模型费用按调用单独预占/结算，避免后续失败回滚已发生的费用，见 [model-budget.md](model-budget.md)。
- LangGraph Checkpoint、Blob、Pending Writes 使用同一租约检查。PostgreSQL 使用平台事务的同一连接；SQLite 保留原 Checkpoint 文件并在本地平台写锁下调用同步 Saver。异步封装将完整操作移到工作线程，不在事件循环中跨 await 持有数据库锁。
- API/Worker 启动时不会绕过生产迁移开关修改 Checkpoint Schema；独立迁移命令同时更新平台和 Checkpoint Schema。
- 队列消息的 ACK、心跳和释放绑定具体投递轮次，旧消息不能确认新一轮投递。执行失败不再被登记为队列成功。
- 恢复器持续检查过期 Attempt 和未入队的 PENDING Run；并发恢复只产生一个新 Attempt。超过恢复上限进入失败状态，需要显式重试。
- 取消先撤销数据库执行租约；Coding 使用 Schema 18 独立收尾租约停止沙箱、原子提交产物和取消恢复标记，然后提交 CANCELLED。未完成的收尾由恢复器重试，不恢复旧执行权限；详见 [取消收尾契约](cancellation-finalization.md)。取消单个任务不会退出整个 Worker 消费循环。
- 知识库导入同样有租约保护、事务入队、定期心跳和持续恢复。解析/Embedding 后再次校验所有权，旧 Worker 不能更新新导入的阶段、索引或失败状态。
- 同一知识库的索引发布在知识库行锁下串行提交，保留递增的不可变版本，避免并发文档相互覆盖。

## Coding 图与文件的一致恢复点（Schema 8）

Coding 执行使用同步持久化边界：根图当前步骤完成后，先保存工作区归档，再发布包含图状态、文件快照、Plan Hash、源码基线、工作区代次和内容摘要的恢复点。单独写入 LangGraph Checkpoint 不代表恢复点已发布。下一步只有在发布成功后才执行；这是基于 [LangGraph 同步持久化语义](https://docs.langchain.com/oss/python/langgraph/persistence) 增加的文件一致性层。

- 恢复只读取最新已发布的配对记录，不拼接“最新图状态 + 最新 ChangeSet”。文件或图记录损坏、范围不符、扫描拒绝时停止恢复，不继续猜测。
- 每个 Attempt 使用独立图会话，只从配对记录导入根图、子图和当时已保存的 Pending Writes；旧会话之后产生的记录不会被导入。审批暂停时必须排空图流及保存任务，再封存完整子图状态。
- 在配对提交前中断，回退到前一个文件/图状态并重做尚未提交的步骤；配对提交后中断，不重复已提交的写文件步骤。费用不随文件回退而清零。
- 归档覆盖 `/workspace/repo`、`/artifacts`、`/tmp`，保留二进制内容和普通权限位。Git 基线由原始源码重新初始化，恢复包不能改写 `.git`；Skills 按固定版本重新生成。新沙箱先清除源文件再恢复，因此已删除文件不会重新出现。
- 文件内容可以回退，但工作区代次仍严格递增；恢复事件分别记录源代次和新代次。审批通过精确恢复点验证原文件状态，旧 ChangeSet 不能因版本号回退后再次相同而误被认可。
- 恢复点也绑定审批票据。上一次审批不能授权后来刚捕获、尚未登记给用户的新中断。新 Run 在首次检查点前中断，仍须处理新输入，不能误用前一个 Run 的完成状态。
- 模型已经回答、但 Run 尚未提交完成时中断，可以从图状态恢复回答，不虚构新模型调用或重复计费。Git `-z` 输出的 NUL 只在文本日志中转义，供解析器使用的原始结果保持不变。
- Docker Provider 在冻结容器后拒绝仍有后台进程的恢复点；不承诺恢复进程内存或长期后台任务。该守卫和新归档格式已做契约测试，真实 Docker 链路仍待验收。

归档上限为 100 MiB，图封存数据上限为 32 MiB、2,000 个 Checkpoint（含继承历史）；超过上限会明确失败，不丢弃状态后继续。当前按恢复点保存有界图历史，后续仍需历史压缩/回收及长期大工作区容量验收。旧版仅有 ChangeSet 而没有配对记录的工作区不会被隐式认定为可安全接续，需要显式迁移或在保留旧产物后新建工作区。

PostgreSQL 保存图会话及恢复点记录；生产文件归档必须使用固定 Object Version 的共享存储，读取时校验摘要与租户范围并重新扫描。文件上传成功但配对记录尚未发布时中断，可能留下未引用对象；对象回收属于后续数据生命周期治理，不能提前删除仍被恢复点引用的版本。

## 参数

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| DEEPAGENT_RUN_LEASE_SECONDS | 30 | Run 租约秒数，至少 3 秒 |
| DEEPAGENT_RUN_RECOVERY_LIMIT | 3 | 单 Run 自动孤儿恢复次数上限 |
| DEEPAGENT_RECONCILE_SECONDS | 2 | Run 恢复扫描间隔 |
| DEEPAGENT_QUEUE_LEASE_SECONDS | 300 | 队列消息租约，至少 30 秒 |
| DEEPAGENT_QUEUE_POLL_SECONDS | 0.25 | 消费轮询间隔 |
| DEEPAGENT_INGESTION_LEASE_SECONDS | 30 | 导入租约秒数，至少 3 秒 |
| DEEPAGENT_INGESTION_RECOVERY_LIMIT | 3 | 导入累计领取次数达到此值后不再自动恢复 |

数据库时间统一使用 UTC。生产使用 PostgreSQL；SQLite 仅支持本地单进程开发，不适用于跨进程 Worker。

PostgreSQL 默认连接超时 5 秒、单语句超时 15 秒、锁等待超时 5 秒，避免数据库异常无限挂起消费者；分别由 `DEEPAGENT_DB_CONNECT_TIMEOUT_SECONDS`、`DEEPAGENT_DB_STATEMENT_TIMEOUT_MS`、`DEEPAGENT_DB_LOCK_TIMEOUT_MS` 配置。显式连接串 options 可覆盖会话参数，长时间迁移需要单独评估维护窗口与超时。

## 自动化证据

`tests/test_runtime_concurrency.py` 在 SQLite 与真实 PostgreSQL 独立 Schema 上验证竞争领取、幂等创建、Thread 互斥、事务回滚、路由重复提交、过期 Lease、旧投递 ACK、Checkpoint / Pending Writes 拦截、导入并发发布和 Worker 退出恢复。

PostgreSQL 子进程测试实际终止已领取任务的 Worker，再创建新 Worker 验证恢复完成。为缩短测试，进程终止后由测试将该 Attempt 的租约时间设置到过去；这不是整套生产节点断电/网络分区演练。

`tests/test_coding_recovery.py` 在 SQLite 与真实 PostgreSQL 上验证配对发布前/后中断、较新 Pending Writes 隔离、嵌套审批、二进制/删除文件/临时文件恢复，以及实际 SIGKILL 子进程后的新 Worker 接续。另覆盖完成回答恢复不多调用模型、新 Run 输入不丢失、固定对象版本和损坏拒绝。沙箱文件与命令提供方仍是明确的测试实现；这些证据不能替代真实 Docker/mTLS/OSS 的跨节点验收。

## 尚需完成的可靠性工作

1. Sandbox Service 已加入服务端租约校验、旧命令排空及失效后终止残留进程；还需完整的真实 mTLS、Docker、数据库分区联动演练，不能用各组件单测替代分布式验收。
2. 配对恢复已加入并完成数据库/进程级测试；真实 Docker 临时卷及取消链路、跨 Linux 节点与真实对象存储恢复尚未通过最终验收。平台验证命令可能重跑，仍需后处理阶段的幂等凭证与完整故障矩阵。
3. 对有外部副作用的工具提供稳定幂等键、去重凭证和恢复策略；不能把租约实现解释成外部世界的 exactly-once。
4. 数据库长时间不可达、队列积压、取消/审批/重试竞争、限额耗尽、沙箱宿主机退出等更完整的故障及容量演练。
