# 企业化改造实施状态

分支：`codex/enterprise-hardening`。本次按用户要求保存阶段性代码；不要把“已经提交”或“单元测试通过”理解为“已经完成生产上线验收”。下文各批次的“未提交”等描述为当时的历史状态。

## 阶段性提交检查

- 保存现有企业化改造代码、前端、测试、部署配置及项目 CI；本次不推送、不迁移业务数据库、不部署或重启服务。
- 最新快照准备代码已增加 Schema 21（`repository-object-materializations`），引入事务外准备、对象写入记录和提交时授权复核；该批次仍需完善专项回归及灾备衔接，不能视为完成验收。
- 提交前仓库网络、共享归档、Coding 与意图路由专项：80 passed、2 deselected；本次没有运行真实 Docker 或 PostgreSQL 全量门禁。
- 前端：8 项测试通过，类型检查及生产构建通过；主包约 618 KB，仍有超过 500 KB 的构建告警。
- 账号与持久化测试在达到 3 项失败后停止：5 passed、3 failed。失败位置为 `tests/test_auth.py` 的迁移清单/旧库升级断言，以及 `tests/test_persistence.py` 的 SQLite 迁移断言；代码已到 Schema 21，断言仍只包含 Schema 20。此次保留当前代码状态，未为提交而修改测试断言；后续须补充 Schema 21 验证并重新执行完整回归。

## 已加入代码

- 仓库出站边界批次（不增加 Schema）：每次 checkout 重新校验全部 DNS 地址；生产精确 origin 许可；HTTPS 数字 IP 固定的单操作 CONNECT 网关、端到端 TLS、重定向与备用对象跨站拒绝；SSH 固定 IP/主机密钥且不加载宿主凭据。Git 后续探测/归档禁网，修复远程目录误判和本地 Git metadata 越界；克隆期限/输出、进程组实际终止和隧道排空。增加公共信任 overlay、只读部署预检及原生镜像网络测试入口。见 [repository-network.md](repository-network.md)。快照撤权、事务内外部 IO、对象去重/回收及真实生产验收仍未完成；本批未迁移业务库。

- 管理写入撤权一致性批次（不增加 Schema）：计费价格/配额/人工对账、模型注册/启停、评测集/策略/结果、Agent 创建/草稿/发布、ChangeSet 审核、非生产路由更新接入事务内主体复核与账号锁。保护审计原子性、用户配额目标范围和写入期间会话/密码到期，新增 Agent/非生产路由治理审计。编译保持事务外，发布前重新授权；生产路由仍需独立审批。见 [management-write-consistency.md](management-write-consistency.md)。仓库/外部 IO 等剩余入口和真实生产验收仍需继续，本批未迁移业务库。

- Schema 20：上传意图/逻辑保留字节额度、到期处理、全局及租户/项目/用户摄取领取上限、单 API HEAD 并发上限、OSS 精确 Content-Length 签名及浏览器头处理。API 不再下载/扫描完整对象；Worker 持久领取后校验、扫描和索引，202 仅表示接受校验。原生线程取消保留所有权，下载/扫描增加总时限及连接清理。见 [upload-governance.md](upload-governance.md)。逻辑字节不是全部 OSS 版本的物理账单；到期不删除对象、不返还保留字节；对象版本核对/回收、真实 OSS/浏览器链路和生产容量仍待验收。新旧进程需统一升级，本批未迁移业务库。

