# 组织管理 UI 评审记录

2026-10-03。用户补充的 `10:111` 是「组织管理」Section，本次实际读取并改造其四个子画板。实现已可运行，真实接口功能检查通过；**视觉比较仍为 FAIL，尚未完成视觉验收**。网页视觉基准未获批准或更新。

## 设计来源与工作区保护

| 页面 | 原始节点 | 画板尺寸 |
| --- | --- | --- |
| 成员与邀请 | [10:112 · N01](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-112) | 1280 × 1024 |
| 组织配额 | [10:113 · Q01](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-113) | 1280 × 1024 |
| 审计日志 | [10:114 · L01](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-114) | 1280 × 1024 |
| Owner 支持审批 | [10:115 · S01](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-115) | 1280 × 1024 |

入口：[用户提供的 10:111](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI-%25C2%25B7-Approved-Sidebar-States?node-id=10-111)。Section 返回层级信息，继续读取四个具体 Frame 的完整设计上下文、截图和原始 SVG，没有凭链接推测页面。

检查了 `/Users/jie/AGENTS.md`、`frontend-functional-spec.md`、`frontend-api-contracts.md`、`implementation-contract.md`、现有前端与后端接口实现。开始和交付时均为 `master`、HEAD `366b589`。开始时已经有上一轮 Agent 工作和未跟踪文件；保存了 `previous-work.patch` 与 `source-before/`，未重置、暂存或清理这些文件。工作期间其他任务继续写入工作空间、资源与费用模块；这些修改被保留，不属于本次组织管理交付。共享 `App.tsx`、`pages.tsx`、`types.ts` 仅做所需的局部合并。

## 本次修改

| 文件 / 区域 | 改动 |
| --- | --- |
| `apps/web/src/OrganizationDesign.tsx` | 组织页面标题、标签页、搜索、表格页脚和原稿图标映射；按真实时区显示日期。 |
| `apps/web/src/organization-design.css` | 四页的黑白灰、少量靛蓝强调、字号、表格、间距；样式作用于组织路由或专用类。 |
| `apps/web/src/Tenancy.tsx` 的 `MembersPage` | 成员 / 邀请标签、本地搜索、管理入口、所有权选择；保留原有邀请、角色修改及所有权确认流程。 |
| `apps/web/src/pages.tsx` 的 `QuotasPage`、`AuditPage` | 配额编辑和套餐只读页；审计本地筛选、刷新、基于接口已返回数据的详情抽屉。 |
| `apps/web/src/Support.tsx` 的 `OwnerSupportPanel` | 授权列表、审批和撤销确认；按真实到期 / 撤销记录推导状态。其他平台支持页面保持原有实现。 |
| `apps/web/src/App.tsx`、`AgentManagement.tsx` | 组织路由接入共用 64px 头部、240px / 64px 侧栏、正确选中态与图标目录；默认 Agent 行为保留。 |
| `apps/web/src/types.ts` | 增加接口已有的可选 `Membership.joined_at`、`Audit.details` 字段。其他任务的类型改动保留。 |
| `apps/web/src/api.ts` | 仅对 `POST /support-grants` 返回 `support_role_required` 作业务错误处理，避免把目标支持人员角色不符误判为 Owner 权限变化并丢弃表单；其他 403 / 401 权限与登录刷新保留。 |
| `apps/web/public/organization-design/` | 下载并保留 40 个原始 SVG，记录尺寸、来源和 SHA-256；不是截图贴图或相似图标代替。 |
| `apps/web/tests/organization.spec.ts`、`organization_acceptance_server.py` | 六项真实 API 验收；独立迁移的临时 SQLite 数据库，未拦截请求或模拟响应。 |

字体沿用已经获准自托管的 Noto Sans SC、Roboto、Roboto Mono 与许可证，不改变其他模块的全局字体。收起态仅 Logo 点击展开，没有额外展开按钮、`title` 或悬停文案。展开侧栏沿用原 Agent 收起图标；与组织导出的 SVG 仅非渲染 group id 不同，路径、颜色、尺寸完全相同。

没有新增后端接口、分页、删除配额、编辑审计等操作，没有修改后端。成员角色 / 状态版本、Cookie 登录、CSRF、租户切换取消、额度 null / 0 语义、支持审批 1–60 分钟和正文授权默认关闭均保留。

## 截图与视觉结果

证据目录为 `apps/web/.design-to-ui/runs/20261003-organization-review/`。下表链接指向当前本机的真实截图。改造前采用当时已有的独立验收环境，改造后采用新的组织夹具，因此账号 / 动态数据不保证逐项相同。

