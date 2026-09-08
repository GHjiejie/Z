# 生产交付与严格验收

本文件描述交付代码，不是已上线证明。当前分支包含未提交改动；不应发布 `uncommitted` 镜像。

## 依赖与镜像

`uv.lock` 是唯一 Python 解析结果，使用 uv 0.11.16 生成。四份导出均包含版本和分发文件 SHA-256：

| 文件 | 范围 |
| --- | --- |
| `requirements.prod.lock` | 应用运行依赖 |
| `requirements.txt` | 应用及测试依赖 |
| `requirements.build.lock` | 固定 pip/setuptools/wheel 构建工具 |
| `requirements.tools.lock` | 锁文件检查及交付工具 |

依赖升级后必须重新生成四份导出并运行 `make release-locks`。`alibabacloud-tea==0.4.3` 只有源码分发，是唯一允许源码构建的例外；先安装带哈希的构建工具，再以 `--no-build-isolation` 构建，拒绝其他依赖隐式回退源码。运行镜像只离线安装构建阶段生成并复验清单的 wheels。清单证明完整性，不替代可信 CI 或签名。

`docker/platform/Dockerfile` 固定 Python/Node 镜像摘要及有签名校验的 Debian 快照仓库，包含 `api`、`worker`、`migrate`、`sandbox-service` 四个运行目标及独立 `acceptance` 测试目标。构建上下文限制在本项目；`.env`、数据库、业务数据、私钥、父目录和 node_modules 不进入镜像。应用及 Python 运行库为只读，容器运行 UID/GID 10001；入口拒绝 root、可写根目录、附加 capability 和平台进程的 Docker socket。

```bash
make release-tools
make release-locks
make release-images REVISION=<完整提交SHA>
make release-scan REVISION=<同一提交SHA>
```

构建不会推送镜像、启动业务服务或执行迁移。扫描要求 HEAD、提交参数及干净工作区一致；源码只从 Git 归档获取，不扫描本机忽略的 `.env` 或运行数据。镜像扫描从导出的归档读取，不向扫描器挂载 Docker socket。HIGH/CRITICAL 漏洞或秘密发现会阻止放行，输出各镜像 SBOM 和元数据。镜像 source label 只是绑定检查，不能当作签名来源证明；生产 registry 摘要、签名及发布授权仍需在制品提升流程中验证。

## 自动化入口

仓库根目录 `.github/workflows/deepagent-release.yml` 使用临时 Ubuntu runner、只读仓库权限及固定 SHA 的 Actions；不使用 `pull_request_target`、部署密钥或持久宿主机执行外部贡献代码，也不自动发布。

流水线依次检查锁文件、前端测试/构建、SQLite/真实 PostgreSQL、真实 Docker、监控告警触发/恢复、生产镜像及容器内 Linux 解析，最后扫描并输出 SBOM。告警工具使用官方 Prometheus 3.13.2，下载后先验证固定 SHA-256，不启动监控服务。严格测试入口：

```bash
# 仅允许独立测试数据库，测试会创建并删除自己的随机 schema。
DEEPAGENT_TEST_POSTGRES_URL=<独立测试数据库> make test-integration
DEEPAGENT_TEST_POSTGRES_URL=<独立测试数据库> make test-docker
```

普通 `make test` 仍允许开发环境跳过无 Docker/无 PostgreSQL 的测试；不得用它替代发布门禁。严格入口拒绝缺失 PostgreSQL；Docker 用例集合缺失或任何跳过均失败。平台组仅允许三项明确只适用于 PostgreSQL 的测试在 SQLite 参数下跳过。原生解析只在实际运行镜像内单独验收，Linux 内核策略不能降级或以 mock 成绩替代。

原生镜像验收还包含仓库网络用例：真实 Git、本机 TLS/临时 CA、跨站连接拦截及子进程排空。API 可选的公共 CA/SSH 主机公钥 overlay 及 `config --repository-trust` 预检见[仓库网络边界](repository-network.md)。公共信任材料不是客户端凭据，不允许覆盖代码或向其他角色扩大挂载。

完整平台组新增真实 PostgreSQL 备份/恢复检查，测试账号需 CREATEDB，并要求可用的 `pg_dump` / `pg_restore`。CI 在本组之前从官方、固定 SHA-256 的 PostgreSQL 17.11 源码构建客户端到临时目录；不会使用 Ubuntu 24.04 runner 预装的 16 系列去备份 17 系列测试服务。构建保留 OpenSSL，原生工具缺失不计为通过。备份包、隔离库与凭据边界见 [灾备说明](disaster-recovery.md)。当前四类业务镜像不包含运维原生客户端，恢复工具需独立准备的运维环境。

## 三类独立部署配置

- `deploy/platform.compose.yaml`：仅 API/Worker，禁止 Docker socket，不引用迁移账号。
- `deploy/migration.compose.yaml`：独立迁移任务，仅挂载 DDL 账号，不能与运行账号混用。
- `deploy/sandbox.compose.yaml`：专用执行宿主机上的 mTLS 控制服务；Docker socket 是宿主机级高权限接口，即使挂载 `:ro` 也不是只读 API，不得与 API/数据库同机。

