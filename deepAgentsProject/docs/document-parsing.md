# 文档解析：资源限制与 Linux 安全策略

## 已接入的执行链路

知识摄取 Worker 校验对象 SHA-256 并扫描文件后，将单份文档通过管道发送给一次性 Python 子进程，在子进程中完成解析及分块。原文不写入临时文件或命令行；解释器使用隔离模式，工作目录为独立临时目录，继承的文件句柄关闭，环境仅保留固定语言及线程设置，不传递模型和数据库环境凭据。生产子进程在读取任何文档正文前必须成功安装内核策略并通过拒绝探测。

父进程限制标准输出/错误输出，校验返回协议、版本、Chunk 次序、长度、定位字段及 SHA-256。解析失败、超时或取消后终止所属进程组、排空管道并回收进程；重复取消或子进程创建中的取消也等待清理。错误不返回解析库原始异常、文档正文或宿主路径。

解析器与分块器版本均为 `2.0`。新摄取结果记录新版本；既有索引不被自动重写。本批资源限制不要求数据库迁移。

## 配置

环境变量均以 `DEEPAGENT_PARSER_` 为前缀，完整默认值见 `.env.example`；整数及有限数值校验和硬上限见 `packages/knowledge/ingestion/limits.py`。

`DEEPAGENT_PARSER_OS_SANDBOX` 留空时，生产模式强制启用 Linux 内核策略，开发模式仅启用资源限制。Linux 开发/验收可以显式设为 `true`。生产显式设为 `false` 或配置未知值会失败，不存在缺少内核能力后自动退回普通子进程的路径。生产环境值会先去掉空白并统一大小写。

| 后缀 | 默认值 | 含义 |
| --- | --- | --- |
| `TIMEOUT_SECONDS` / `CPU_SECONDS` | 30 / 15 秒 | 等待执行名额、创建及解析的总等待时长 / 子进程 CPU 时间 |
| `MEMORY_BYTES` | 512 MiB | Linux 地址空间上限，macOS RSS 监测阈值 |
| `MAX_INPUT_BYTES` / `MAX_OUTPUT_BYTES` | 100 / 16 MiB | 单份原文 / 子进程结果大小 |
| `MAX_EXPANDED_BYTES` | 64 MiB | DOCX 实际解压总量、PDF 内容流展开总量 |
| `MAX_ARCHIVE_ENTRIES` / `MAX_COMPRESSION_RATIO` | 2048 / 200 | DOCX 条目数及每条目压缩比上限 |
| `MAX_PAGES` / `MAX_BLOCKS` | 500 / 20000 | PDF 页数 / 解析结构块数 |
| `MAX_TEXT_CHARACTERS` / `MAX_CHUNKS` | 2000000 / 5000 | 提取文本字符数 / 最终分块数 |
| `CHUNK_CHARACTERS` / `OVERLAP_CHARACTERS` | 2200 / 220 | 分块大小 / 重叠字符数，重叠必须小于分块 |
| `MAX_CONCURRENT` | 2 | 单 Worker 进程内的解析并发名额，不是集群总配额 |

DOCX 在解析前校验 ZIP 条目路径、重名、加密、压缩方式、声明大小、实际展开大小和 CRC；不向磁盘解压。超限明确失败，不以静默截断的索引假装完整摄取。PDF 解压本身也可能先分配内存，因此展开大小检查必须与进程内存/时间限制配合，不能替代它们。

## Linux 内核策略

策略版本为 `landlock-seccomp-v1`，实现位于 `packages/knowledge/ingestion/linux_sandbox.py`：

- 要求非 root、非 set-ID 的单线程 Linux x86_64/aarch64 进程；预加载受信解析库后再次确认线程数，并关闭所有非标准输入/输出/错误的句柄。预先打开的文件描述符不能留作绕过文件访问限制的入口。
- 设置 `no_new_privs` 和不可转储属性；Landlock ABI 至少为 3，必须覆盖读取、写入、执行、重命名与截断等文件访问权限，不兼容时直接失败。
- 仅允许读取由 Python 运行时推导的标准库/site-packages/dist-packages 目录及标准库压缩包。不给工作区、业务源码、`.env`、数据目录、`/run/secrets`、`/proc` 或整个宿主根目录读权限；不允许写入任何目录。项目中的解析实现先作为受信代码加载，不把项目目录加入读取白名单。
- libseccomp 默认拒绝系统调用，仅允许解析需要的读写管道、受 Landlock 约束的文件读取、内存、时间和解释器操作。网络、IPC、启动程序、创建线程/进程、ptrace、跨进程内存访问、信号发送、权限修改、命名空间、挂载和 io_uring 不在允许列表；其他架构调用入口不被启用。`fcntl` 仅允许读取标志，不能设置异步信号目标。
- 策略安装后实际尝试读取自身 `/proc` 环境、写入临时目录和创建网络 Socket，必须得到权限拒绝。父进程检查返回的策略版本和 ABI；这个标签是受信启动链路的协议检查，不是独立的远程可信证明。

