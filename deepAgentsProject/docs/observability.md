# 可观测性与故障定位（Schema 19）

本页描述已经加入代码的采集、安全边界和验收方式，不代表生产采集器、通知接收人或外部审计存储已经部署。

## 请求与持久任务关联

API 为每次业务 HTTP 请求生成新的 `X-Request-ID` 和 `X-Trace-ID`，响应通过 CORS 显式开放这两个头。公网调用者传入的同名头、`traceparent` 和 baggage 不作为可信来源。前端 API 错误可显示经过格式检查的 Request ID，便于报障，不显示凭据或响应内部堆栈。

Run 创建和知识上传完成时，追踪来源与业务记录在同一事务提交到 `run_trace_origins` / `ingestion_trace_origins`。幂等重试保留第一次来源，事务失败不留下孤立关联。Worker 从这些受平台控制的表恢复父上下文，不从用户可编辑的 metadata 恢复。独立进程、任务重试与取消收尾因此可以关联到原始请求；重试请求本身仍有新的 HTTP Request ID。

`GET /api/v1/runs/{id}` 在原有资源权限检查后返回 `observability` 中的 `trace_id`、`request_id`，没有历史来源时为 null。这不是追踪查询接口，也不授予采集器访问权限；当前控制台没有集成外部 Trace 浏览器。

观测范围包括 HTTP/流式响应生命周期、Run Attempt、取消收尾、知识摄取，以及模型调用、沙箱命令、扫描、解析和 Embedding 子操作。普通模型网关与原生 LangChain 模型回调均有接入。Trace 不记录 prompt、模型回答、命令正文、文档、请求路径参数/查询串、上传地址、认证头或错误原文。Sandbox Service 本身尚未加入跨服务远程父上下文传播，不能宣称全链路所有节点已覆盖。

Trace 是有采样、可能丢失的诊断数据，不能替代持久事件、审计和费用账本。操作指标记录当前进程观察到的尝试，包含重试；不能用其计数推导业务精确一次执行或供应商最终账单。默认根采样率 0.1，持久任务沿用原始采样决定。没有导出端点的开发环境仍可产生关联 ID，但采集器中不会存在对应 Trace。

## 脱敏日志

生产 API、Worker、Sandbox Service、迁移入口使用 `apps/logging.json` / `SafeJsonFormatter`。只输出固定事件、时间、级别、代码位置、可信关联 ID、有限数值和异常类型/调用位置；不格式化任意日志消息、参数或异常原文，也不输出堆栈源代码及局部变量。未知第三方日志保留 `diagnostic` 和代码位置，不依赖正则猜测秘密。

普通 access log 被关闭，避免路径、查询参数和签名 URL 外泄。诊断时按 Request ID → Trace ID → Run/Attempt/Job ID 关联；不要为找错误临时开启正文日志。开发者直接启动 Uvicorn 时，要显式传入 `--log-config apps/logging.json --no-access-log` 才能获得相同日志策略。外围代理、第三方采集器和宿主机日志策略不在本应用格式器的控制范围内，须独立验收。

## 生产配置与凭据

具体文件引用见 `deploy/platform.env.example` 和 `deploy/platform.compose.yaml`，部署预检会检查挂载契约。API/Worker 必须配置：

| 配置 | 契约 |
| --- | --- |
| `DEEPAGENT_METRICS_TOKEN_FILE` | 采集专用 Bearer token；不接受用户会话替代 |
| `DEEPAGENT_OTLP_TRACES_ENDPOINT` | 运维批准的 HTTPS 地址，明确以 `/v1/traces` 结尾，无用户名、密码、查询串或 fragment |
| `DEEPAGENT_OTLP_TOKEN_FILE` | 独立 OTLP 采集凭据，不放入普通环境配置值 |
| `DEEPAGENT_OTLP_CA_FILE` | 校验采集器证书的 CA 文件；启动时检查 HTTPS CA 可加载 |
| `DEEPAGENT_TRACE_SAMPLE_RATE` | 有限数值，0 到 1；默认 0.1，按采集容量和诊断要求批准 |
| `OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED` | 生产必须为 `true`，用于追踪队列容量和丢弃可见性 |

Token 要求 32–512 个 URL-safe 字符。生产只从文件读取；文件权限与轮换遵循生产交付契约，当前配置在进程启动时加载，不承诺热轮换。为避免 SDK 隐式改变出口、附加头或资源标签，其余环境级 `OTEL_*` 配置被拒绝；接入企业统一注入前应评审显式适配，而不是删掉校验。

