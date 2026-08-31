# 仓库接入的出站边界

本批修复仓库网络访问，不增加 Schema、不迁移业务库。它不代表快照创建的授权、幂等、事务和回收流程已经全部完成。

## 实际连接约束

- 注册和每次 checkout 均重新解析、验证地址。生产必须配置 `DEEPAGENT_REPOSITORY_ALLOWED_ORIGINS`；空值禁用远程仓库访问，不影响没有使用远程仓库的其他功能。配置使用完整、精确的 origin，例如 `https://github.com`，不接受通配符、路径、用户名、密码、query 或 fragment。开发空配置仍仅允许公网地址。
- 所有 DNS 结果都必须是允许的公网地址；混合公网/内网结果整体拒绝。拒绝 loopback、链路本地、私网、共享地址、组播以及 IPv6 转换/隧道地址，不能通过备用地址或 NAT64/6to4 绕过。DNS 使用独立解释器，5 秒超时后终止并回收。
- HTTPS 经过单次 checkout 专用的本地 CONNECT 网关。网关只向刚验证的数字 IP 建立连接，每个 CONNECT 都必须匹配原 origin。Git 与远端保持端到端 TLS，网关不解密 TLS；客户端继续验证证书和主机名。网关凭据只存在于本次子进程环境，不进入命令参数、数据库或远端请求。
- clone 在全新的临时目录中运行，避免读取当前项目的 Git 配置；执行前验证 Git 确实读取到了显式代理设置，不支持该配置入口的版本会拒绝操作，不回退到直接联网。
- 显式拒绝 HTTP 重定向。Git 的 HTTP 备用对象地址也不能扩大 origin；测试中刻意放宽 Git 的重定向选项，仍要求连接网关拒绝实际跨站 CONNECT。不能只验证初始 URL 就信任后续传输。
- SSH 固定 `HostName` 为已验证 IP，并按原域名 `HostKeyAlias` 校验独立供应的主机公钥。禁止自动信任新主机密钥、代理、跳板、用户 SSH 配置、SSH agent 和默认身份文件。使用已知主机公钥，不运行 `ssh-keyscan` 自动建立信任。
- Git 仅在 clone 阶段联网，获取完整对象且不 checkout、不加载模板、不递归拉取子模块。后续探测、归档和文件枚举清空协议白名单、禁止 lazy fetch，并禁用外部 fsmonitor、hooks、全局/系统 Git 配置和环境凭据。

远端临时目录只在本次操作中被认可，不加入共享的本地路径白名单。修复了旧实现将远程 checkout 错当成本地未授权目录的问题。本地 linked worktree 的 Git metadata/common directory 也必须位于显式批准的根；未获批准的共享对象目录、对象目录外链和非空 alternates 会拒绝访问。这里不把操作员授权的宿主目录当作可供不可信用户任意并发改写的隔离沙箱；生产默认不开放本地目录。

## 限制与退出

HTTPS 网关每次 checkout 最多 8 个活动连接，累计双向传输不超过 1 GiB；单次 CONNECT 头部最多 16 KiB，头部/连接/写入有超时，空闲隧道超时 30 秒。Git clone 执行期限 180 秒，输出上限 1 MiB。失败响应不反射远端 stderr。

超时、失败和取消会终止独立 Git/SSH 进程组，回收直接子进程，并等待组内辅助进程实际终止；已成为 zombie 的进程没有活动文件或连接，由其父进程接管者回收。不能把“发送 SIGKILL”当成“资源已经释放”。网关关闭时关闭连接并排空自己拥有的处理线程。清理若遭遇内核级阻塞，会继续等待实际终止，这不是硬实时的接口响应承诺。`__enter__` 失败也清理本次临时 checkout。

这些限制不替代跨用户/租户的快照任务准入、磁盘总量配额或持久恢复。直接 SSH 当前只有执行/输出期限，不能将 HTTPS 网关的字节上限套用到它。

## 公共信任材料与交付

默认 HTTPS 使用运行镜像的系统 CA。自定义 CA 设置 `DEEPAGENT_REPOSITORY_CA_FILE`；SSH 必须设置 `DEEPAGENT_REPOSITORY_SSH_KNOWN_HOSTS_FILE`。文件必须是绝对路径、非符号链接的普通文件，非空且不超过 1 MiB，group/other 不得写入。生产通过只读配置文件注入，仅包含公共证书/主机公钥，不能放客户端私钥或令牌。

两个可选 Compose overlay 仅向 API 提供固定路径：

- `deploy/repository-https.compose.yaml`：`REPOSITORY_CA_SOURCE_FILE` → `/run/repository-trust/ca.pem`。
- `deploy/repository-ssh.compose.yaml`：`REPOSITORY_KNOWN_HOSTS_SOURCE_FILE` → `/run/repository-trust/known_hosts`。

宿主源文件还必须对运行 UID 可读；提供的预检要求公共文件 other 可读、group/other 不可写，例如 0444/0644。Compose 文件型 config 不保证替你更改宿主文件权限，因此同时检查真实文件元数据。预检拒绝改写目标路径、可写 mode、未知/内嵌 config、缺失挂载及 Worker/迁移/沙箱角色的额外配置访问。

导出原有部署变量及对应公共文件变量后，只读检查：

```bash
python scripts/release.py config --kind platform --repository-trust https
# 同时启用两个公共信任 overlay 时使用 --repository-trust both。
```

实际部署须使用同样的 overlay 集合和已审核镜像，预检不会部署。生产镜像显式安装 OpenSSH client；原生镜像验收组除 Linux 解析测试外也运行仓库网络测试。可信镜像构建和目标环境连接仍需要实际验收。

## 测试和剩余工作

`tests/test_repository_network.py` 包含真实本机 Git + 本机 HTTPS 服务 + 临时 CA 的完整源码拉取/归档/探测，证书不可信、主机名不匹配、重定向、备用对象跨站、DNS 改变、IPv4/IPv6 私网、环境污染、SSH 参数、Git metadata 越界及子进程退出测试。只有本机 TLS fixture 绕过公网地址构造，真实连接不访问公网；公网判断单独使用拒绝用例验证。没有用自签测试 CA 替换系统信任配置。

本批不支持仓库客户端凭据：原 `credential_ref` 拒绝策略继续保留。SSH 主机公钥不等于用户认证凭据，服务器要求认证时操作失败；不暗中借用宿主私钥。企业私有 Git 凭据代理、受审批的内网仓库接入、实际 SSH 服务/目标 Linux/TLS 组合验收仍需继续。

快照扫描期间撤权、上传后才去重造成的无引用版本、Coding/路由创建事务内的外部 IO、任务公平准入、崩溃后的持久接管及对象回收仍是下一阶段。不要把本批网络修复当成这些问题已经解决。

协议依据：[Git HTTP 与代理配置](https://git-scm.com/docs/git-config)、[Git 协议及 lazy-fetch 环境约束](https://git-scm.com/docs/git)、[OpenSSH 客户端设置](https://man.openbsd.org/ssh_config)。
