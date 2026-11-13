# 数据库连接池改造方案

- 文档状态：已按本方案实施；实现与验收记录见[连接池验证报告](connection-pool-validation.md)。
- 编写日期：2026-11-13。
- 适用模块：`agent_permissions` 用户管理与 RBAC 后台。
- 改造前代码基线：`b5f49e0`，已支持 PostgreSQL 18 与 SQLite。下文“改造前行为”用于说明原始约束。

## 1. 建议结论

为 PostgreSQL 接入同步 `psycopg_pool.ConnectionPool`，每个 API worker 独立持有一个连接池，业务事务从池中借用连接，完成后归还。继续复用当前 `PostgresConnection`、`Result` 和 `DatabaseBackend` SQL 边界。

每个 worker 默认使用 **最小 2、最大 10 条连接，借用等待 3 秒、最多 20 个等待者**。这些参数已经生效，本地压测验证了连接复用与容量上限；40 并发时出现队列过载，因此它们仍是需要按目标负载调整的起步配置。

SQLite 保留现有的短连接模式，作为本地运行和离线测试后端；补齐统一生命周期接口。连接池不能改变 SQLite 的写入串行特性，池化还会引入跨线程连接管理，本轮将资源复用集中在当前 PostgreSQL 服务端连接上。

预期收益是减少 PostgreSQL 建连与认证开销、限制每个进程的数据库连接数，并为连接等待和过载提供明确边界。实际延迟与吞吐收益在实施后的相同负载压测中确认。

## 2. 改造前实现与约束

| 位置 | 改造前行为 | 改造要求 |
| --- | --- | --- |
| [postgres.py](../postgres.py) `PostgresDatabase.connect()` | 每次调用 `psycopg.connect()`，上下文退出后关闭连接 | 改为借用、归还池中连接 |
| [postgres.py](../postgres.py) `transaction()` | 读事务使用只读 `REPEATABLE READ`；写事务先取得统一事务级 advisory lock | 保留隔离级别、写锁顺序和提交、回滚语义 |
| [api.py](../api.py) `lifespan()` | 启动时初始化数据库，没有数据库关闭阶段 | 增加开池、预热和 `finally` 关池 |
| [storage.py](../storage.py) `DatabaseBackend` | 提供初始化、连接和事务接口 | 增加显式生命周期接口 |
| [service.py](../service.py) 登录、改密和失败审计 | 一个请求可能依次执行多个独立事务 | 每个事务独立借还，保持各自提交边界 |
| [__main__.py](../__main__.py)、[migrate_postgres.py](../migrate_postgres.py) | 一次性工具直接创建数据库适配器 | 工具也需要显式关闭自己的池 |

根环境使用 `psycopg 3.3.6`，`pyproject.toml` 声明 `psycopg[binary]>=3.2,<4`。本次已增加 `psycopg-pool>=3.2,<4`，由 `uv` 锁定为 `3.3.3`，复用根 `.venv`。驱动与池是独立发行包，见 [Psycopg 官方连接池说明](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)。

## 3. 生命周期与资源归属

### 3.1 一个 worker 一个池

`create_app()` 仍创建应用自己的数据库适配器。适配器构造时仅保存配置；实际连接池在该 worker 的 lifespan 中创建，显式使用 `open=False`，随后打开和等待最小连接数就绪。

池供同一进程内执行同步接口的线程共享，连接则由每次借用独占。不同 worker、不同副本和一次性 CLI 进程各自建池，不能跨进程共享物理连接。连接借用继续发生在同步业务路径中；异步 lifespan 通过 `run_in_threadpool()` 调用阻塞的开池、初始化与关池方法。

物理连接与用户登录会话没有一对一关系。连接轮换或应用正常重启不主动撤销持久化会话，用户仍通过原令牌在新的数据库连接上完成当前身份与权限校验。

数据库协议已经增加以下同步方法，方法名和行为保持统一：

