# Agent 操作与授权审计

Agent 每次申请业务工具都会得到服务端生成的 `call_id`。通过表单读取候选项或核对目标会生成关联的子调用。普通管理页面沿用既有业务审计。列表中每行是一项操作。详情可以回答谁发起、代表哪个用户、谁开启或关闭了工具、谁确认过表单、实际调用了哪个接口，以及业务是否生效。采集从业务库升级到 schema 6 后开始。升级前的操作仍可查原业务库的 `audit_events`。

## 查一次操作

登录控制台后，从侧栏进入「Agent 操作审计」。在「操作记录」中筛选并打开调用详情，在「授权变更」中查看工具开关的修改历史。

使用当前账号的 Bearer 会话令牌。三个查询接口均要求 `agent.audit.read`，升级时内置 `admin` 角色获得该权限。自定义角色可以在现有角色管理中显式授予。查阅行为会写入原有管理审计。

| GET 接口 | 查询内容 |
| --- | --- |
| `/api/v1/audit/agent-operations` | 主操作列表。支持 `actor_user_id`、`subject_user_id`、`agent_id`、`operation_id`、`target_id`、`run_id`、`business_outcome`。指定 `include_children=true` 可包含表单候选项和目标版本核对等辅助读取。 |
| `/api/v1/audit/agent-operations/{call_id}` | 操作摘要、每次尝试、当时的权限判断、按源端序号排列的事件，以及关联的子调用。直接调用 `POST /agent-tools/call` 的响应头 `X-Agent-Call-ID` 可用于定位。 |
| `/api/v1/audit/agent-grant-changes` | 工具开关的前后值、修改人、授权所属用户、配置版本和原业务审计 ID。支持 `actor_user_id`、`subject_user_id`、`operation_id`、`grant_version`。 |

两个列表都支持带时区的 `started_from`（含）和 `started_to`（不含）。`limit` 默认 50，最大 100。下一页沿用相同筛选条件并传回 `data.next_cursor`。列表按源端时间和调用 ID 倒序排列。采集和投递异步进行，刚结束的操作可能稍后才出现在查询结果中。

下例查询一次密码重置，再读取详情。环境变量 `USER_SESSION_TOKEN` 使用拥有审计权限的当前用户会话。

```sh
curl -sS -G "$API_BASE/api/v1/audit/agent-operations" \
  -H "Authorization: Bearer $USER_SESSION_TOKEN" \
  --data-urlencode "operation_id=user.password.reset" \
  --data-urlencode "subject_user_id=$AUDITED_USER_ID" \
  --data-urlencode "limit=10"

curl -sS "$API_BASE/api/v1/audit/agent-operations/$CALL_ID" \
  -H "Authorization: Bearer $USER_SESSION_TOKEN"
```

## 怎样解读结果

- `entrypoint` 标识 AG-UI、兼容 Chat、会话任务或直接工具 API。运行时将 `agent_id` 标为 `agent_permissions.chat`。仅有用户令牌的直接调用可以归到实际账号，Agent 身份留空。
- `permission_checks` 保存每次实际校验时的工具开关、修改人、配置版本、角色和满足或缺失的权限。确认表单时如果已撤权，这次尝试会显示 `TOOL_NOT_GRANTED`。主操作继续等待，`dispatch_state=not_sent`，本次尝试没有确认人。
- `dispatch_state=received` 表示业务接口已经收到请求。`business_outcome` 的四种取值为 `not_executed`、`succeeded`、`failed`、`unknown`。工具入口的 HTTP 状态与业务 HTTP 状态分别保存在 `control_http_status` 和 `business_http_status`。例如工具入口可能返回 200，而业务接口返回 403。
- 写操作的成功事件与业务变更同事务提交。读取以实际响应与已保存的结果摘要为证。重置密码可以看到目标用户 ID 和 `change_summary.password_changed=true`。输入密码、令牌、表单敏感值、自由搜索词、模型正文和完整业务响应均从采集字段中剔除。
- HITL 重试沿用同一个调用 ID，每次尝试有各自的 `attempt_id`。表单实际读取用户或角色时使用子调用 ID。请求已派发而结果尚未核实时保留 `unknown`，已有提交回执可在恢复时核对。

## 启动与配置

本地运行原有 API 即可使用。默认投递到独立的 SQLite 审计文件，例如业务库 `users.sqlite3` 对应同目录的 `users.audit.sqlite3`。线上可将审计服务作为独立进程，并为它配置专用 PostgreSQL 数据库和账号。业务服务与审计服务之间只通过私有 HTTP 接口通信。