- 生产交付批次：新增四类角色镜像及测试镜像、uv 锁文件/四份哈希导出、wheel 构建/复验、根目录 CI 工作流、严格 PostgreSQL/Docker/原生解析门禁、只读部署预检。修复 Compose 临时挂载被拆分，并将迁移单独配置；检查真实秘密文件权限和镜像 digest。Worker 监督八项业务/健康任务，配置指标监听器后额外监督该任务，生产共九项；异常结束后清理并失败退出，不会为了恢复消费而重复启动仍在运行的任务。见 [production-delivery.md](production-delivery.md)。此批交付代码尚未提交/推送，镜像构建和远端 CI 未验收。
- 本机 Docker 恢复可用后的真实验收发现：归档接口漏读 HostConfig tmpfs 内容；已将临时/技能/产物目录改为有限容命名内存卷，保留暂停原子捕获，中断同时保留临时文件，旧挂载拒绝生成不完整恢复证据。ChangeSet 清理自己的临时 Git 链接与对象，归档继续拒绝任意用户链接。随后复现的取消标准产物缺失已在 Schema 18 批次修复，旧执行租约仍被强制拒绝。
- Schema 18：取消采用独立持久收尾记录、专用租约/权限视图和工作区代次保护；支持进程死亡接管、续租、退避重试及原子发布快照/五类标准产物/终止恢复标记。取消后验证结果为 PARTIAL、差异需审核；新 Run 恢复文件但不重放旧图。真实 Docker 已验证中断、文本/二进制补丁及实际 git apply。见 [cancellation-finalization.md](cancellation-finalization.md)。尚未迁移业务数据库；生产须维护窗口统一升级 API/Worker/Sandbox Service，并增加取消视图只读授权。
- Schema 19：安全 JSON 日志、服务端 Request/Trace ID、Run/摄取来源同事务持久化与跨进程关联、HTTP/模型/沙箱/知识子操作 Trace、独立凭据指标端点、Worker TLS 管理监听及监督、显式 HTTPS OTLP 导出、有界队列丢弃指标。新增 8 条告警及真实 promtool 触发/恢复检查，生产交付配置和 CI 门禁同步更新。前端错误可显示安全 Request ID。见 [observability.md](observability.md)。未迁移业务数据库，采集平台/仪表盘/实际通知送达/远程沙箱 Trace 传播及防篡改审计仍未完成；关联 ID 和计数器不替代持久账本。
- 灾备批次（不新增 Schema）：AES-256-GCM 加密包、PostgreSQL/Checkpoint 同一导出快照、固定对象版本复制与逐表/逐文件摘要、认证后才能解包、仅新建隔离恢复库、不同 bucket 固定版本回读及引用更新、API/Worker 拒绝隔离库启动。失败不删除恢复库/对象，不改源库。新增独立运维 CLI、生产最小权限/TLS 要求及官方哈希校验 PostgreSQL 17.11 客户端构建；修复生产镜像漏复制日志配置。见 [disaster-recovery.md](disaster-recovery.md)。这不是生产接管完成：异地备份调度、审核激活、真实 OSS/最小权限/TLS 链路与 RPO/RTO 演练仍未完成。

- 知识库创建/上传准备将业务记录、事件和幂等标识同事务提交；新增受主体/完整内容/范围约束的 `Idempotency-Key`，独立连接竞争使用事务锁。重试按当前权限重新签发待上传对象，不存储签名地址，已提交摄取的对象不重新授权上传。控制台保留当前标签页的失败请求键，刷新后可重试；新增前端测试入口。见 [knowledge-write-consistency.md](knowledge-write-consistency.md)。本批不增加 Schema。

