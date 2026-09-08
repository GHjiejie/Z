# 生产隔离与密钥边界

这份文档描述已经加入代码的生产隔离能力及部署契约。它不代表整个企业化改造已经完成；可观测性、交付治理、备份恢复、配额和最终生产演练仍需按实施计划验收。

## 进程与信任边界

- API：`DEEPAGENT_PROCESS_ROLE=api`，处理认证、权限、发布和任务接收，不消费执行队列。
- Worker：`DEEPAGENT_PROCESS_ROLE=worker`，消费 PostgreSQL 持久队列，不挂载 Docker Socket。
- 解析子进程：生产要求非 root Linux、Landlock ABI >=3 与 libseccomp；在读取文档前安装只读文件范围及默认拒绝系统调用策略。Worker 自检失败不消费任务。目标环境必须先通过 `make test-parser-linux`，不允许降级到开发解析模式；边界和仍未完成的验收见 [文档解析](document-parsing.md)。
- Sandbox Service：独占一台执行宿主机及其 Docker daemon，通过双向 TLS 和服务令牌接受内部控制请求。不得与 API、Worker、数据库或密钥管理服务共用宿主机。
- 沙箱容器：非 root、只读根文件系统、全部 capability 移除、no-new-privileges、CPU/内存/PID/超时限制、默认完全断网；工作目录使用限容 tmpfs volume，不挂载宿主项目目录。

每台沙箱宿主机运行一个控制服务实例，其 SQLite 状态库保存在独立持久目录。当前远程客户端对应一个沙箱服务地址；跨宿主机调度、主机故障自动迁移和控制服务多副本不在这个适配器的已验证范围内。不能把多个独立宿主机随意放在无粘性的负载均衡后面。

## 配置生产 API / Worker

两类进程均设置：

```dotenv
DEEPAGENT_ENVIRONMENT=production
DEEPAGENT_AUTO_MIGRATE=false
DEEPAGENT_ALLOW_DEMO_IDENTITY=false
DEEPAGENT_SESSION_COOKIE_SECURE=true
DEEPAGENT_CSRF_ENABLED=true
DEEPAGENT_CORS_ORIGINS=https://console.example.com
DEEPAGENT_ALLOWED_HOSTS=console.example.com
DATABASE_URL_FILE=/run/secrets/database-url
DEEPAGENT_BOOTSTRAP_ADMIN_PASSWORD_FILE=/run/secrets/bootstrap-password
OPENAI_API_KEY_FILE=/run/secrets/model-api-key
DEEPAGENT_MODEL_ALLOWED_ORIGINS=https://model.internal.example.com
KNOWLEDGE_OBJECT_STORE=oss
KNOWLEDGE_EMBEDDING_PROVIDER=openai_compatible
KNOWLEDGE_EMBEDDING_API_KEY_FILE=/run/secrets/embedding-api-key
KNOWLEDGE_EMBEDDING_ALLOWED_ORIGINS=https://embedding.internal.example.com
DEEPAGENT_SANDBOX_PROVIDER=remote
DEEPAGENT_SANDBOX_SERVICE_URL=https://sandbox.internal:8443
DEEPAGENT_SANDBOX_SERVICE_TOKEN_FILE=/run/secrets/sandbox-token
DEEPAGENT_SANDBOX_CA_FILE=/run/tls/sandbox-ca.pem
DEEPAGENT_SANDBOX_CLIENT_CERT_FILE=/run/tls/controller.pem
DEEPAGENT_SANDBOX_CLIENT_KEY_FILE=/run/tls/controller-key.pem
DEEPAGENT_CONTENT_SCANNER=clamav
CLAMAV_HOST=clamav.internal
```

分别设置 `DEEPAGENT_PROCESS_ROLE=api` 和 `worker`。模型、Embedding 与 OSS 的其余必填配置见 `.env.example`。生产环境会拒绝 SQLite、`all` 进程模式、自动迁移、内嵌密钥、默认管理员密码、本地对象存储、Hash Embedding、直接 Docker Provider 和未配置扫描器。

发布前由独立迁移任务运行 `make migrate`。API / Worker 使用最低权限数据库账户；迁移账户单独持有 DDL 权限。

