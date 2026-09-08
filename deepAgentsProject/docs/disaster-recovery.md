# 加密备份与隔离恢复

本批提供可实际执行的 PostgreSQL 与固定版本对象备份、离线完整性检查、隔离数据库/对象恢复工具。它不是已经部署的异地备份服务，也不会自动切换生产流量或重跑历史队列。

## 备份范围与一致性

命令入口是 `python -m packages.operations.disaster_recovery`，仅供运维使用，没有通过业务 API 暴露。支持 `backup`、`verify`、`restore`。

备份在同一个 PostgreSQL `REPEATABLE READ READ ONLY` 事务中读取业务、Checkpoint 和对象来源清单，并将 `pg_export_snapshot()` 的结果交给真实 `pg_dump`。备份期间共享锁阻止普通 Schema/Checkpoint 迁移；若迁移已持锁则立即拒绝备份重试，不带着旧快照阻塞等待并发建索引。备份不修改源库业务状态。源库并发提交在快照之后发生的修改不会混入备份，普通业务写入仍可继续。

备份包含：

- 整个专用数据库的 custom-format dump，包括用户/权限、持久队列、审计/账本、Run/Attempt、Checkpoint blob 与 pending writes。
- 全部已提交知识文档的固定 Object Version，以及源码/工作区快照的固定版本对象。每份内容验证已知 SHA-256 和大小，相同内容在备份包内去重。
- 加密的清单：数据库版本、Schema 版本、快照时间、逐表行数/内容摘要、对象来源版本与每份文件的 SHA-256/大小。

未完成且没有固定对象版本的上传只保留其数据库状态，不把“当前 latest”当成已提交对象。已提交对象缺失、损坏、没有版本来源或尚未迁移的本地归档会阻止备份成功发布。未通过安全扫描的内容不会因此取得可下载/可执行资格；恢复保留原有状态，备份工具只复制不解释对象字节。

当前支持专用 PostgreSQL 数据库、`public` 应用 Schema、当前应用/Checkpoint Schema，以及固定版本对象存储；不支持将开发 SQLite 的两个独立文件热拷贝成生产备份，也不支持跨 PostgreSQL 主版本升级。自定义 Schema、时间点恢复/WAL、超过单包限制的大规模备份还需继续实现，不能用本工具替代它们。

## 准备独立运维环境

1. 使用单独、可信的恢复宿主机或隔离恢复集群。备份源库只授予必要读取权限；生产恢复账号应是非超级用户并有新建数据库及所需可信扩展的权限。运行中的 API/Worker 不获得这些凭据。
2. 准备源库/恢复集群两个私有连接文件，显式指定 host、user、dbname。生产必须使用 `sslmode=verify-full` 和受信 CA；不接受 `service`、`options` 或环境级 `PG*` 覆盖。恢复集群连接文件指向维护库，不是要覆盖的业务库。
3. 准备源对象存储只读身份和另一恢复 bucket 的读写身份。恢复 bucket 必须不同于源 bucket，开启版本控制，建议使用独立账号/故障域及专用 IAM。配置文件结构见 `deploy/recovery.storage.example.json`，不包含凭据；凭据由已批准的工作负载身份供应。只按数据库记录读取固定版本，不遍历或清理整个 bucket。
4. 从组织密钥管理流程供应独立的 **32 字节二进制加密密钥**。密钥文件、数据库连接文件必须为当前执行账号所有、普通文件、0400/0600，无 group/other 权限。密钥不能与备份包放在同一失效域；本工具不自动生成或备份企业主密钥，也不打印它。
5. 准备当前账号所有、0700 的输出和临时目录。**生产临时目录必须位于加密且有磁盘配额的卷上**，因为原生 dump、校验中的明文和数据库工具配置会短暂落盘。正常退出/失败会清理本次临时目录；SIGKILL/宿主机故障后须按记录核对并清理本次遗留，不能递归清空共享临时目录。工具不证明底层磁盘加密已启用。
6. 安装同主版本的 `pg_dump` / `pg_restore`、项目哈希锁定 Python 依赖。当前本机使用 PostgreSQL 17 验证。工具不打包数据库集群角色密码、外部密钥、模型配置文件、证书、镜像或第三方账单；它们须由独立的配置/密钥/制品灾备流程提供。