- 统一 Permission/RBAC、生产安全配置检查、CSRF/CORS/Host/请求大小/安全响应头、受签名保护的可信身份头。
- PostgreSQL 连接池、SQLite/PostgreSQL 事务、独立迁移入口、PostgreSQL LangGraph Checkpoint。
- API/Worker 进程角色、PostgreSQL 持久队列、去重与 Lease、Worker 数据库心跳。
- 远程 Sandbox Provider 与独立 mTLS Sandbox Service、服务端策略和归档校验、限容 tmpfs 工作区。
- 文件型密钥读取、生产拒绝内嵌凭证、ClamAV 扫描、OSS Object Version 固定、本地对象不可覆盖、未完成安全检查的版本禁止下载。
- 启动失败和正常退出均通过 AsyncExitStack 释放数据库、Checkpoint 和 Worker 资源。
- 模型/Embedding 出站源白名单和重定向拒绝；普通与 Coding Run 共享逐调用预占和结算预算，跨 Attempt 不清零。
- 静态页面只提供固定入口，目录外文件与符号链接逃逸被拒绝；当前运行服务已验证原漏洞路径返回 404、正常页面返回 200。
- Agent 草稿强制版本并原子更新，编译期间发生编辑会拒绝发布，Revision/Deployment 并发操作按数据库锁序列化。
- Schema 7 的不可变评测集、实际 Run 证据评分、最新结果门禁、生产新 Run 准入检查和评测治理审计。
- 移除普通执行器中虚构的 SubAgent/待办记录；批准的产物只有成功落库后才能产生完成事件。
- Schema 8 的图/文件配对恢复点、独立 Attempt 图会话、嵌套审批恢复、原始输入与审批票据绑定；已完成模型回答可恢复而不新增模型调用。
- 恢复归档覆盖代码、产物和临时工具文件；保留删除状态及二进制内容，拒绝路径逃逸、Git 元数据改写及损坏记录。修复 PostgreSQL 保存 Git NUL 分隔日志的问题。
- Schema 10/11 的私有 Thread、显式分享、冻结和当前知识权限联查、账号撤权、环境边界、路由归属和 ChangeSet 原子审批；Runs/Threads/路由决定增加权限感知游标分页。
- PostgreSQL 普通迁移采用事务锁串行化，Checkpoint 使用已完成的 try-lock 轮询，避免与并发建索引的快照互锁；有真实 PostgreSQL 双迁移回归。
- Schema 12/13：分类与两类 Embedding 的全入口计量、真实摄取发起人、租户级序列化日/月及并发额度、版本化价格/配额/人工对账 API；普通/Coding 模型按批准 profile 与发布计划绑定，不再将全局网关用于所有已注册模型。
- Schema 14：知识任务/事件继承文档权限，角色撤销后同步隐藏；任务/版本返回白名单，私有文档清单不通过公共 manifest 或计数泄露；查询事件按发起人隔离，事件权限感知游标分页。
- Schema 15：显式环境发布授权、独立生产审批、评测/模型/权限/渠道复核、原子发布与回滚、意图路由版本联动及审计；旧部署 DRAINING 不接受新任务，已有执行可继续完成。提供 Production releases 页面，原生产直部署入口改为申请审核。
- Schema 16：未完成任务部分索引；租户/项目/用户任务与摄取容量、并发原子准入和 429 回滚；新建/重试/补充输入/审批恢复/摄取入口覆盖。新增 `/livez`、缓存单次在途 `/readyz` 与 `/health`，Worker 停止消费、积压、检测超时及过期不能误报健康。
- 修复首次生产路由初始化与发布交错导致指向 DRAINING 部署；Run 和审批的幂等记录绑定请求内容、主体及审批版本；重复完成知识上传同样过滤内部租约字段。契约见 [operational-readiness.md](operational-readiness.md)。
- Schema 17：生产路由更新/回滚的显式授权、独立审批、快照和评测复核、事务审计、发布竞争保护、旧决定失效、明确目标选择；Settings 增加路由申请与审核面板。旧生产路由保留为 LEGACY，重新审核前拒绝自动选路。升级契约见 [production-routing.md](production-routing.md)。
- 账号创建/变更/改密/停用、会话撤销与审计同事务提交；登录失败计数与拒绝审计仍提交。事务内重查当前操作者身份，PostgreSQL 用户锁覆盖登录/改密/撤权竞争，权限或所属范围变化撤销旧会话；用户名唯一冲突统一为领域错误，PATCH 显式 null 被拒绝。见 [account-consistency.md](account-consistency.md)。本批不增加 Schema。
- 一次性子进程文档解析与分块已接入摄取，增加时间、CPU、输入/输出、解压、页数及块数限制、取消回收和返回协议校验。psutil 7.2.2 已安装。资源限制本身不替代操作系统权限隔离；新增内核策略与待验收范围见下一条及 [document-parsing.md](document-parsing.md)。
- Linux 解析新增强制 Landlock/seccomp 策略、非 root/单线程校验、清理非标准句柄、默认拒绝系统调用、每份文档内核拒绝探测和 Worker 启动自检。生产不允许降级，新增 `make test-parser-linux` 严格验收入口。数值 UID 不变，依靠进程安全域限制权限；只读运行库交付及原生 Linux 效果尚未验收，不能标记此生产阶段完成。详见 [document-parsing.md](document-parsing.md)。

隔离边界和部署契约见 [enterprise-isolation.md](enterprise-isolation.md)。

## 当前正在补强：并发执行与故障恢复

已加入原子 Run/Attempt 领取、持续恢复、租约失效写入拦截（包括 LangGraph Checkpoint / Pending Writes）、投递轮次 ACK、Thread 串行准入、跨进程取消、审批/重试/意图路由的事务内投递，以及导入租约和知识库发布互斥。详见 [runtime-reliability.md](runtime-reliability.md)。

已经用独立临时 PostgreSQL 17.10 验证：竞争领取/恢复、并发幂等、事务回滚、旧投递不能 ACK、新旧 Checkpoint 写入隔离、两份文档并发发布；另有真实子进程强制结束后换 Worker 恢复的测试。

生产源码/工作区快照已改用固定 Object Version 的共享对象，不再依赖创建者本机路径；本地开发保留兼容模式。旧快照迁移工具支持只读预检、显式应用、SHA-256/租户范围检查和重复运行，并保留原文件。

远程沙箱服务端执行代次 fencing、操作排空及配对恢复接口已加入代码及契约测试。图/工作区一致恢复点已通过 SQLite/真实 PostgreSQL 的提交前后故障注入、嵌套审批与实际 SIGKILL 子进程测试。本机真实 Docker 取消/临时卷恢复已通过；取消收尾覆盖原子提交、重试幂等、权限丢失和迟到结果拦截。此阶段仍未完成新接口的目标 Linux/真实 mTLS/OSS/扫描组合验收、长期历史压缩回收、完整网络分区及宿主机故障/容量演练。

## 新增：真实评测与发布门禁

评测接口不再返回固定分数，基于冻结用例及实际 Run 的状态、输出、事件、审批、费用和平台验证结果生成不可变记录。生产只接受当前策略所需的最新通过结果，并检查计划、用例集、运行来源和原始样本时效；重复评价旧 Run 不能刷新有效期。开发环境模拟评测可以验证流程，但不能授予生产资格。