迁移入口同时更新平台表和 LangGraph Checkpoint Schema。生产启动只检查版本，不执行 Checkpoint DDL。当前平台 Schema 为版本 17，包含资源权限、统一计量、不可变模型绑定、独立生产发布/路由审批及未完成任务容量索引。旧生产路由保留为 LEGACY，完成独立审核前拒绝自动路由；升级窗口与兼容性见 [生产路由治理](production-routing.md)。执行一致性边界见 [runtime-reliability.md](runtime-reliability.md)，模型及旧数据升级契约见 [model-governance.md](model-governance.md)、[model-budget.md](model-budget.md)、[resource-access.md](resource-access.md)、[production-releases.md](production-releases.md) 和 [健康与容量](operational-readiness.md)。

模型和 Embedding 的生产地址必须使用 HTTPS，且来源必须在对应的 `*_ALLOWED_ORIGINS` 中。配置项使用逗号分隔的完整来源（scheme/host/port，不包含 `/v1` 等 API 路径）；示例域名需要替换成真实受批准地址。客户端禁止所有重定向，包括同源重定向；普通对话、原生 Coding 模型和 Embedding 使用同一规则。原生 SDK 自动重试已关闭，后续重试必须经过计量和幂等策略。客户端不隐式读取代理环境变量。

## 文件型密钥

`NAME_FILE` 优先于 `NAME`。生产读取器要求文件为普通文件、最大 64 KiB、仅文件所有者可访问（例如 `0400` 或 `0600`）；进程运行 UID 必须能够读取。生产不允许通过 `NAME` 直接注入数据库连接串、管理员初始密码、模型 API Key、Embedding API Key 或沙箱服务令牌。

这些文件应由组织的 Secret Manager / Vault / Secrets Store CSI 等外部系统挂载，不要写入 Git、镜像、Deployment 的明文环境变量或应用数据库。当前模型配置在进程启动时读取；密钥轮换后应滚动重启 API / Worker。OSS 使用云厂商默认凭证链，生产应采用工作负载身份/RAM Role/OIDC，不设置长期 AccessKey。

运行上下文不再创建虚假的临时凭证句柄。模型凭证仅保留在网关内存中，不传给 Agent、Prompt、Checkpoint 或沙箱。

## 启动独立 Sandbox Service

执行宿主机预装与发布计划摘要一致的 Coding Runtime 镜像；服务不自动构建镜像。配置：

```dotenv
DEEPAGENT_CODING_IMAGE=deepagent/coding-runtime:0.1.0
DEEPAGENT_SANDBOX_SERVICE_TOKEN_FILE=/run/secrets/sandbox-token
SANDBOX_STATE_PATH=/var/lib/deepagent/sandboxes.db
SANDBOX_LEASE_DATABASE_URL_FILE=/run/secrets/sandbox-lease-database-url
SANDBOX_TLS_CERT_FILE=/run/tls/sandbox.pem
SANDBOX_TLS_KEY_FILE=/run/tls/sandbox-key.pem
SANDBOX_TLS_CLIENT_CA_FILE=/run/tls/controller-ca.pem
SANDBOX_MAX_INSTANCES=32
SANDBOX_MAX_CPUS=4
SANDBOX_MAX_MEMORY_MB=8192
SANDBOX_MAX_DISK_MB=10240
SANDBOX_MAX_PIDS=256
SANDBOX_MAX_TTL_SECONDS=86400
SANDBOX_MAX_ARCHIVE_BYTES=104857600
```

使用 `make sandbox-service` 启动，入口强制校验客户端证书。禁止使用未启用 TLS 的通用 ASGI 启动方式对外暴露此服务。只允许 API / Worker 节点访问 8443；Docker Socket 只授予该服务，不通过网络公开。

租约数据库凭证只交给 Sandbox Service，不交给沙箱容器。该独立 PostgreSQL 登录角色必须是 `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION`，仅授予目标 Schema 的 `USAGE` 和 `sandbox_execution_leases`、`sandbox_cancellation_leases` 两个视图的 `SELECT`，不得授予平台基础表的读写权限。连接串必须包含 `sslmode=verify-full` 并配置受信任 CA；服务初始化会拒绝过权账户，查询会话强制只读。角色密码由密钥管理系统配置，不写入部署文档或命令历史。Schema 18 取消收尾的权限与升级顺序见 [取消收尾契约](cancellation-finalization.md)。