| 页面 | 改造前 | Figma | 改造后 | 最后差异比例 |
| --- | --- | --- | --- | --- |
| 成员与邀请 | [前](../apps/web/.design-to-ui/runs/20261003-organization-review/before/members.png) | [设计](../apps/web/.design-to-ui/runs/20261003-organization-review/reference-10-112.png) | [后](../apps/web/.design-to-ui/runs/20261003-organization-review/actual-3/members.png) | **0.820% · FAIL** |
| 组织配额 | [前](../apps/web/.design-to-ui/runs/20261003-organization-review/before/quotas.png) | [设计](../apps/web/.design-to-ui/runs/20261003-organization-review/reference-10-113.png) | [后](../apps/web/.design-to-ui/runs/20261003-organization-review/actual-3/quotas.png) | **1.323% · FAIL** |
| 审计日志 | [前](../apps/web/.design-to-ui/runs/20261003-organization-review/before/audit.png) | [设计](../apps/web/.design-to-ui/runs/20261003-organization-review/reference-10-114.png) | [后](../apps/web/.design-to-ui/runs/20261003-organization-review/actual-3/audit.png) | **1.224% · FAIL** |
| Owner 支持审批 | [前](../apps/web/.design-to-ui/runs/20261003-organization-review/before/support.png) | [设计](../apps/web/.design-to-ui/runs/20261003-organization-review/reference-10-115.png) | [后](../apps/web/.design-to-ui/runs/20261003-organization-review/support-resource-final/support.png) | **1.373% · FAIL** |

比较阈值保持 `threshold=0.1`、`maxDiffRatio=0.005`。1280 × 1024、DPR=1、本机 Chrome `154.0.8037.93`；每次截图等待 `document.fonts.ready` 和所有图片 `decode()`，禁用动画和 caret。没有修改 Figma 原图、阈值或基准。

初始四页差异约 1.884% / 2.055% / 1.952% / 2.251%；依据差异裁片、DOM 盒子及字体证据修复两轮后为上表结果。支持页在功能修复时补换为原稿白色 SVG 加号，单独重拍比较并保留报告。设置的字体检查全部通过；实际字体审计识别本地 Noto Sans SC 兼容内部名 `Noto Sans SC Thin` 和 Roboto，不以 CSS 声明代替实际渲染证据。

[三轮比较报告](../apps/web/.design-to-ui/runs/20261003-organization-review/report-3/report.html)；[支持图标修正后的报告](../apps/web/.design-to-ui/runs/20261003-organization-review/report-support-final/report.html)。报告有参考 / 实际 / 差异、区域裁片、DOM 字体记录。

剩余差异包括：侧栏下方分组的纵向位置（支持页局部约 8px）、表格列宽与部分文字基线 / 标签尺寸；审计和授权真实账号、租户 ID、日期也不同于静态稿。插件固定 UTC 截图，应用仍按浏览器真实时区显示，不为匹配稿子硬编码时间。抗锯齿已由算法排除，剩余差异不能全部归因于抗锯齿。

共用组件影响检查额外重拍 Agent **列表 / 详情 / 创建**，与上一轮未批准的网页截图逐一比较，三页差异均为 **0 像素**；证据为 `agent-impact/`、`agent-impact-diff/report.json`。这是影响检查，不是批准或更新视觉基准。其他模块并行改造后的视觉状态未由本次任务验收。

## 功能验证与边界

`tests/organization.spec.ts` 最终 **6 passed（5.9s）**，截图在 `functional-5/`：

1. 成员列表、本地搜索、标签页、Logo 收起 / 展开、真实邀请 `POST 201`、邀请链接与记录。
2. 角色修改 `PATCH 200` 并恢复原角色；所有权目标选择、确认和取消，Owner 未变化。
3. 真实配额 `PUT 200`、RPM=0、其他字段保留，再恢复；套餐上限只读且没有编辑按钮。
4. 审计筛选、查看已返回记录的详情、无附加信息状态和靠右抽屉。
5. 支持人员角色不符 `403`，错误反馈并保留输入；真实授权 `201`、正文授权默认 false；确认撤销 `DELETE 200`，状态变为已撤销。
6. 真实网络断开后的错误提示与重试恢复；390 × 844 页面无横向溢出；普通成员不显示组织管理页，四类管理接口返回 `403`。

`npm run build`、`npx tsc -b --pretty false`、`git diff --check` 最后均通过。`npm run lint` 实际执行失败：项目没有 `lint` script，也没有现成 lint 配置，**lint 未验证**，未为视觉改造增设全局规则。

四个默认画板以外，没有读取到配套邀请 / 编辑 / 确认 / 请求失败 / 套餐详情 / 审计抽屉 / 移动端状态稿。上述状态沿用组件和业务规则实现，标记为推断，截图不是 Figma 验收通过。未实际执行所有权转移交易；未穷举所有角色、组织暂停 / 关闭、跨租户竞态、配额全部上界或服务端所有失败码。没有用构建成功替代这些验证。