自动样本编排、完整多轮/语义评测、评测控制台与企业业务评测集仍待补齐。普通执行器尚不支持真正的 SubAgent 工具调用循环，对此明确报告不可用并阻止其取得生产评测资格，不能将删除模拟事件等同于已经完成该能力。详见 [evaluation-gates.md](evaluation-gates.md)。

## 尚未完成的阶段

1. 日志、请求/运行 Trace、指标与告警规则已经加入并完成本机验证；企业采集平台/仪表盘、告警实际送达与升级演练、防篡改外部审计及远程节点链路覆盖仍待完成。数据库/Worker/队列就绪已实现，外部依赖端到端健康仍待补齐。任务容量和逻辑上传配额已覆盖外部入队/上传，租户公平调度、任务自动排队过期、解析策略的只读镜像交付/原生 Linux 验收及物理对象版本配额/回收未完成。
2. 生产镜像/部署清单、依赖锁定/扫描/SBOM 和 CI 门禁代码已加入，但完整镜像、原生 Linux 安全及远端流水线尚未通过验收；签名制品提升、异地备份与受审批的生产恢复/接管、数据保留及删除治理未完成。已实现的逻辑备份/隔离恢复当前限专用 PostgreSQL public Schema、同主版本、32 GiB 单包；跨版本迁移、自定义 Schema、大规模/WAL 时间点恢复还未提供。
3. 模型/计费/评测及环境授权管理控制台、供应商自动对账与凭据验证、缓存/工具等额外计价维度、真实通用 SubAgent、完整 AI/RAG 评测、企业测试。发布/回滚及生产路由配置变更的基本独立审批已实现，多级审批、定时/灰度发布未实现。准确范围见 [model-governance.md](model-governance.md)、[model-budget.md](model-budget.md)、[production-releases.md](production-releases.md) 和 [production-routing.md](production-routing.md)。
4. PostgreSQL 向量检索适配：当前仍是参考检索实现，不应宣称已完成 pgvector 生产索引。
5. 企业组织/项目成员关系和按企业要求接入的身份治理，完整安全与故障演练、真实环境集成验收、全部文档与配置复核。

## 最近验证记录

- 仓库出站边界批次最终完整平台门禁：1156 passed、3 skipped、8 deselected（519.98 秒），使用独立 PostgreSQL 17.10 与现有 17.11 备份客户端。3 项仅为 SQLite 不适用的 PostgreSQL 专项；8 项为独立 Docker 和原生 Linux。最终真实 Docker 门禁 6 passed、1161 deselected（25.93 秒），没有跳过。
- 最终网络/交付专项 102 passed（8.36 秒），已全部纳入上述全量结果。覆盖真实本机 Git/TLS 完整拉取、归档、探测、错误证书/主机名、重定向、客户端放宽策略时的实际备用对象跨站拒绝、DNS 变化、Git metadata 边界、环境污染、OpenSSH 参数、辅助进程终止和真实 Compose 公共信任 overlay 解析/权限检查。此前综合专项 117 passed、2 deselected（17.60 秒）之后补了一项代理配置支持回归；以最终全量为准。
- 辅助进程回归确实复现了 SIGKILL 发送后连接短暂存活，已改为等待实际终止；部署回归修正了 Compose 将 mode 返回为 `0444` 字符串的处理。首轮全量为补充 Git 配置能力检查而主动终止，SIGINT 下的临时夹具清理异常不作为最终验收结果；最终完整重跑通过。锁文件/四份导出一致性、pip check 和差异格式检查通过。
- Docker Hub 认证端点本批 5 秒连接检查仍超时（HTTP 000），未重新启动完整镜像构建，未关闭验证或切换来源。目标 Linux/真实 SSH 服务/真实 OSS 及整体生产验收仍未完成。本批没有新增 Schema、迁移业务库、提交/推送、主动重启用户服务或修改前端；上轮前端测试不算本批新验收。下一阶段仍是快照撤权、事务外准备、去重、持久任务与对象回收。

以下为管理写入撤权批次及更早的历史验证：