配置文件均不会提供可直接运行的默认生产镜像。镜像变量应来自已批准制品的 registry digest；示例模型、域名、bucket、证书和凭据必须由实际环境提供。环境文件只放非秘密配置，秘密通过文件引用。

部署前，在对应 Linux 宿主机导出 Compose 所需变量后执行：

```bash
python scripts/release.py config --kind platform
python scripts/release.py config --kind migration
python scripts/release.py config --kind sandbox
```

这是只读预检：通过真实 Compose 解析检查镜像 digest、角色隔离、只读根、权限、容量和秘密挂载；不显示解析后的环境值，不启动服务、不迁移数据库。它不会加载项目开发 `.env`。文件型秘密需要在宿主机上由 UID 10001 所有、owner 可读、group/other 无权限（如 0400/0600）；预检只检查文件元数据。Compose 对文件型 secrets 不会替你调整 uid/gid/mode，详见 [Docker 官方说明](https://docs.docker.com/reference/compose-file/services/#long-syntax-2)。文件供应与轮换由部署平台负责；不要为通过检查放宽文件权限。

预检不验证镜像是否已获业务批准，也不代替外部依赖联通、TLS、租户角色、运行数据库账号权限、反向代理就绪摘流及恢复演练。API 的 `/livez` 仅用于存活检测；入口/负载均衡须使用 `/readyz` 判断是否接收流量。

## Worker 监督与关闭

独立 Worker 监督运行/摄取消费、恢复循环、数据库心跳、沙箱回收和健康采样八项永久任务；配置指标监听器时额外监督该任务，生产共九项。任何任务非预期返回、异常或取消，主进程清理资源后以非零状态结束，由部署平台重启。生产日志采用安全 JSON 格式，不输出任意异常原文或日志消息；通过固定事件、异常类型及代码位置诊断。

SIGTERM/SIGINT 仍正常关闭。清理过程中某个任务已经失败，不会阻止其他任务取消及 Worker 下线登记。该机制检测任务结束，不声称覆盖事件循环完全卡死；宿主机资源/进程监控及外部存活策略仍需补齐。

生产 API/Worker 现在强制要求文件型指标/OTLP 凭据、批准的 HTTPS 采集器和追踪队列丢弃观测开关；Worker 另需管理端 TLS 证书。Schema 19 新增持久任务关联表，需先执行独立迁移并授予运行账号必要权限。升级及采集契约见 [可观测性](observability.md)。这些配置不自动部署采集平台或发送告警。

## 本轮真实 Docker 发现与修复

Docker 29.4.0/OrbStack 的归档接口对 HostConfig tmpfs 返回空目录，而容器内实际存在文件。`/tmp`、`/skills`、`/artifacts` 改为带固定容量与非 root 权限的命名内存卷，保持暂停后原子捕获；中断同时保留临时文件。真实回归已覆盖二进制产物、临时文件、删除状态、Git 基线和工作区容量。

旧 HostConfig tmpfs 实例被拒绝生成配对恢复证据，防止把空归档认作成功。不要直接重启现有实例期待临时数据保留；在维护窗口按实际可验证恢复点替换。此批没有修改或重启用户原有容器。

ChangeSet 的临时 Git index/object 目录和指向只读 Git 基线的链接在计算后清理，避免混入恢复包；归档仍拒绝用户链接和 Git 元数据。Docker hijack 连接按 HTTP response→socket 顺序关闭，避免关闭后析构告警。

## 未完成的发布条件

- 完整生产镜像构建在 Docker Hub 认证连接超时处失败，尚未运行其原生 Linux Landlock/seccomp 验收；没有替换为浮动镜像或禁用验证。
- CI 文件尚未提交/推送，不能声称远端流水线已成功；签名制品提升和实际部署尚未验收。
- 真实 Docker 取消标准产物缺失已由 [Schema 18 独立收尾](cancellation-finalization.md) 修复，本机严格 Docker 门禁 6 项全部通过；旧执行租约仍不能写入。生产仍须验收远程收尾组合链路，并按该文档执行维护窗口升级和取消权限视图授权；本机测试不是生产升级完成证明。
- 完整 mTLS/OSS/扫描链路、备份/实际告警送达/数据治理、目标宿主机容量与灾难演练仍未完成。监控代码及规则验证已加入，但不是上述真实环境验收的替代品。
- 加密一致快照及隔离恢复已有可执行工具与本机真实数据库验证；异地存储/调度、生产最小权限/TLS/真实 OSS、审核激活及业务级 RPO/RTO 尚未验收。生产镜像另补齐了此前遗漏的 `apps/logging.json` COPY 和交付测试；完整镜像仍需实际构建复验。

各批次实际测试结果以 [企业化实施状态](enterprise-hardening-status.md) 为准。
