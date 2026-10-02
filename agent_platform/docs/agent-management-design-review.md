# Agent 管理设计实现评审记录

日期：2026-10-03（Asia/Shanghai）。当前结果是可运行的评审版：真实 API 功能验收 6/6 通过，构建和独立类型检查通过；**Figma 像素验收未通过**，未批准或更新任何网页视觉基准。

## 范围与已有工作保护

- 开始时分支为 `master`，HEAD 为 `366b589`；没有已修改的跟踪文件。原有未跟踪的根目录 `index.html`、前端 `.design-to-ui/`（包括 `20261002-native`）均保留，未覆盖。
- 按 `/Users/jie/AGENTS.md`、现有前端功能规范、接口契约和实现代码执行。沿用 React 19、TypeScript、Vite、原有 hash 路由、API 客户端及 Button / Field / Modal / ResourceState 等组件。
- 仅实现 Agent 列表、详情、创建/编辑及相关保存、发布、错误和权限状态。其他模块的页面实现、全局样式和共用组件文件未改动。Agent 页专用样式限定在 `.agent-shell` / `.agent-management` 和专用类名内，字体也使用专用别名。
- 后端源代码、数据库迁移、依赖清单和锁文件未修改；未提交、推送或部署。验收中的写入只发生在新建的独立 SQLite 测试数据库。

## 设计来源

原始入口：[AgentPlatform-UI，10:98](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-98)。通过真实 Figma metadata 定位 Agent 区域 `10:99`，再逐个读取以下画板的设计上下文、截图和资源：

| 页面 | 节点与原始名称 | 视口 / DPR | 前端路由 |
| --- | --- | --- | --- |
| 列表 | [10:100 · A01 · Agent列表 · 已核验](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-100) | 1280 × 1024 / 1 | `/#agents` |
| 详情 | [10:101 · A02 · Agent详情 · 配置与发布 · 已核验](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-101) | 1280 × 1024 / 1 | `/#agents?agent=<id>` |
| 创建 | [10:102 · A03 · 创建Agent · 表单 · 已核验](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-102) | 1280 × 1024 / 1 | `/#agents?agent=new` |

在本次读取的 Agent 区域中，没有找到独立的保存成功、发布确认/成功、校验、请求失败、收起态或移动端画板。这些状态依据现有接口及业务逻辑补齐，使用现有组件并限定 Agent 页样式；不声称有对应设计稿或已获视觉批准。编辑页沿用创建表单结构。

真实设计来源、完整工具响应和参考 PNG 保存在：

`apps/web/.design-to-ui/runs/20261003-agent-review/`

其中 `design-source.json` 记录节点/尺寸/字体/缺失状态，`context-10-100.json` 等保存设计上下文，`metadata-10-99.json` 保存画板层级，`downloaded-assets.json` 记录原始资源与槽位。Figma 生成的参考 TSX 只保存在证据目录，没有直接替代生产业务代码。

## 修改清单

| 文件 | 改动与目的 |
| --- | --- |
| `apps/web/src/AgentManagement.tsx` | 新增列表、详情、创建/编辑、Agent 专用侧栏、草稿保存反馈、发布确认/成功和字段/请求错误；复用现有 API 与组件。 |
| `apps/web/src/agent-management.css` | 按画板实现黑白灰与少量靛蓝、240px 侧栏、64px 头部、双栏配置面板、底部保存栏、专用字体与推断的窄屏布局。 |
| `apps/web/src/App.tsx` | Agent 路由接入新模块；仅该路由使用设计头部/侧栏；保留 capability 门控、组织切换、账号、退出及平台入口。 |
| `apps/web/src/pages.tsx` | 移除被替代的旧 AgentsPage / AgentEditor；其他页面逻辑不变。 |
| `apps/web/public/agent-design/` | 33 个原始 SVG（46 个设计资源槽位）；3 个官方字体文件及各自 OFL 许可证。 |
| `apps/web/tests/agents.spec.ts` | 新增 6 个真实 API 验收场景，仅在显式开启独立测试环境时运行。 |
| `apps/web/tests/agent_acceptance_server.py` | 复现用的测试夹具启动器：使用现有后端和迁移，新目录/新数据库，拒绝使用已有目录。不是后端功能改动。 |
| `apps/web/playwright.config.ts` | 可选浏览器 channel 环境变量，支持本机 Chrome；保留默认行为。 |
| `apps/web/.gitignore` | 忽略插件私有 runtime、数据库和截图证据目录；已有文件仍在本机。 |