- 管理写入撤权批次完整平台门禁：1098 passed、3 skipped、8 deselected（502.04 秒），覆盖 SQLite 与独立 PostgreSQL 17.10，并使用已构建的 17.11 备份客户端。3 项仅为 SQLite 不适用的 PostgreSQL 专项；8 项为独立 Docker 及原生 Linux 测试。真实 Docker 门禁另有 6 passed、1103 deselected（23.36 秒），没有跳过。生产镜像/原生 Linux/真实 OSS 仍未验收。
- 本批新增授权专项 182 passed（122.79 秒），全部纳入以上最终全量结果；覆盖七类计费/模型/评测写入的四种撤权、两种提交顺序、审计故障、会话/密码期限和配额目标竞争，以及 Agent/评测结果/ChangeSet/非生产路由与编译期间撤权。先在旧实现复现 7 项失败，首轮修复 SQLite/PostgreSQL 28 项通过；扩大测试中的夹具表名和 SQLite 写锁内屏障问题已修正，草稿测试使用独立连接并保留单一赢家/版本断言。
- 本批锁文件/四份依赖导出一致性、pip check 和差异格式检查通过。未新增 Schema、未迁移业务库、未修改前端、未另做浏览器验收、未重启用户服务、未提交/推送。Docker 官方认证端点本次 5 秒连接检查仍超时，未重试完整镜像构建，未更换镜像来源或关闭验证。实现范围及剩余外部 IO 授权巡检见 [管理写入一致性](management-write-consistency.md)。

以下为 Schema 20 上传批次及更早的历史验证：

- Schema 20 上传批次完整平台门禁：914 passed、3 skipped、8 deselected（372.72 秒），使用独立 PostgreSQL 17.10 和已构建的 17.11 备份客户端。3 项仅为 SQLite 不适用的 PostgreSQL 专项；8 项为 6 项独立 Docker 及 2 项原生 Linux。真实 Docker 门禁另有 6 passed、919 deselected（22.67 秒），没有跳过。完整生产镜像/原生 Linux/真实 OSS 仍未验收。
- 在以上全量收集后，新增 SQLite/PostgreSQL 两项真实摄取取消排空后恢复回归；上传专项最终 44 passed（12.82 秒），包括跨连接配额竞争、同版本重复完成/领取、真实 SDK 长度签名、总超时、线程取消所有权和旧 Schema 迁移。新增两项不计入前面的 914 项。此前上传/知识事务/隔离专项为 142 passed、2 deselected（48.03 秒）。
- 本批前端 8 passed，类型检查和生产构建通过；主包 618.00 KB / gzip 178.19 KB，体积警告仍保留。本机没有进行新的浏览器或实际 OSS/CORS 验收，签名测试使用真实锁定 SDK 与合成凭据，不连接供应商。锁文件/四份依赖导出一致性、pip check 和差异格式检查通过。没有迁移业务数据库、重启用户服务、提交或推送。

以下为灾备批次及更早的历史验证，不覆盖 Schema 20 的新增改动：

- 灾备批次最终完整平台门禁：872 passed、3 skipped、8 deselected（371.29 秒），使用实际构建的 17.11 客户端及独立 PostgreSQL 17.10。3 项跳过仍仅为 SQLite 不适用的 PostgreSQL 专项；8 项未选中为 6 项真实 Docker 及 2 项原生 Linux 解析。独立 Docker 门禁 6 passed、877 deselected（26.00 秒），没有跳过；原生 Linux 生产镜像仍未验收。
- 最终全量收集后补强了日志配置的 Docker 构建白名单断言和备份文件排除规则，交付专项另有 44 passed（1.53 秒）。实际 Docker 使用 FROM scratch/COPY 构建并导出日志配置，内容与源文件完全一致，验证文件确实进入构建上下文；这不是完整应用镜像构建成功。8 条监控规则语法及触发/恢复复验、锁文件/四份导出一致性、pip check 和差异格式检查均通过。本批未修改前端，没有把上一批前端成绩算作本批新验收。
- 灾备批次第一轮完整平台门禁：871 passed、3 skipped、8 deselected（374.07 秒），使用既有 17.10 客户端。随后增加迁移与备份竞争的立即拒绝回归，并优化对象清单索引、统一二进制摘要格式；最终结果为上面的 872 项完整复验，初轮成绩不覆盖后续修改。
- 灾备/交付/Checkpoint 迁移专项最新 79 passed（8.59 秒），使用从官方源码下载、固定 SHA-256 校验并在本机实际构建的 PostgreSQL 17.11 pg_dump/pg_restore。构建最初发现 OpenSSL 搜索路径和组件并行生成文件竞争；配置本机开发库路径、显式生成头文件并串行组件构建后成功。没有安装/升级/重启 PostgreSQL 服务。
- 专项覆盖真实一致快照并发更新、Checkpoint/blob/pending writes、文档与源码归档固定版本恢复、知识索引摘要、恢复库默认只读及 API 拒绝启动、认证失败/路径逃逸/缺失对象/失败保留。对象端为测试版本存储，不冒充真实 OSS；仅使用并清理本次自建数据库。新增 cryptography 显式依赖，项目锁文件及四份导出一致性与 pip check 通过；本批不修改前端代码。

以下监控与更早记录为历史成绩，不覆盖灾备批次的后续修改：