| 方法 | PostgreSQL | SQLite |
| --- | --- | --- |
| `open()` | 创建并打开池，等待预热；已打开时幂等 | 无需预开连接 |
| `initialize()` | 使用已打开的池完成现有 schema 检查与初始化 | 保留现有初始化 |
| `connect()` | 借用连接并返回现有 SQL 适配器，退出时归还 | 保留创建、关闭连接 |
| `transaction(write=False)` | 在借用期内完成事务 | 保留现有事务 |
| `close()` | 关闭池，重复调用安全 | 无持久连接需要关闭 |

`connect()` 不负责隐式开池，避免错误调用在服务关闭期间重新创建连接池。显式 `open()` 若用于新的测试生命周期，应创建新的池实例；已关闭的 `ConnectionPool` 不能直接重新打开。该行为依据 [Psycopg 连接池生命周期 API](https://www.psycopg.org/psycopg3/docs/api/pool.html#psycopg_pool.ConnectionPool.open)。

### 3.2 FastAPI 启停

已接入以下生命周期顺序：

```text
进入 lifespan
  try
    在线程池中执行 database.open()，等待池预热
    在线程池中执行 database.initialize()
    yield，开始处理请求
  finally
    在线程池中执行 database.close()
```

预热失败或 schema 初始化失败时，启动失败并清理已创建的资源，配置 PostgreSQL 的应用保持使用同一数据源。`finally` 覆盖失败启动与正常关闭，而不是只在 `yield` 之后处理成功启动的场景。资源放在 lifespan 的启动和清理阶段，符合 [FastAPI 官方生命周期用法](https://fastapi.tiangolo.com/advanced/events/)。

正常关闭时，由 HTTP 服务先停止接收新请求并等待在途请求结束，再关闭池。`pool.close(timeout=5)` 的超时是等待池工作线程退出的时间；已借出的连接在归还时关闭，不能把它解释为强制在 5 秒内结束所有数据库操作。详见 [Psycopg close API](https://www.psycopg.org/psycopg3/docs/api/pool.html#psycopg_pool.ConnectionPool.close)。

### 3.3 CLI、迁移与测试

- 管理员初始化命令负责打开、初始化和最终关闭自己的数据库适配器，成功、重复初始化、异常及用户取消都要经过清理。
- 迁移 CLI 负责目标 PostgreSQL 池的生命周期；`migrate(source, target)` 使用调用方传入的已打开适配器，不关闭调用方资源。保留源 SQLite 快照、目标事务、逐表校验与失败回滚逻辑。
- 迁移测试中直接构造的 `PostgresDatabase`，须在 setup 中显式打开，并注册关闭清理；先关池，再删除隔离 schema。
- 同一应用重复进入 `TestClient` 时，验证生命周期可以完整结束并重新建立资源，不遗留上一轮池线程。
- CLI 可显式配置 `min_size=max_size=1`，减少一次性任务的闲置连接；若继承 API 参数，其连接预算也要单独计算。

## 4. 连接借还与事务规则

### 4.1 借用边界

`PostgresDatabase.connect()` 使用 `pool.connection(timeout=...)` 管理借还，保持驱动 `autocommit=True`、`connect_timeout=10` 和现有 `application_name`。健康检查等连接级操作不会因此隐式开启业务事务。

保持驱动默认的元组行格式，继续由当前 `Result/Record` 适配字段名与下标读取，避免在接入池时改变业务 SQL 的返回结构。

一次业务事务的执行顺序为：借用连接、开始事务、设置事务局部参数、完成鉴权与业务 SQL、提交或回滚、释放本次游标、归还连接。业务层仍通过现有 `Connection` 协议执行 SQL。

`PostgresConnection` 需要统一管理自己创建的游标，在借用结束前显式释放。跨借用边界只传递已经物化的 `Record`、字典或列表；不返回仍依赖连接的 `Result`、游标或延迟迭代器。正常提交与异常回滚之后都要释放资源。

清理失败时保留主要业务或数据库异常，将不可靠的物理连接关闭并交由池补充；清理异常不能覆盖原始失败原因，也不能提前归还仍在执行 SQL 的连接。

池上下文提供归还及异常处理的兜底，业务事务的显式提交、回滚仍由当前适配器负责。连接失效时由池淘汰、补充，不把失效连接重新交给业务。相关机制可核对 [Psycopg 官方池实现](https://github.com/psycopg/psycopg/blob/master/psycopg_pool/psycopg_pool/pool.py)。

### 4.2 保留授权与一致性边界

1. 写事务仍在第一次鉴权读取前取得 `pg_advisory_xact_lock`，保持现有 `WRITE_LOCK` 命名空间。
2. 读事务继续使用只读 `REPEATABLE READ`，事务内的授权与数据使用一致快照。
3. 保留 `SET LOCAL lock_timeout='10s'`、`SET LOCAL statement_timeout='30s'`。
4. RBAC 每次请求仍从数据库读取；不把当前用户、角色或授权结果缓存到物理连接上。
5. 用户修改、角色调整、会话撤销和成功审计仍在原事务内原子提交，继续检查 `If-Match` 和最后管理员保护。
6. 限流事务、业务事务与失败审计事务保持独立。不能为复用一条连接将它们合并，否则业务失败可能回滚本应保留的失败尝试计数。

因此，一次登录可以依次借用两次连接，但不需要同时持有两条。连接池也不会取消当前全局写入串行规则；增加池大小不能直接增加并行写入能力。

### 4.3 健康检查与连接状态

建议启用 `check=ConnectionPool.check_connection`，在交付空闲连接前进行可用性检查。保留池的事务状态清理机制，归还后应处于 `IDLE`；异常回滚失败或连接已经失效时丢弃连接。当前业务仅使用事务局部配置，首期不增加全量会话重置操作。

实施时验证借用之间的事务状态、只读属性和局部超时互不泄漏，并保留 DSN 自带的 `options`，尤其是集成测试的 `search_path`。不能用新的连接参数覆盖原有 schema 配置。

**3 秒池等待不是整个 HTTP 请求的截止时间。** 借出前检查本身需要网络交互，已经开始的 SQL、锁等待和网络故障也有自己的耗时边界；不能声称池参数能中断所有网络 I/O。上线前应在目标环境验证连接超时及适用的 libpq TCP 参数。健康检查与借用截止时间的实际调用关系见 [官方池源码](https://github.com/psycopg/psycopg/blob/master/psycopg_pool/psycopg_pool/pool.py)。

## 5. 配置建议与容量预算

### 5.1 已实施配置

以下环境变量均已新增，作用于 PostgreSQL 后端，单位已经写入名称：

| 环境变量 | 建议默认值 | 用途 |
| --- | ---: | --- |
| `AGENT_USERS_DB_POOL_MIN_SIZE` | `2` | 每个 worker 的最小连接数 |
| `AGENT_USERS_DB_POOL_MAX_SIZE` | `10` | 每个 worker 的连接上限 |
| `AGENT_USERS_DB_POOL_TIMEOUT_SECONDS` | `3.0` | 等待借到连接的预算 |
| `AGENT_USERS_DB_POOL_MAX_WAITING` | `20` | 等待借用的队列上限 |
| `AGENT_USERS_DB_POOL_STARTUP_TIMEOUT_SECONDS` | `30.0` | 等待最小连接数预热就绪的预算 |
| `AGENT_USERS_DB_POOL_MAX_IDLE_SECONDS` | `300.0` | 超出最小容量的空闲连接回收参数 |
| `AGENT_USERS_DB_POOL_MAX_LIFETIME_SECONDS` | `1800.0` | 连接轮换参数，由池安排回收 |
| `AGENT_USERS_DB_POOL_RECONNECT_TIMEOUT_SECONDS` | `60.0` | 连续重连失败触发告警的时间阈值 |
| `AGENT_USERS_DB_POOL_CLOSE_TIMEOUT_SECONDS` | `5.0` | 关闭池时等待工作线程的预算 |

池的参数含义与默认行为参考 [Psycopg 参数文档](https://www.psycopg.org/psycopg3/docs/api/pool.html#psycopg_pool.ConnectionPool)。这里的数值是本服务建议值，而不是库的默认值。连接轮换不会强行中断正在执行的业务事务。

配置校验要求：

- 本服务要求 `1 <= min_size <= max_size`，启动阶段实际验证至少一条连接。
- `max_waiting` 必须为正整数；不采用库中表示无限队列的 `0`。
- 所有秒数必须为有限正数，拒绝 `NaN`、无穷大和非数值。
- `Settings.from_env()` 需分别解析整数与浮点数，扩展现有统一 `int()` 的解析方式。
- 环境变量继续优先于显式配置文件；池参数为启动配置，调整后通过重启 worker 生效。

现有物理建连 `connect_timeout=10`、事务锁超时 10 秒和单条 SQL 超时 30 秒保持原有值。池预热超时只覆盖预热等待，不等同于整个应用初始化超时；schema 初始化仍受其事务和 SQL 规则约束。

部署配置可以覆盖以下非敏感参数；保留原数据库连接串即可：

```dotenv
AGENT_USERS_DB_POOL_MIN_SIZE=2
AGENT_USERS_DB_POOL_MAX_SIZE=10
AGENT_USERS_DB_POOL_TIMEOUT_SECONDS=3.0
AGENT_USERS_DB_POOL_MAX_WAITING=20
AGENT_USERS_DB_POOL_STARTUP_TIMEOUT_SECONDS=30.0
```

配置生成器保留现有私有 `.env`；新默认值写入 `Settings` 和 `.env.example`，由部署者按需要覆盖。

### 5.2 按进程计算数据库预算

设 `P` 为部署与滚动发布期间同时存在的 API 副本峰值，`W` 为每副本 worker 数，`C` 为每池最大连接数：

```text
API 连接预算 = P × W × C
API 预算 + CLI/迁移池预算 + 其他客户端预算
    <= PostgreSQL max_connections - 数据库保留连接预算
```

例如，1 副本 × 1 worker、`max_size=10` 时，API 的预算为 10；2 个副本各 4 个 worker 时为 80；滚动发布若峰值同时存在 3 个副本，则变为 120。部署前需读取数据库实际连接上限及保留配置，再确定参数，不能按“整个服务只有 10 条连接”估算。

首次从短连接版本滚动切换时，旧实例没有池上限，其并发连接要单独估算；本地和测试环境可先排空、停止旧 API，再启动池化版本，避免把旧实例也误按新池上限计算。

池大小还要与同步线程池额度共同规划。`max_waiting` 只限制已经向数据库池申请连接的等待者，不限制尚在 HTTP 或线程池入口排队的请求。

## 6. 异常、过载与恢复

### 6.1 对外错误语义

| 情况 | HTTP 结果 | 内部处理 |
| --- | --- | --- |
| 借用等待超时 `PoolTimeout` | `503 SERVICE_UNAVAILABLE` | 记录池等待超时原因与请求标识 |
| 等待队列满 `TooManyRequests` | `503 SERVICE_UNAVAILABLE` | 不继续排队，记录池队列过载 |
| 池未打开或已关闭 `PoolClosed` | `503 SERVICE_UNAVAILABLE` | 记录生命周期状态，排查资源归属 |
| 数据库断连或连接故障 | 沿用数据库错误的 `503` | 失效连接归池时淘汰，后续请求重新借用 |
| 用户权限、版本或业务规则不满足 | 保留现有 `401/403/409/412` 等 | 保留原业务错误与失败审计规则 |

上述池异常继承 `psycopg.OperationalError`，见 [官方异常定义](https://github.com/psycopg/psycopg/blob/master/psycopg_pool/psycopg_pool/errors.py)。现有数据库错误边界可以继续返回统一 `503`，但需要区分池故障的内部原因。池队列满不使用业务限流的 `429`，避免混淆账号限流与数据库过载。

响应继续携带原有 `request_id`、`Cache-Control` 等字段；服务端返回脱敏说明。连接池故障不能清除客户端本来有效的登录会话，也不能因鉴权数据不可读而扩大权限。

### 6.2 防止失败审计再次排队

当前 `error_response()` 会调用 `record_failure()`，后者再次申请数据库事务。若首次错误来自池耗尽或连接不可用，再走同一条路径会重复等待。

明确的池可用性错误和建连类错误已改为直接记录结构化应用日志，跳过数据库失败审计的第二次借用。日志仅包含 `request_id`、动作、故障类别、worker 和可用池指标；不打印数据库 URL、密码、令牌或异常原文。其他业务拒绝仍沿用独立失败审计；成功变更及其审计继续原子提交。

具体在仍持有原异常的 `persistence_error()` 中识别池异常，以及经异常来源、连接状态或 SQLSTATE 确认的连接故障，再通过 `error_response()` 的显式审计开关控制是否调用 `record_failure()`。不能把所有 `OperationalError` 都直接等同于连接不可用，也不能在统一包装成 `ApiError` 后才尝试还原故障类别。

还需检查 `psycopg.pool` 后台日志的脱敏策略，使用含敏感文本的故障注入验证日志与 HTTP 响应。不能只验证接口没有泄露信息。

### 6.3 故障恢复

运行中的池按库策略重建失效连接；达到重连告警阈值时报告故障，并通过恢复后的新借用验证连接重新可用。连接补充和重连行为参考 [官方池维护说明](https://www.psycopg.org/psycopg3/docs/advanced/pool.html#pool-connection-and-sizing)。

构造池时配置 `reconnect_failed` 回调记录受控告警，回调不主动关闭池或退出 worker；恢复行为通过故障测试验证，告警按进程与时间窗口聚合。

事务已经交给数据库后发生断连，提交结果可能不确定，不能自动重放用户创建、角色修改、改密等写操作。查询确认和重试继续遵循现有接口契约。同步操作也不会因为浏览器断开立即停止；连接只在工作线程真正完成清理后归还，避免将仍在执行的连接交给下一位借用者。

## 7. 可观测性

在适配器边界提供受控的池统计快照，优先采集：

| 指标 | 关注点 |
| --- | --- |
| `pool_size`、`pool_available` | 当前管理容量及空闲连接 |
| `requests_waiting` | 当前排队数量 |
| `requests_num`、`requests_queued` | 借用量及发生排队的次数 |
| `requests_wait_ms`、`requests_errors` | 累计等待与借用失败 |
| `connections_num`、`connections_errors` | 物理建连尝试与失败 |
| `returns_bad` | 归还连接状态异常 |

使用 `get_stats()` 获取累计快照，不因监控读取清空计数。部分零值字段可能缺失，采集方用 `get(key, 0)` 处理；字段以锁定版本验证，参考 [官方统计说明](https://www.psycopg.org/psycopg3/docs/advanced/pool.html#pool-stats) 及 [统计实现](https://github.com/psycopg/psycopg/blob/master/psycopg_pool/psycopg_pool/base.py)。

统计按进程标记，不包含用户、密码和连接串。本期先提供内部采集与受控日志，不为池状态新增公开业务接口。若接入现有监控系统，再沿用该系统的指标出口。

## 8. 实施文件

| 文件 | 改动 |
| --- | --- |
| 根 `pyproject.toml`、`uv.lock` | 增加并锁定 `psycopg-pool` 依赖 |
| `agent_permissions/config.py` | 池参数、浮点解析、交叉校验 |
| `agent_permissions/storage.py` | 明确 `open/close` 生命周期契约 |
| `agent_permissions/postgres.py` | 池的创建、借还、游标清理、关闭与统计 |
| `agent_permissions/database.py` | SQLite 生命周期兼容及工厂传递配置 |
| `agent_permissions/api.py` | lifespan 完整清理及池故障错误边界 |
| 新增 `agent_permissions/database_errors.py` | 分类池故障与连接故障；失败审计降级在 API 错误边界完成，`service.py` 事务保持不变 |
| `agent_permissions/__main__.py` | 管理员初始化工具的资源归属 |
| `agent_permissions/migrate_postgres.py` | 迁移 CLI 的目标池生命周期 |
| `agent_permissions/test_postgres.py` | 直接构造适配器与测试清理适配 |
| 新增 `agent_permissions/test_connection_pool.py` | 池复用、过载、故障和生命周期测试 |
| `agent_permissions/deploy/.env.example` 与运行文档 | 参数示例、容量预算和运维说明 |

沿用现有 21 个业务接口、前端交互、RBAC schema 2 和数据源选择规则。此次池化不要求重新导入数据或重新初始化管理员。

## 9. 验证与验收

以下为本次改造的验收标准；执行结果、测试范围与本地压测限制见[连接池验证报告](connection-pool-validation.md)。

| 场景 | 方法与通过条件 |
| --- | --- |
| 正常开池与失败启动 | 预热完成后才服务请求；预热或 schema 初始化失败均关闭资源，无额外闲置池 |
| 连接真实复用 | 在 `min=max=1` 时顺序执行多次事务，`pg_backend_pid()` 保持相同；预热后无故障期间物理建连计数不因每次事务递增 |
| 借用超时 | 占用唯一连接后，第二次借用在配置预算附近返回超时；释放后可继续使用，不泄漏容量 |
| 队列上限 | `max_size=1/max_waiting=1` 时占用连接和唯一队列位置，额外申请得到 `TooManyRequests`；等待者结束后容量恢复 |
| 事务与状态清理 | 成功提交、业务异常、SQL 异常和回滚失败分别验证；正常归还为 `IDLE`，失效连接被替换，局部参数不泄漏；`max_size=1` 时业务拒绝后的独立失败审计也能完成 |
| 授权与并发保护 | 复用现有 RBAC、角色撤销、最后管理员、会话撤销和审计回滚测试，证明物理连接复用不复用旧授权 |
| 故障恢复 | 在独立测试环境终止测试池已知 backend PID，验证失效检测、后续补连和业务恢复；写操作不会自动重放 |
| 错误响应与审计降级 | 池超时、队列满、池关闭均为脱敏 `503`；统计故障请求的借用次数，确认不会因失败审计再次排队 |
| 关闭与取消 | 重复关闭安全，关闭后拒绝新借用；在途工作完成后释放连接；测试池无遗留物理连接和工作线程 |
| 独立入口 | CLI 成功、失败和迁移异常都关闭池；重复 `TestClient` 生命周期及多应用实例之间不共用池 |
| 兼容回归 | SQLite 原有契约继续通过；PostgreSQL 真实集成测试执行成功，不能将跳过用例算作通过 |

PostgreSQL 故障注入使用专用测试数据库或容器，并沿用随机 schema 清理；只终止测试池自己的连接。当前写锁是同一数据库内共享的，隔离 schema 本身不能隔离该 advisory lock 的竞争。

实施后的基础检查包括 SQLite API/RBAC/Console 托管测试、PostgreSQL 集成测试、新增连接池测试及 Ruff 检查。浏览器沿用现有操作回归；默认 SQLite 浏览器测试不作为 PostgreSQL 池复用与故障恢复的证明。

压测用同一数据库、数据集、worker 数和接口比例对比改造前后，先预热再记录并发 `1/10/20/40` 下的 P50、P95、P99、吞吐、失败率、排队与建连增量。读操作与含写操作的负载分别记录，避免把现有写锁竞争误判为池容量不足。先验证连接确实复用、容量有界和资源无泄漏，再据实调整默认值。

## 10. 实施、发布与回退顺序

1. 评审本稿范围和起步参数，核对 API 副本峰值、worker 数及 PostgreSQL 连接预算。
2. 增加根依赖与配置，完成适配器生命周期、连接借还和统一清理。
3. 适配 FastAPI、CLI、迁移与测试资源归属，处理池故障的失败审计路径。
4. 通过真实 PostgreSQL 池测试、既有业务契约与前端回归，记录基线和压测结果。
5. 在本地或测试环境使用相同 PostgreSQL URL 重启 API，检查原账号登录、角色权限、会话与监控统计；保留既有数据库及 volume。
6. 按包含滚动重叠进程的容量预算发布，观察队列、借用失败和物理建连情况后再调参。

本改造保持 schema 2，不涉及数据重建。需要回退时先排空在途请求，再关闭池、回退应用代码与依赖锁文件，继续连接同一 PostgreSQL 数据库。回退应用版本不等于切回历史 SQLite 快照；后者涉及切换后的数据同步问题，仍遵循 [数据库迁移与回退说明](docker-database.md)。

实施前需核实的部署信息是副本和 worker 数、数据库实际连接上限及保留量、其他客户端连接预算，以及目标并发与延迟。本文已经给出可用于本地验证的起步配置，生产容量在取得这些信息后计算并压测确认。