| 配置项 | 使用位置与含义 |
| --- | --- |
| `AGENT_USERS_AUDIT_SERVICE_URL` | 业务进程使用的审计服务地址。例如本机 `http://127.0.0.1:8011`。远程使用 HTTPS。留空时使用本地接收器。 |
| `AGENT_USERS_AUDIT_WRITE_TOKEN`、`AGENT_USERS_AUDIT_READ_TOKEN` | 独立部署时在两端分别注入相同的一组凭据。投递和查询各使用一个随机值，每个至少 32 个字符。 |
| `AGENT_USERS_AUDIT_PRODUCER_ID` | 生产方标识，两端一致，默认 `agent_permissions`。接收器校验此标识与写入身份。 |
| `AGENT_USERS_AUDIT_DATABASE_URL`、`AGENT_USERS_AUDIT_DATABASE_PATH` | 接收端使用专用 PostgreSQL URL，或指定审计 SQLite 文件路径。 |
| `AGENT_USERS_AUDIT_RETENTION_DAYS`、`AGENT_USERS_AUDIT_OUTBOX_DAYS` | 默认保留终态明细 90 天，终态调用已投递的本地事件保留 7 天。待投递、隔离和结果尚待核实的证据继续保留。 |
| `AGENT_USERS_AUDIT_STORE_MODE` | `automatic` 默认由接收端建表和清理。`managed` 由维护账号执行初始化和清理，适合为常驻服务配置更小的数据库权限。 |

在两个终端分别启动审计进程和原业务 API。两个私有配置文件可以分别只包含各进程所需的数据库连接。示例中的业务配置包含审计服务地址。远程部署在受限网络入口终止 TLS。

```sh
make -C agent_permissions audit-serve ENV_FILE=/private/config/agent-audit.env
make -C agent_permissions serve ENV_FILE=/private/config/agent-users.env
```

独立服务的 `POST /internal/v1/agent-audit/events` 使用投递凭据。内部 `GET /internal/v1/agent-audit/operations`、详情和 `/grant-changes` 使用只读凭据。控制台和 SDK 使用业务服务的三个公开查询接口。审计 API 在工具注册表中均不可调用。

## 投递、维护与排障

业务库先持久化脱敏事件。多个工作进程通过租约批量认领，发送期间释放业务库事务。审计服务离线时调用仍可使用本地待发送表。确认响应丢失会触发重投，相同事件去重，内容冲突的事件进入 `quarantined`。操作摘要按源端序号生成，较晚收到的等待事件不会覆盖已提交的结果。

若本地审计写入失败，尚未派发的允许调用返回 `503 AUDIT_UNAVAILABLE`。事务内的业务变更回滚，读取结果也只在本地留证后交付。已确定的 401/403 维持原来的拒绝结果，审计存储故障通过运行日志报告请求 ID。

```sh
uv run python -m agent_permissions.audit_admin status --env-file /private/config/agent-users.env
uv run python -m agent_permissions.audit_admin init-store --env-file /private/config/agent-audit-maintenance.env
uv run python -m agent_permissions.audit_admin prune-store --env-file /private/config/agent-audit-maintenance.env
```

`status` 显示待投递数、最老待投递事件时长、隔离数和已经派发但尚无确定业务结果的调用数。`managed` 模式下先用维护身份执行 `init-store`，再启动常驻服务。常驻 PostgreSQL 账号需要读取审计表、追加事件与授权差异、写入和更新操作索引。保留期清理使用独立维护身份定期执行 `prune-store`。每批每类最多清理 500 项，输出删除事件数，清理动作留存在 `agent_audit_maintenance`。清理后保留用于识别重投的随机标识和内容摘要。记录仍有序号缺口时保留明细等待补投。

已有存量业务库支持 SQLite 和 PostgreSQL 原地升级。跨库迁移包含调用关联和待发送事件。切换业务实例时同时保留原审计库，已投递的历史明细由审计库持有。[数据库迁移](docker-database.md)

## 本地验证记录

在临时业务库和独立审计库中，以同一已登录账号连续调用 30 次 `user.list`。先预热 3 次，测量期间暂停自动投递。对照组仅在测试进程中跳过源端事件落库，保留相同的鉴权、工具执行和响应路径。随后主动投递积压，并从审计查询接口读取 10 项操作。下表记录一次本机测量，耗时单位为毫秒。

| 存储 | 对照组调用 P50 | 启用审计后调用 P50 | 投递积压的 180 个事件 | 获取一页的审计库 SELECT 次数 |
| --- | ---: | ---: | ---: | ---: |
| SQLite | 6.52 | 12.63 | 15 | 1 |
| PostgreSQL 18 | 9.47 | 17.21 | 101 | 1 |

另用两个独立 Uvicorn 进程经环回 HTTP 验证离线恢复。先关闭接收端执行 8 次业务查询，再启动接收端。按返回的 8 个调用 ID 逐项核对，全部可查，业务结果均为成功，工具授权变更也可查。日常回归覆盖丢失响应、撤权、事务回滚、乱序、重复投递、隔离和保留期清理。[审计回归用例](../test_audit.py)