- Schema 19 监控批次最终完整平台门禁：840 passed、3 skipped、8 deselected（388.85 秒），覆盖 SQLite 与独立 PostgreSQL。3 项跳过仍仅为 SQLite 不适用的 PostgreSQL 专项；8 项未选中是 6 项真实 Docker 执行及 2 项原生 Linux 解析。独立 Docker 门禁 6 passed、845 deselected（34.49 秒），没有跳过。原生 Linux 和完整生产镜像仍未验收。
- 监控/交付/旧 Schema 18 升级专项最终 88 passed（15.30 秒），含原生模型回调、有界 SDK 队列实际丢弃及摄取父子 Trace。随后增加的 Worker 指标监听器退出测试已纳入上述 840 项全量通过。前端 7 passed，类型检查/构建通过（主包 617.67 KB、gzip 178.06 KB，体积警告保留）；本监控批次未另做浏览器操作验收。
- 官方 Prometheus 3.13.2 macOS arm64 工具下载并校验 SHA-256 后，8 条告警语法及全部触发/恢复用例通过；Linux CI 使用另行固定的官方 amd64 校验值，远端尚未运行。项目锁文件/四份导出一致性和 pip check 通过，新增 Prometheus/OpenTelemetry 依赖已在专用 Python 3.13 环境安装。没有把旧版依赖 wheel 构建成绩记为本批镜像成功。
- 本批扩大回归曾暴露 SDK 指标读取签名和测试响应键不匹配，以及旧迁移断言仍固定 Schema 18；均修正并完成上述最终复验。Docker Hub 认证端点本轮有界连接检查仍超时，未重启服务、关闭 TLS/哈希校验、迁移业务库或提交/推送。

以下取消批次和更早记录为历史成绩，不覆盖以上最新成绩：

- Schema 18 取消批次最终完整平台门禁：797 passed、3 skipped、8 deselected（338.74 秒），覆盖 SQLite 与独立 PostgreSQL。3 项跳过仅为 SQLite 不适用的 PostgreSQL 专项；8 项未选中为 6 项真实 Docker 参数化执行及 2 项原生 Linux 解析验收。最终独立 Docker 门禁为 6 passed、802 deselected（22.97 秒），没有跳过；原生 Linux 解析仍未运行。
- 取消专项最终 50 passed（18.97 秒），覆盖数据库回滚、SIGKILL 后接管、慢收尾续租、迟到结果、代次/工作区绑定、撤权、丢失权限视图/网络、凭据分离、非法归档和创建迟到清理。前端测试 5 passed，类型检查/构建通过，主包约 617 KB 告警仍在。Docker Hub 认证端点本轮再次 10 秒连接超时，未重启 Docker、关闭验证、迁移业务数据、提交或推送。
- 取消批次第一轮完整平台门禁为 784 passed、1 failed、3 skipped、8 deselected（368.96 秒）；唯一失败为账号测试的显式迁移名称清单缺少 Schema 18。已补齐准确断言并完成上述最终全量复验，未放宽门禁。
- 本批隔离浏览器验收使用临时 SQLite、合成身份和 FakeSandboxProvider，分别准备真实收尾服务的 RUNNING/PENDING/COMPLETED 记录；运行详情及 Coding Workbench 正确区分取消中/自动重试/已取消，已取消记录展示五类标准产物和 PARTIAL/REVIEW_REQUIRED。截图发现共享提示样式在窄侧栏排成三列，已用独立样式改为纵向；重新构建、刷新后复验。最终前端 5 passed，类型检查/构建通过（617.47 KB 主包告警保留）。此浏览器测试不代替真实沙箱端到端取消，也不声称覆盖运行详情的自动刷新；真实取消由独立 Docker 门禁覆盖。

以下为更早批次的历史记录，不覆盖以上最新成绩：

- 生产交付/Worker 本批专项 58 passed；修复实际 Docker 发现的问题后，交付、Worker、配对恢复和沙箱租约专项合计 87 passed（包括独立 PostgreSQL）。干净 Python 3.13 环境锁文件检查、pip check、带哈希生产 wheel 构建/清单复验通过，前端 5 passed 且构建通过（主包约 617 KB 告警仍在）。
- 本批第一轮完整平台门禁 742 passed、3 skipped、8 deselected（353.58 秒）；随后生产归档修复加入新检查，第二轮发现三个旧容器夹具未模拟挂载元数据/临时文件，结果 744 passed、3 failed、3 skipped、8 deselected（337.17 秒）。夹具已补齐真实契约，相关专项 87 passed；最终完整复验仍在进行，不把专项结果冒充新的全量成绩。
- 本机 Docker 29.4.0/OrbStack（Linux arm64）真实门禁最新 5 passed、1 failed（取消标准产物缺失，24.54 秒），没有跳过；隔离用例现在同时使用 SQLite/真实 PostgreSQL 已验证身份，五个用例对应六次参数化执行。临时内存卷配对恢复、容量、中断和真实 Coding 修改已通过；旧执行租约不能写入仍保持强制拒绝。失败测试已改为按实际 provision 得到的容器 ID 清理，避免断言失败遗留资源。
- 生产镜像构建实际运行后在 Docker Hub OAuth 认证端点连接超时处失败；原生 Linux 解析镜像未运行，远端 CI 未执行。没有更换不可信镜像源、关闭 TLS/哈希验证或重启 Docker。没有迁移业务数据库、提交或推送代码。