OTLP 使用 HTTP/protobuf，不使用 gRPC。出口忽略环境代理/netrc，拒绝所有重定向，并校验证书；需要代理时应先设计显式受控出口，不能关闭 TLS。后台导出队列最多 2048 个 Span，每批最多 128，默认间隔 1 秒，导出超时 3 秒。采集器故障不无限积压，也不要求业务请求等待同步导出；失败批次和队列丢弃有独立指标。采集器未发送数据的闲置期不等于已探测其可用。

## 指标与采集拓扑

- API：`GET /metrics`，必须通过已有 HTTPS 管理入口采集，且保留 Host 校验与网络访问控制。未配置 token 返回 503，错误/重复 Authorization 返回 401。普通登录不会取得指标权限。
- Worker：独立 TLS 管理监听器，生产必须配置 `DEEPAGENT_WORKER_METRICS_PORT`、`DEEPAGENT_METRICS_TLS_CERT_FILE`、`DEEPAGENT_METRICS_TLS_KEY_FILE`；示例端口 9108。只提供 `/metrics`，没有业务 API，部署清单不发布宿主机端口。管理监听器退出属于关键任务故障，Worker 清理后失败退出。
- `prometheus.example.yaml` 是接入既有安全 Prometheus 的示例，不会创建采集器、Grafana 或 Alertmanager。为每个进程配置稳定独立目标；**不能把轮询到不同 API/Worker 副本的负载均衡地址当作一个指标实例**，否则进程计数器和故障定位会失真。API 管理入口应一对一映射目标副本；证书 SAN、Host 白名单与实际目标保持一致。

HTTP 指标仅使用有限的方法、服务端路由模板与状态码标签；未知路径统一 `__unmatched__`。不把用户、租户、模型、Run 或原始 URL 放入标签。HTTP 延迟是完整响应/流生命周期，不应把 SSE 长连接和普通接口混算成相同延迟 SLO。

队列、Worker 数量和取消积压读取缓存健康快照；抓取指标不查询数据库。过期快照明确输出 `deepagent_observation_fresh=0`，且不输出虚假的零积压/零等待值。数据库级 Gauge 在多个副本间用 `max by (cluster, kind)` 等去重，不能按采集副本求和；进程级 Counter 应先 `rate` 再聚合。必须为同一数据库集群配置一致且不与其他集群重名的 `cluster` 标签。

## 告警与验证

`deploy/monitoring/alerts.yaml` 包含 8 条规则：目标不可抓取、观测过期、Worker 缺失、排队过久、取消收尾过久、HTTP 错误比例、Trace 导出失败和有界队列丢弃。阈值是初始运维策略，不是压测所得承诺；健康阈值变化后，应同步评审排队告警阈值。

```bash
# 使用经官方 SHA-256 校验的 Prometheus 3.13.2 promtool；不启动服务。
python scripts/monitoring_checks.py --promtool /absolute/path/to/promtool
```

检查同时运行规则语法及故障触发/恢复测试，包括低流量错误率不误报、多个采集副本不重复累加全局状态。CI 固定官方工具版本和下载哈希。规则测试通过不等于消息已送达值班人员；生产还必须配置接收渠道、告警负责人、去重/静默/升级策略，并做实际触发—送达—确认—恢复演练。

`tests/test_telemetry.py` 覆盖并发请求上下文隔离、伪造头拒绝、动态路径基数、流取消、无正文日志、独立采集凭据、SQLite/真实 PostgreSQL 持久来源与回滚、真实子进程恢复、OTLP protobuf 发送/重定向拒绝、带证书验证的 Worker HTTPS、模型回调收尾及真实 SDK 队列溢出。测试采集器和 TLS 服务是受测试所有的本机夹具，不代表企业采集平台已验收。准确批次成绩见 [实施状态](enterprise-hardening-status.md)。

## 升级与尚未完成

Schema 19 只新增两张来源表，不回填猜测出来的历史追踪数据。生产在维护窗口停止旧写入者，使用独立迁移任务升级，并统一部署 API/Worker；运行数据库账号须有新表的必要读写权限。无需把新表授权给 Sandbox Service。此前 Schema 18 的取消权限视图和升级要求仍适用。不要直接删除迁移记录或回退数据库版本；应用回滚兼容性必须另行验证。本批未迁移业务数据库。

必须继续完成：企业采集平台与真实接收渠道接通及容量/故障演练；链路浏览和仪表盘；对象存储、扫描器、远程沙箱端到端健康；独立且防篡改的审计归档与保留/访问策略。采集端自己的认证授权、存储加密、留存和可用性不由本代码自动提供。

实现参考：[OpenTelemetry Python 导出文档](https://opentelemetry.io/docs/languages/python/exporters/)、[Prometheus 指标实践](https://prometheus.io/docs/practices/instrumentation/)、[告警规则文档](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)。