三类页面使用相同 Z Logo、头部高度和侧栏状态。收起态宽 64px，只有平台 Logo 点击/键盘激活展开；没有额外展开按钮、`title` 或悬停提示。功能验收覆盖了此行为。

设计中的“演示数据”标记没有伪造为业务状态：该位置显示当前真实组织，并提供现有组织/账号菜单。来源筛选和刷新保留在“已加载”菜单内。设计的会话子入口指向已有对话实验室，未添加独立会话历史或轨迹接口。已发布版本标记在有运行权限时可进入现有对话实验室。

## 接口与状态保留

依据 [前端接口契约](frontend-api-contracts.md) 和 `apps/api/main.py`、`apps/api/schemas.py`、`modules/platform.py` 核对后实现：

- 使用原有 Cookie 登录、CSRF 和组织作用域客户端。读取 `GET /api/v2/tenants/{tenant}/agents`、`/models`；详情来自现有列表数据，没有虚构详情 GET。
- 创建 `POST .../agents`，编辑 `PATCH .../agents/{id}`；保存响应立即更新当前数据，保存不会自动发布。发布单独调用 `POST .../agents/{id}/publish`，使用后端返回的整数版本。
- 名称 1–120、描述 ≤2000、提示词 1–20000、Temperature 0–2、步骤 1–30、Token 1–128000 且受已选模型上限约束；工具仍只有后端支持的 `calculator` / `current_time`。模型缺省为 `null`，沿用组织策略。
- 保留原前端创建默认值：Temperature 1、步骤 6、Token 2048、默认提示词、工具未选。视觉截图中的示例填写通过测试交互完成，没有硬编码为真实页面数据。
- `agents.manage` 决定创建/编辑/发布入口；成员读取的是后端发布快照，不能看到未发布草稿或修改它。失败保留输入或确认对话框，支持重试；保存/发布均防止重复提交。作用域改变后的结果不驱动当前组织页面。
- 没有增加删除、复制、编排或服务端分页，也没有将请求改成模拟数据。

## 截图与视觉结果

以下均为本机证据链接。原设计参考与三轮实际截图均为 1280 × 1024、DPR 1；截图前等待字体与图片解码，禁用动画并隐藏光标。功能状态截图使用测试默认视口 1440 × 1000；移动端截图为 390 × 844，无对应状态/断点稿。

| 页面 | 改造前 | Figma 原稿 | 改造后 | 最终差异 |
| --- | --- | --- | --- | --- |
| 列表 | [before/list.png](../apps/web/.design-to-ui/runs/20261003-agent-review/before/list.png) | [reference-10-100.png](../apps/web/.design-to-ui/runs/20261003-agent-review/reference-10-100.png) | [actual-3/list.png](../apps/web/.design-to-ui/runs/20261003-agent-review/actual-3/list.png) | 0.900% · FAIL |
| 详情 | [旧编辑弹窗 editor.png](../apps/web/.design-to-ui/runs/20261003-agent-review/before/editor.png)，原实现无独立详情页 | [reference-10-101.png](../apps/web/.design-to-ui/runs/20261003-agent-review/reference-10-101.png) | [actual-3/detail.png](../apps/web/.design-to-ui/runs/20261003-agent-review/actual-3/detail.png) | 1.272% · FAIL |
| 创建 | [before/create.png](../apps/web/.design-to-ui/runs/20261003-agent-review/before/create.png) | [reference-10-102.png](../apps/web/.design-to-ui/runs/20261003-agent-review/reference-10-102.png) | [actual-3/create.png](../apps/web/.design-to-ui/runs/20261003-agent-review/actual-3/create.png) | 1.607% · FAIL |

完整对比：[最终 HTML 报告](../apps/web/.design-to-ui/runs/20261003-agent-review/report-3/report.html)、[机器可读报告](../apps/web/.design-to-ui/runs/20261003-agent-review/report-3/report.json)。报告包含原像素区域裁片、差异图、DOM 盒子、实际渲染字体及字体检查。

阈值始终为 `threshold=0.1`、`maxDiffRatio=0.005`（允许差异像素比例 0.5%），未放宽。首轮列表/详情/创建差异分别为 1.376% / 1.710% / 2.493%；完成两轮修正后得到上表结果，三页均没有达到 PASS。