- 知识写入批次完整非 Docker 回归：687 passed、5 skipped、5 deselected（361.51 秒），覆盖 SQLite 与独立 PostgreSQL。5 项跳过为 2 项原生 Linux 解析和 3 项 SQLite 不适用的 PostgreSQL 专项；5 项真实 Docker 用例未执行。收集后新增的完整摄取重试用例另有 2 passed；前端 5 passed，构建、Python 编译及差异格式检查通过。上述两批后端验证范围不应混写为一次全量成绩。
- 知识写入批次：先在旧实现上复现 8 项事务故障（2 passed、8 failed）；修复后知识相关专项 90 passed，继续加入撤权/完成竞争和非法记录测试后，本批原子性专项 88 passed，全部覆盖 SQLite 与独立 PostgreSQL。全量回归收集后另补真实上传→摄取→重复准备/完成用例，两种数据库均通过（2 passed），不冒充已纳入先启动的全量回归。前端请求重试测试 5 passed，类型检查及构建通过，主包约 617 KB 的告警仍在。最终完整回归见上一条记录。
- 本批隔离浏览器验收：首次成功创建的响应被故意丢弃；刷新页面后重新提交相同内容，列表仍只有 1 份知识库；收到成功确认后再次主动创建，列表变为 2 份。使用独立临时数据库和合成身份，已关闭自建标签页与测试服务并清理临时夹具数据。本批浏览器覆盖创建重试，文件上传重试由后端完整摄取用例及前端指纹测试覆盖，不宣称已执行文件选择器的浏览器验收。