执行和上传必须携带当前 Run Attempt 的租约。服务在准入和执行期间查询授权视图，租约失效后终止旧进程，并等待实际 IO 排空后才允许新所有者进入。迟到的旧 Attempt 取消不得中断新 Attempt。只读检查和平台清理仍受服务令牌与 mTLS 保护，不允许匿名或无租约的任意命令。

服务对创建请求实行幂等校验：请求 ID、租户/项目/会话哈希、源码摘要和策略摘要必须一致。服务端重新校验镜像、用户、资源和网络策略，对源码归档拒绝路径穿越、Git 元数据、链接、设备文件、重复路径及解压超限。容器创建使用已验证的镜像 ID，避免标签被并发替换。

当前只支持完全断网。`allowlist` 不会被降级为任意外网访问，而是明确拒绝；需要出网的场景须增加独立、可审计的出口代理适配器后再启用。

tmpfs 工作目录在容器意外停止后不视为可恢复实例；Coding 平台创建新实例，从原始源码基线及已发布的图/文件配对恢复点恢复。主动中断会冻结并保存工作目录、终止全部容器进程，再恢复目录。配对归档覆盖工作区、产物和临时工具文件，新恢复接口也强制验证当前执行租约。文件传输、响应和归档均有大小限制。

## 内容扫描与对象不可变性

知识文件完成上传时扫描，Worker 在解析前再次扫描实际读取且通过 SHA-256 校验的内容；未完成摄取或被拒绝的版本不能下载。源码快照和沙箱快照在落盘/使用前也执行扫描。

OSS Bucket 必须开启版本控制，完成上传后固定 Object Version ID，避免已经扫描的对象被有效期内的上传 URL 替换。本地对象适配器禁止用不同内容覆盖相同对象键。

生产源码和工作区快照也使用同一个共享对象存储，对象键包含租户/项目哈希和内容摘要，数据库固定具体 Object Version，读取时重新校验租户范围、大小和 SHA-256。API 与 Worker 不需要共享本机快照目录；快照当前统一限制为 100 MiB。

既有本地快照不会被隐式上传。切换生产前应停止 API/Worker 并处理所有非终态 Run，配置目标 OSS 与扫描器，然后运行：

```bash
.venv/bin/python3.13 -m packages.persistence.migrate_archives --legacy-data-root /absolute/path/to/old/data
.venv/bin/python3.13 -m packages.persistence.migrate_archives --legacy-data-root /absolute/path/to/old/data --apply
```

第一条只验证原文件，第二条扫描并上传后更新引用；支持失败后重复运行，不删除原文件。切换到共享存储后会拒绝尚未迁移的本地快照。上述迁移命令尚未对用户现有项目数据执行。

ClamAV 使用 `INSTREAM` 协议；超时、不可用或异常响应均按失败处理，不能绕过。`StreamMaxLength` 应不小于 `CLAMAV_MAX_BYTES`，同时配置 `MaxScanSize`、`MaxFileSize`、`MaxRecursion`、`MaxFiles` 和 `AlertExceedsMax`，定期更新病毒库。ClamAV TCP 协议本身不提供认证或加密，只能使用 Unix Socket 或受网络策略保护的私有连接。[ClamAV 协议说明](https://docs.clamav.net/manual/Usage/ClamdProtocol.html)

## 验证范围

已加入自动化测试：远程生命周期/文件传输/策略与源码摘要、拒绝重定向、文件密钥权限、扫描协议及失败关闭、服务端资源策略、归档安全、幂等创建、拒绝内容不能入库或下载。

真实 Docker tmpfs 容量、主动中断恢复、进程逃逸防护、mTLS 证书轮换与宿主机故障恢复仍需在目标 Linux 执行节点完成集成/安全验收。此前真实 Docker 测试暴露过临时卷恢复与取消链路问题，正在处理；不能把 Mock/Fake 或非 Docker 回归结果当作这些项目已验收。