恢复 dump 会执行其中的数据库定义；加密认证只证明包由持有密钥的人产生，不证明源 SQL 无恶意。只接受可信来源并使用隔离集群，不以超级用户在共享生产集群恢复未知备份。参考 [PostgreSQL 的恢复安全说明](https://www.postgresql.org/docs/17/app-pgrestore.html)。

## 操作

下列是运维模板，不会被项目启动流程自动执行。路径和存储配置必须由实际环境供应，命令参数不携带数据库密码。

```bash
python -m packages.operations.disaster_recovery backup \
  --database-file /run/secrets/backup-source-dsn \
  --storage-config /etc/deepagent/backup-source-storage.json \
  --key-file /run/secrets/recovery-key \
  --postgres-bin /opt/postgresql/bin \
  --scratch /secure/recovery-scratch \
  --bundle /secure/backups/approved-unique-name.dagbackup

python -m packages.operations.disaster_recovery verify \
  --key-file /run/secrets/recovery-key \
  --scratch /secure/recovery-scratch \
  --bundle /secure/backups/approved-unique-name.dagbackup

python -m packages.operations.disaster_recovery restore \
  --database-file /run/secrets/recovery-maintenance-dsn \
  --storage-config /etc/deepagent/recovery-target-storage.json \
  --key-file /run/secrets/recovery-key \
  --postgres-bin /opt/postgresql/bin \
  --scratch /secure/recovery-scratch \
  --bundle /secure/backups/approved-unique-name.dagbackup
```

`--development-loopback` 只供独立本机 PostgreSQL 测试，且拒绝非 loopback 地址，不能拿来连接远程生产库。

输出只包含备份 ID、时间、数量、文件摘要或本次新建的恢复库名；不包含 DSN、Token、对象地址、SQL 内容或原生错误全文。CLI 返回非零即未通过验收，不能因文件存在就判定成功。

## 加密和失败处理

备份使用 AES-256-GCM、随机 96-bit nonce 和完整 128-bit 认证标签；格式头同样参与认证。密钥与清单不以明文写进备份外层。解密完成认证之后才解析内部文件，更不会提前执行数据库恢复。错误密钥、正文/标签修改、截断、额外字节、缺失文件、重复成员及路径逃逸都被拒绝。

输出以原子、不覆盖方式发布，已经存在的文件或链接不会被替换。发生发布结果不明确时先 `verify`，不要自动覆盖重试。临时明文归档、解密后的原生 dump 和每份恢复对象都留在私有临时目录；认证失败的明文不会用于后续操作。实现依据 [cryptography GCM 文档](https://cryptography.io/en/latest/hazmat/primitives/symmetric-encryption/)。

当前单包上限 32 GiB，单对象 100 MiB，清单上限 64 MiB，最多 100,000 个文件。原生子进程有文件大小限制和 30 分钟操作超时，SQL 有语句/锁等待超时；总任务仍受数据量、对象存储重试和运维调度影响，这些数值不是经过容量验收的 RTO。到达限制必须失败，不静默跳过数据。

## 恢复为什么保持隔离

恢复先验证整个加密包和文件摘要，随后仅创建随机命名的 `deepagent_restore_*` 数据库。工具不接受已有目标库名，不执行 `--clean`、DROP 或强制终止其他连接。新库创建时禁止连接；设置隔离标记、撤销 PUBLIC 数据库权限并设置默认只读后才允许恢复工具连接。

原生恢复以单事务执行。业务与 Checkpoint 的逐表行数及内容摘要必须与备份完全一致；固定对象清单还必须与恢复后的数据库引用逐项匹配。随后将对象写入不同 bucket，读取刚写入的固定版本再次校验，并在事务中更新数据库的存储引用；不会把目标 bucket 的 latest 猜成原版本。知识索引/Plan 的内容摘要不因存储地址迁移而重写。

成功后状态仍为 `QUARANTINED`，默认只读。API 与 Worker 装配入口均拒绝带此数据库标记的恢复库，避免旧会话、审批、队列、执行租约和未结算调用在恢复后直接复活。原始记录保留以供核对，不能将工具恢复成功等同于业务已经可用。

对象复制或校验失败时，不删除已创建的恢复库或已写入对象版本；返回可定位的隔离库名，供运维核查。源库、源对象与已存在数据库均不修改。重试会创建另一隔离库，遗留资产的审核、清理和配额需纳入运维流程，不能无限重试。

## 接管生产前仍需完成

- 确认原生产写入者和沙箱执行已隔离，避免双活；核对沙箱代次、在途外部操作、供应商费用及待审批任务。
- 设计并实现受审批的激活操作：旧会话/工作租约失效、任务逐类恢复/终止和必要的外部对账。目前没有自动解除隔离或切流入口，不应手工删标记来跳过上述检查。
- 在真实独立对象存储、数据库 TLS 和最小权限账号上完成演练，验证恢复后业务读取、权限、审批及新任务执行。
- 配置异地加密备份调度、上传校验、不可变保留、密钥恢复、缺失/过期告警和清理策略。代码库本身不会选择企业存储账号或向外上传当前数据。
- 与业务确认 RPO/RTO，以快照时间、灾难发生时间和实际业务恢复时间计算；命令输出的 `restore_seconds` 仅是隔离恢复耗时，`snapshot_age_seconds` 仅是快照年龄，不能当作已承诺的生产指标。

## 验证和 CI

测试使用专门的临时 PostgreSQL 数据库（测试账号需 CREATEDB），每次只删除测试实际创建/返回的明确库名。对象端采用固定版本的测试存储，不连接真实 OSS；测试覆盖源库并发更新、真实 pg_dump/pg_restore、Checkpoint/blob/pending writes、对象迁移、知识索引摘要、隔离启动拒绝和失败保留。

```bash
DEEPAGENT_TEST_POSTGRES_URL=<独立测试数据库> python -m pytest -q tests/test_disaster_recovery.py
```

完整平台发布门禁包含这些测试，缺少 PostgreSQL 或真实原生工具时不能跳过后获得绿色放行。CI 使用 `scripts/build_postgres_clients.py` 从 PostgreSQL 17.11 官方源码构建客户端，固定 SHA-256、保留 TLS 支持，不安装服务器或修改系统包。构建宿主机需 C 编译器、make、Perl、Bison/Flex、zlib 和 OpenSSL 开发库。客户端安装在新目录；macOS 若 OpenSSL 不在默认搜索路径，可显式传入批准的 CPPFLAGS/LDFLAGS。实际批次成绩见 [实施状态](enterprise-hardening-status.md)。

一致快照备份语义参考 [PostgreSQL pg_dump 文档](https://www.postgresql.org/docs/17/app-pgdump.html)。