- Linux 解析策略批次完整非 Docker 回归：599 passed、5 skipped、5 deselected（374.22 秒），覆盖 SQLite 与独立 PostgreSQL。5 项跳过为 2 项原生 Linux 解析验收和 3 项 SQLite 不适用的 PostgreSQL 专项；5 项真实 Docker 用例明确未执行。Python 编译、差异格式检查及配置默认值核对通过；本批未修改前端代码。
- Linux 解析策略本机专项 75 passed、2 skipped，包含原资源隔离回归；两项跳过为原生 Linux 测试。严格验收入口在 macOS 按预期拒绝执行，不能计为通过。下一阶段仍需只读运行镜像、目标 Linux 内核/原生调用验收及全量生产交付验证。
- 本轮账号/解析批次完整非 Docker 回归：560 passed、3 skipped、5 deselected（396.26 秒），覆盖 SQLite 与独立 PostgreSQL。5 项真实 Docker 用例明确未执行，3 项跳过是 SQLite 不适用的 PostgreSQL 专项。Python 编译及差异格式检查通过，解析配置示例的 15 项默认值与实现一致；没有运行本批前端构建，因为未修改前端代码。
- 本轮账号第一批专项 97 passed（含 SQLite/独立 PostgreSQL 和原账号用例）；随后新增拒绝审计故障、过期/撤销会话、账号范围与密码要求变化、登录/改密反向竞争及启动/用户名竞争，均纳入上述完整回归。
- 本轮开始前只读 review 专项 70 passed、16 skipped（未配置连接的 PostgreSQL 用例），包含解析、账号、安全、健康和资源权限。在独立内存数据库中复现了用户已创建但审计缺失、密码已提交但会话未撤销两项问题，本批针对它们补充了事务与回归测试。
- 本轮 Docker 信息只读探测 5 秒超时，未重启 Docker、清理容器或改变用户服务；真实 Docker/Linux 验收仍未完成。
- 生产路由/发布/账号专项：73 passed，覆盖 SQLite 与独立 PostgreSQL。随后补充跨项目、旧库保留、撤权后取消和发布/路由竞争，并启动全量回归，最终结果以下续记录为准。
- 生产路由批次首次全量：412 passed、2 failed、3 skipped、5 deselected（301.51 秒）；两项失败均为容量索引测试仍断言 Schema 16，已改为 17。随后包含 SQLite/真实 PostgreSQL 的容量迁移与旧库保留专项 4 passed；当前已启动完整复跑，尚不能将复跑计为通过。3 个跳过为 SQLite 不适用的 PostgreSQL 专项，5 个真实 Docker 用例明确未执行。
- 路由控制台使用一次性数据库和两个独立账号完成实际页面操作：申请不生效、自审批入口不可用、独立确认后批准、取消不改变路由、回滚经审批形成新版本。修复浏览器验证发现的“刷新列表但详情仍显示旧 PENDING”问题；刷新后列表、详情与生效版本一致。前端构建通过，主包约 616 KB 的警告仍待后续拆分。
- 本批浏览器验证已结束并关闭自建测试标签页、停止自建验收服务；未操作业务账号。后续解析隔离预检确认当前 macOS 不支持所尝试的 RLIMIT_AS 限制，且 psutil 尚未安装；不能将 Linux 内存限制策略直接当作本机已有保障。
- 本批首次 SQLite 专项 37 passed、3 failed：两处测试辅助参数重名和一处旧迁移版本断言；修正后得到上述 73 项通过，未放宽业务断言。
- 本批容量与健康全量：375 passed、3 skipped、5 deselected（150.71 秒），覆盖 SQLite 与独立 PostgreSQL。3 个跳过为 SQLite 不适用的 PostgreSQL 专项，5 项真实 Docker 明确未运行。随后控制台状态与就绪结果统一，相关健康/平台/静态控制台专项 46 passed；前端构建、Python 编译与差异格式检查通过，约 605 KB 主包警告仍在。
- 首次包含容量的全量为 374 passed、1 failed；唯一失败是测试迁移清单缺少 Schema 16，补齐明确断言后账号专项 7 passed，并完成上述全量重跑。未降低迁移验证条件。
- 健康/路由竞争/任务幂等专项：16 passed，覆盖 SQLite 和真实 PostgreSQL。加入容量前全量为 347 passed、3 skipped、5 deselected。
- 容量及持久化专项首次有测试夹具缺失字段/Content-Type 错误，修正后 27 passed；继续增加了用户/项目配额隔离与索引保留数据验证。
- 上一批知识与发布阶段：331 passed、3 skipped、5 deselected，覆盖临时 SQLite 和真实 PostgreSQL；3 个跳过是 SQLite 不适用的 PostgreSQL 专项，5 项真实 Docker 测试明确未执行。
- 新增知识元数据与发布专项最终共 38 passed，含路由变化拒绝、发布/回滚路由同步；首次发布专项 30 passed，补充路由变化后 32 passed。
- 前端类型检查、生产构建、Python 编译与差异格式检查通过。当前主 bundle 约 605 KiB，超过 500 KiB 的构建警告仍待处理。
- 发布页面正在用隔离浏览器环境验证；测试域名 CORS 配置已修正，生产安全策略未放宽。完整浏览器验收结果以下续记录为准。
- 前一轮只读 review 实测基线为 293 passed、3 skipped、5 deselected；本轮在此基础上修复知识元数据与环境发布缺口。
- 2026-08-31 review 基线：260 passed、3 skipped、5 deselected（未运行的真实 Docker 用例）；本轮没有重启用户服务、修改现有账号或迁移现有数据。
- 前一阶段模型绑定专项 10 passed，计量专项初次 18 passed；加入价格竞争后，连同平台/知识/预算/评测/持久化专项共 67 passed，已完成后续全量验证。
- 本轮首次全量验证 288 passed、2 failed、3 skipped、5 deselected；两个失败均为迁移版本断言仍停留在 11，已更新为实际的 13。最终结果以后续记录为准。

- 本轮静态文件及 SQLite/PostgreSQL 并发编辑、编译竞争、重复部署专项：26 passed。
- 评测与 SQLite/PostgreSQL 并发策略/幂等专项：12 passed；随后补充了非整数分数和输入完整性回归。
- 最新评测、账号治理和普通运行契约专项：34 passed。
- 前端类型检查通过；先前生产构建仍有主 bundle 大于 500 KiB 的体积警告。
- 本轮配对恢复及隔离服务专项已通过 30 项（包括 SQLite/真实 PostgreSQL、子进程强制结束和服务端恢复契约）；随后增加新输入/审批票据与回答恢复回归。
- 配对恢复与 Coding 专项最终通过 34 项，覆盖恢复后的代次递增、已完成回答、新 Run 输入和审批票据绑定。
- 先前配对恢复阶段全套非 Docker 回归（含真实 PostgreSQL 17.10）：219 passed、3 skipped、5 deselected（真实 Docker 用例）。前端类型检查、Python 编译检查及差异格式检查通过。
- Docker 只读探测本轮出现超时，真实 tmpfs 用例因不可用跳过；没有重启 Docker 或清理用户容器。真实 Docker tmpfs/取消恢复与目标 Linux/mTLS 集成验收仍未完成。

不得因此跳过后续阶段或标记整体目标完成。