Landlock 建立单独的受限安全域，子进程的数值 UID 仍与 Worker 相同；这里没有承诺独立 UID 或挂载命名空间。必要的 `stat`/路径元数据查询并未完全隐藏文件存在性。标准库及已安装包目录必须是不可变发布资产，不能放置凭据或用户上传内容。内核策略不能防止内核漏洞，也不能替代外层非 root/只读运行镜像和目标环境安全验收。[Landlock 内核契约](https://docs.kernel.org/userspace-api/landlock.html)

Python 审计钩子继续作为附加检查，但不是上述安全边界；操作系统测试使用原生 C 系统调用绕开钩子来验证拒绝行为。[Python 审计钩子边界](https://docs.python.org/3/library/sys.html#sys.addaudithook)

## 启动、平台差异与验收

Linux 子进程同时设置地址空间、CPU、文件大小、文件句柄和进程数限制，以及父进程死亡终止机制。macOS 开发环境使用采样 RSS 监测，存在采样间隔，不是严格的内存硬上限，也不具备此 Linux 策略。

摄取 Worker 在注册心跳、对账或消费队列前执行一次受保护的实际文本解析自检；自检失败则启动失败，已有持久任务不会被领取。每份文档的子进程仍重新安装和探测内核策略。API-only 进程不执行本机摄取自检；Worker 不在线会由已有健康检查反映。自检不是模型/对象存储/扫描器全链路健康检查，也不是所有格式与恶意文档的完整验收。

生产 Worker 必须安装提供 `libseccomp.so.2` 的系统包（Debian/Ubuntu 为 `libseccomp2`），目标内核启用 Landlock 且支持 ABI 3 或更新，外层容器安全策略允许安装这些不增权的限制。不能通过添加 `--privileged`、Docker Socket、放宽整个系统调用策略或关闭解析隔离来让验收通过。当前代码不包含已验收的生产 Worker 镜像，这仍属于生产交付阶段。

在准备好的非 root Linux Worker 环境中运行：

```sh
make test-parser-linux
```

该入口在非 Linux/root 环境先失败，不能把测试全部跳过当作成功。Linux 测试会实际验证原生文件读写、IPv4/IPv6/Unix Socket、信号、权限和 exec 调用被拒绝，再验证受限进程下的七种格式与启动自检。缺少内核功能或 libseccomp 时测试失败，不自动跳过。升级生产 Worker 前必须执行这个验收；本地 macOS 成绩不能代替它。

对象下载、扫描和 Embedding 仍在原摄取链路中，不受此解析子进程的资源上限覆盖。运行库只读交付、真实 Linux 内核验收、恶意文件红队和外部依赖健康仍未完成。

## 已有测试范围

`tests/test_parser_isolation.py` 包含真实文本/Markdown/CSV/JSON/HTML/DOCX/PDF 子进程解析、定位字段、页数与文本限制、ZIP 展开、输出溢出、超时、内存监测、CPU 限制、取消回收、子孙进程管道占用及环境/句柄隔离测试。macOS 测试通过不代表 Linux 内存限制、完整操作系统隔离或生产恶意文件红队验收通过。

`tests/test_parser_os_sandbox.py` 区分策略构造/失败关闭契约测试与原生 Linux 测试。当前本机解析专项为 75 passed、2 skipped；两项跳过明确为原生 Linux 验收。本机运行 `make test-parser-linux` 已按预期拒绝 macOS，并非 Linux 验收通过。本批完整非 Docker 回归为 599 passed、5 skipped、5 deselected（374.22 秒）；详细范围见企业化实施状态。