## 运行与复验

当前独立验收预览：[成员与邀请](http://127.0.0.1:15180/#users)、[组织配额](http://127.0.0.1:15180/#quotas)、[审计日志](http://127.0.0.1:15180/#audit)、[Owner 支持审批](http://127.0.0.1:15180/#support-access)。API 端口 `18035`。临时 Owner 账号 `owner@example.test`，密码 `Agent-ui-acceptance-2026`。这些账号仅存在于独立测试数据库；预览含已执行邀请和撤销记录。

重新启动必须指定一个**尚不存在**的夹具目录：

```sh
cd /Users/jie/Github/Z
PYTHONPATH=/Users/jie/Github/Z .venv/bin/python agent_platform/apps/web/tests/organization_acceptance_server.py --directory /tmp/organization-ui-review-new --port 18035
```

另一个终端：

```sh
cd /Users/jie/Github/Z/agent_platform/apps/web
PLATFORM_API_PROXY=http://127.0.0.1:18035 npm run dev -- --port 15180 --strictPort
```

功能检查（建议新夹具，否则邀请数量、授权数量已变化）：

```sh
PLATFORM_ORGANIZATION_ACCEPTANCE=1 PLATFORM_E2E_BASE_URL=http://127.0.0.1:15180 PLATFORM_E2E_BROWSER_CHANNEL=chrome npx playwright test tests/organization.spec.ts
```

`visual.config.json` 保留实际截图时的旧预览端口 5180；复验时复制为新配置，只改 `baseUrl` 为当前预览端口，保留参考图、节点、阈值和字体事实。从相同干净夹具重拍，避免功能测试新写入的审计 / 授权记录改变默认页面状态。

## Design to UI 的实际执行和可用性

实际插件为 **Design to UI 0.2.0**，来自 `/Users/jie/.codex/plugins/cache/jie-design-tools/design-to-ui/0.2.0/plugin.json`；Codex 配置 `design-to-ui@jie-design-tools` 为 `enabled=true`，入口技能已加载并执行。没有更改插件缓存。

真实步骤：入口技能 → Git / 规范 / 契约检查 → CLI `parse` 解析 `10:111` → 先加载 Figma 必需的设计实现技能 → 宿主 Figma `get_design_context` 读取 Section 与四个 Frame → `get_screenshot` 获取原尺寸参考图 → 按工具返回 URL 下载 SVG → 项目原生组件实现 → 插件 CLI `capture` / `compare` 三轮 → 依据功能修复单独 `--case support` 重拍 → 六项真实 API 测试和 Agent 共用组件影响检查 → 构建、类型检查、报告。

`doctor` 实际结果：Node `v25.6.0`、Codex CLI `0.159.0-alpha.12.1`、已登录，**独立 CLI 的 `figmaListed=false`**。宿主 Figma 工具实际可读本文件；不能据此宣称独立 `run` 入口已端到端可用。本次没有运行嵌套 `codex exec` 或声称独立 `run` 成功。

| 遇到的问题 | 实际处理 |
| --- | --- |
| Section 不是单页设计上下文 | 钻取四个实际 Frame，保存原始上下文、生成参考代码、PNG、资源清单。 |
| Agent 样式加载顺序覆盖组织导航、弹窗尺寸 | 提高组织路由选择器精度，保留原 Agent 页默认样式；重拍 Agent 三页零变化。 |
| Owner 混合中英文字体检查失败 | 按原稿拆分中英文标题片段，分别验证 Roboto / Noto 实际渲染，不替换设计字体事实。 |
| 支持校验 403 导致表单丢失 | 对目标支持人员角色错误做精确分支处理，真实失败后保留输入，再完成成功审批与撤销测试。 |
| 自动化选择器不精确 | 修正关闭按钮名称及精确 label 匹配；失败证据保留，最终六项通过。 |
| 多轮登录触发真实 429 | 使用新的独立数据库继续验收，未关闭或修改后端限流。 |
| 8036 / 5181 被并行任务占用 | 失败即停止该启动，改用 18035 / 15180；未终止未知进程。 |
| 视觉差异未达标、lint 缺失 | 保留 FAIL / 未验证结论；未替换参考、调高阈值或更新基准。 |

结论：插件的**宿主会话读取 → 原始资源 → 项目实现 → Chrome 截图 / 字体 / 差异报告**在本项目可用；独立 CLI Figma 连接尚未验证，自动视觉对齐尚不能保证达标，仍需要按差异报告继续校准和人工评审。

本次代码提交位于 `codex/organization-ui-20261003` 分支，基于已推送的 `codex/agent-resources-ui-20261003`，只增加组织管理页面及必要共享接线；不包含工作空间改造。证据目录被 `.gitignore` 保护，仅保存在本机。