已经修正侧栏与内容宽度、头部和保存栏高度、面板与字段间距、表格行高、原始图标尺寸/变体、背景/边框颜色和实际字体加载。剩余差异主要在导航与字段文本约 1–3px 的基线/间距、详情 Prompt 和执行配置行、创建页参数/工具区域及操作按钮宽度；真实组织标记文案也与设计示例不同。不能把这些全部归为抗锯齿。

插件入口技能规定：“脚本 `run` 默认最多修复两轮，达到预算后报告剩余差异和具体资源/视觉决定”。本次按相同预算完成初次捕获与两轮修正，保留失败证据交付评审。来源：[Design to UI SKILL.md](/Users/jie/.codex/plugins/cache/jie-design-tools/design-to-ui/0.2.0/skills/design-to-ui/SKILL.md)。这不是请求用户批准失败截图，也没有执行 `approve` / `regression` 或更新基准。

共用影响检查：概览页 [改造前](../apps/web/.design-to-ui/runs/20261003-agent-review/before/overview.png) 与 [改造后](../apps/web/.design-to-ui/runs/20261003-agent-review/states/overview-unchanged.png) 的可见像素差异为 **0**，原 70px 头部/235px 侧栏及非 Agent 外壳保留。此对比只是前后影响证据，不是批准的回归基准；其他模块没有逐页截图验收。

## 功能验证与工程检查

最终 `tests/agents.spec.ts` 在本机 Chrome + 独立真实 API 上运行，**6 passed（6.7s）**；没有路由拦截、模拟响应或替代生产数据。

| 场景 | 实测结果 |
| --- | --- |
| 列表/详情/创建导航 | 真实列表、搜索、来源筛选、详情参数、tenant hash、创建取消、HttpOnly Cookie 和 Logo 点击/键盘展开通过；没有页面脚本异常。 |
| 校验/草稿/发布 | 无效名称/超模型 Token 时无 POST；真实创建 201；保存后版本仍为 0；确认取消不发布；双击只发一次发布请求；成功返回 v1；后续编辑草稿不改变发布快照。 |
| 成员权限 | 看不到未发布 Agent，读取已发布说明；隐藏管理按钮；直接写接口 403；直接访问创建页显示无管理权限。 |
| 发布错误与重试 | 通过真实 API 停用模型，发布返回 400 `model_disabled`，保留对话框/版本；恢复模型后重试成功并返回 v2。 |
| 请求失败 | 浏览器断网触发真实列表/保存失败；显示错误，保留输入；联网后重试成功。 |
| 响应式与范围 | 390 × 844 无横向溢出，保存栏/移动导航可用；切换概览后 Agent 类和布局不残留。 |

状态截图：[字段校验](../apps/web/.design-to-ui/runs/20261003-agent-review/states/field-validation.png)、[草稿保存](../apps/web/.design-to-ui/runs/20261003-agent-review/states/saved-draft.png)、[发布确认](../apps/web/.design-to-ui/runs/20261003-agent-review/states/publish-confirmation.png)、[发布成功](../apps/web/.design-to-ui/runs/20261003-agent-review/states/publish-success.png)、[发布失败](../apps/web/.design-to-ui/runs/20261003-agent-review/states/publish-request-failure.png)、[列表失败](../apps/web/.design-to-ui/runs/20261003-agent-review/states/list-request-failure.png)、[保存失败](../apps/web/.design-to-ui/runs/20261003-agent-review/states/save-request-failure.png)、[成员详情](../apps/web/.design-to-ui/runs/20261003-agent-review/states/member-detail.png)、[创建权限拒绝](../apps/web/.design-to-ui/runs/20261003-agent-review/states/member-create-denied.png)、[收起侧栏](../apps/web/.design-to-ui/runs/20261003-agent-review/states/collapsed-detail.png)、[推断移动布局](../apps/web/.design-to-ui/runs/20261003-agent-review/states/create-mobile-inferred.png)。

- `npm run build`：通过（`tsc -b` + Vite）。
- `npx tsc --noEmit`：通过。
- `git diff --check`：通过。
- `npm run lint`：已尝试，失败原因是没有 lint script；项目也没有现有 ESLint/Biome 配置。**Lint 未配置，不能报告通过。**没有为本次视觉工作增设全项目规则。
- 资源审计：所有本地文件非空、原始 SVG 均有引用；截图中实际 img 宽高与原始 SVG 根尺寸一致，两个 CSS 槽位也保留原尺寸。字体有真实浏览器渲染证据，最终三页的显式字体检查通过。
- 测试启动器额外检查：新目录能完成原迁移并启动 API；已有目录被拒绝。运行需从仓库根目录设置 `PYTHONPATH`，否则 Python 无法找到 `agent_platform`。

未验证：用户原有服务/数据及其在用 Cookie、多组织切换和权限撤销的完整矩阵、其他模块的完整回归、真实模型推理。旧 `platform.spec.ts` 使用旧卡片/弹窗与模型管理预期，本次未运行或顺带改造。没有独立状态稿/移动稿的部分没有 Figma 视觉判定，人眼产品评审尚未完成。

## 运行和复现

本次评审服务保持运行：[Agent 管理预览](http://127.0.0.1:5178/#agents)，前端代理到独立 API `127.0.0.1:8033`。测试账号 `admin@example.test` / `Agent-ui-acceptance-2026`；成员账号 `member@example.test` 使用同一测试密码。这些是本次新数据库的夹具账号。功能测试会产生新 Agent 和发布版本，预览中的数据可能多于原稿三行。

接入已有业务后端时，在前端目录运行（将 URL 改为实际可用后端）：

```sh
cd /Users/jie/Github/Z/agent_platform/apps/web
PLATFORM_API_PROXY=http://127.0.0.1:8000 npm run dev -- --port 5178 --strictPort
```

原本机 `127.0.0.1:8010` 检查时不可达，因此本次结果不冒充在原部署上完成验收。

可使用以下命令重新创建独立环境；**每次测试用一个尚不存在的新目录**，避免重复数据影响首个测试的三行断言。先在仓库根目录启动真实 API（示例端口 8034）：

```sh
cd /Users/jie/Github/Z
PYTHONPATH=/Users/jie/Github/Z .venv/bin/python agent_platform/apps/web/tests/agent_acceptance_server.py --directory /tmp/agent-ui-acceptance-20261003-new --port 8034
```

另一终端启动前端：

```sh
cd /Users/jie/Github/Z/agent_platform/apps/web
PLATFORM_API_PROXY=http://127.0.0.1:8034 npm run dev -- --port 5179 --strictPort
```

再运行测试：

```sh
cd /Users/jie/Github/Z/agent_platform/apps/web
PLATFORM_AGENT_ACCEPTANCE=1 PLATFORM_E2E_BASE_URL=http://127.0.0.1:5179 PLATFORM_E2E_BROWSER_CHANNEL=chrome npx playwright test tests/agents.spec.ts
npm run build
npx tsc --noEmit
```

插件捕获/比较的真实调用方式如下，`visual.config.json` 的 baseUrl 与夹具账号必须对应运行环境；列表/详情夹具需保持设计参考状态，避免把后续功能测试数据误当布局差异：

```sh
node /Users/jie/.codex/plugins/cache/jie-design-tools/design-to-ui/0.2.0/scripts/cli.mjs capture --config /Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-agent-review/visual.config.json --project /Users/jie/Github/Z/agent_platform/apps/web --output /tmp/agent-ui-review-capture
node /Users/jie/.codex/plugins/cache/jie-design-tools/design-to-ui/0.2.0/scripts/cli.mjs compare --config /Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-agent-review/visual.config.json --project /Users/jie/Github/Z/agent_platform/apps/web --actual /tmp/agent-ui-review-capture --output /tmp/agent-ui-review-report
```

`compare` 的视觉 FAIL 返回非零退出码是本次真实结果，不应忽略为通过。证据目录受 gitignore 保护，保存在当前本机；它不会随普通 Git 提交自动传播。

## Design to UI 插件实际执行与可用性

实际安装版本 **0.2.0**，核对了插件 `plugin.json`、`package.json` 和缓存目录。`/Users/jie/.codex/config.toml` 的 `design-to-ui@jie-design-tools` 为 `enabled=true`，当前会话技能目录确实加载了入口 `design-to-ui:design-to-ui`。也按 Figma 工具要求加载了 `figma:figma-design-to-code`。

真实执行步骤：

1. 入口技能 → 检查项目规范/Git/接口/组件 → CLI `parse` 成功解析 fileKey `dojPnHY6X67FVc1Zx7OVCc` 与 `10:98`。
2. `doctor --project`：Node `v25.6.0`、Codex CLI `0.159.0-alpha.12.1` 已登录；独立 CLI 结果 `figmaListed=false`。宿主会话工具可读 Figma，二者分别记录，未声称独立 CLI 连接成功。
3. 真实 `figma_get_metadata`、`figma_get_design_context`（带 `skillNames`）、`figma_get_screenshot`：先定位具体节点，再保存上下文/原 PNG。
4. 按返回 URL 下载原 SVG，检查字节数和槽位；经用户明确允许，从 [Google Fonts 官方仓库](https://github.com/google/fonts) 下载 Noto Sans SC、Roboto、Roboto Mono 及 OFL，作为本地资源。
5. CLI `setup --project` 安装锁定的 Playwright 1.63.0、pixelmatch 7.2.0、pngjs 7.0.0 到项目私有 runtime。
6. 在现有业务组件上实现页面，启动 Vite 和独立真实 API；CLI `capture` / `compare` 完成初次捕获和两轮修正（`actual-1/2/3`、`report-1/2/3`），核对字体、资源尺寸和差异区域；运行功能验收及工程检查。

遇到的问题与处理：

| 问题 | 实际处理和限制 |
| --- | --- |
| `10:98` 初次上下文返回未选中可读图层 | 没有据链接猜页面；读取 metadata，定位 `10:99` 下 `10:100/101/102` 后成功读取具体画板。初次错误响应已保存。 |
| 首次 Python 下载得到零字节文件 | 在资源审计中发现，按 Figma 返回的下载说明使用 `curl -L --fail --show-error` 取回同一授权 URL；检查所有文件非空。没有权限拒绝或绕过授权。 |
| 插件 Chromium 153 约 182MB 下载过慢 | 下载约 60% 时终止本次安装进程；runtime 依赖已就绪，bundled Chromium 未安装完成。使用已经安装的 Google Chrome channel，实际截图版本 `154.0.8037.93`、macOS arm64。没有假称 Chromium setup 全部成功。 |
| 配置动作 `check` 不受支持 / 选择器多匹配 | 按插件现有 schema 改为 `click`，将等待条件改成唯一、真实可见元素；登录选择器改为实际 `.login-form`。随后实际捕获成功。 |
| 首次 E2E 找不到 bundled Chromium | 增加可选 channel 环境变量，以本机 Chrome 执行，6 项实际通过。 |
| Noto 字体检测显示 `Noto Sans SC Thin` | 解析原字体 name/fvar 表：typographic family 为 Noto Sans SC，兼容 family 为 Noto Sans SC Thin，变量轴覆盖 100–900；Chrome 实测为自托管的 Regular/Medium/Bold 实例。保留 Figma 原字体事实，显式检测映射到真实内部名称并保存 `font-metadata.json`。没有替换字体或虚构字重。 |
| Python 夹具启动缺少模块路径 | 从仓库根目录设置 `PYTHONPATH=/Users/jie/Github/Z`；启动器运行方式已记录。 |
| 最终视觉差异仍超标 / lint 不存在 | 保留 FAIL 和未验证结论；未修改参考图、阈值或基准，未把构建成功当视觉通过。 |

结论：插件 **在本项目的宿主会话中可用于真实设计读取、原始资源取回、截图、字体审计和像素差异迭代**。它没有完成本次自动视觉验收；还需要针对剩余差异继续修正并由用户评审。独立 CLI `run` 没有执行成功验证，其 Figma 连接不能从宿主可用推导；bundled 浏览器安装仍不完整。

字体原文件总计约 17.6 MiB，其中 Noto Sans SC 约 17 MiB。选择完整官方变量字体保证真实中文内容与字重覆盖；冷启动下载成本仍是本版的性能限制，未进行会改变覆盖范围的字体子集化。

主要汇总：[validation-summary.json](../apps/web/.design-to-ui/runs/20261003-agent-review/validation-summary.json)、[asset-slot-audit.json](../apps/web/.design-to-ui/runs/20261003-agent-review/asset-slot-audit.json)、[local-resource-checks.json](../apps/web/.design-to-ui/runs/20261003-agent-review/local-resource-checks.json)。

## 后续提交授权与范围核验（2026-10-03）

用户后续明确授权提交并推送，并选择“仅提交本会话的 Agent 管理、资源与费用”。共用 App.tsx、AgentManagement.tsx、pages.tsx 按模块分离暂存；组织管理、概览、运行记录及根目录 index.html 的其他工作保留在原工作区，没有纳入提交。待提交树在独立目录构建通过，并重新通过 6 项 Agent 和 9 项资源与费用的真实 API 浏览器测试。原始 OFL 许可证仅清理行末空格。视觉验收仍未通过，没有批准或更新基准。
