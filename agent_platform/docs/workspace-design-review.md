# 工作空间设计实现与评审记录

本次依据新链接的 `10:103` 工作空间 section，调整概览与我的运行记录。真实 API 功能验收通过；最终像素比较仍为 **FAIL**，概览 **0.784%**、运行记录 **0.536%**，超过插件默认 **0.5%** 阈值。字体和原始资源尺寸检查通过。没有批准或更新视觉基准，没有提交、推送、部署或修改后端。

## 范围与已有工作保护

- 分支保持 `master`，HEAD 保持 `366b589`。沿用 React、TypeScript、Vite、Button、Modal、ResourceState、useResource，以及已有 Cookie / CSRF / 组织切换机制。
- 已保留上一轮 Agent 管理工作、根目录原有 `index.html`、本机 `.design-to-ui/` 证据。工作期间其他任务同时修改组织管理、资源与费用模块及其共用集成代码；这些改动未回退，也不计入本次交付范围。
- 本次新增 `WorkspaceOverview.tsx`、`WorkspaceRuns.tsx`、`WorkspaceDesign.ts`、`workspace-design.css`、`workspace-runs.css`、`public/workspace-design/`、两份 workspace 验收文件，以及本报告。
- `App.tsx` 本次仅接入两个工作空间页面、标题、图标和限定外壳；合并时保留其他任务的组织/资源分支。`types.ts` 本次补充 Run 的 `finished_at`、`message` 可选字段；其他并行类型变更保留。
- `AgentSidebar` 本次补充可选 `iconsDirectory` 参数，默认沿用已有组织图标目录。工作空间传入自己的原始图标目录；Agent 原始图标路径和品牌保持不变。样式均限定于 workspace 外壳或专用类。

## 已读取的设计

| 节点 | 内容 | 视口 |
| --- | --- | --- |
| [10:103](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-103) | 工作空间 section | 2820 × 1184 |
| [10:104](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-104) | O01 · 概览 · 组织汇总 · 已核验 | 1280 × 1024 |
| [10:105](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-105) | R01 · 我的运行记录 · 已核验 | 1280 × 1024 |

概览初次上下文较稀疏，继续读取了侧栏 `16:428`、头部 `17:586`、标题 `17:656`、指标 `17:729`、趋势与模型分布 `17:907`、近期运行 `17:1060`。节点实际数据、完整工具响应、生成的参考 TSX、原图和资源 manifest 保存在本次证据目录：

[/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103)

原始资源共 21 个 SVG，下载到本地并记录 SHA256、原始 width / height / viewBox、对应工具响应和资源槽位。图表样例曲线 `77296.svg` 仅保留为参考材料；生产折线由真实 `daily` 数据生成，没有用参考图片替代数据。

复用已有自托管 Noto Sans SC、Roboto、Roboto Mono。原稿的图表文字另使用 Inter；在既有“允许自托管设计字体”的授权下，从 Google Fonts 官方仓库 contents API 获取 Inter 原文件和 OFL 许可证。原文件 Git blob SHA 与官方 API SHA 相符，字体包含 `opsz 14–32`、`wght 100–900`，实际 Chrome 渲染观察到 `Inter-Regular`。未在生产代码留下临时 Figma URL。

## 业务行为

概览读取既有 `/dashboard`，展示累计调用、Tokens、客户费用、已确认调用占比、活跃运行、每日调用趋势和模型分布。有 `usage.read_all` 时展示当前组织汇总，否则为本人范围。已确认占比沿用模型调用 confirmed / settled 口径，不改成运行成功率；趋势仅包含接口返回的最近最多 30 个有调用日期，按 Asia/Shanghai 显示，没有补造缺失日期或称为近 30 天总计。

近期运行调用 `/runs?limit=3`，始终为本人记录。我的运行记录调用 `/runs?limit=50`，仅按后端 `next_cursor` 追加并按 ID 去重；没有页码、虚构总条数、服务端搜索或日期过滤。搜索明确只筛选已加载记录，刷新重置游标，追加失败保留已有行并允许重试，游标为空时禁用加载更多。

详情使用真实 `GET /runs/{id}`；取消仅调用既有 `POST /runs/{id}/cancel`，受 `runs.execute` 和运行状态限制。queued → cancelled、running → cancelling；后者展示等待停止并每 5 秒更新，未宣称立即终止。取消响应合并已有详情字段，避免 PublicRun 响应缺少费用/消息时短暂丢失已读取内容。查看会话沿用既有会话路由。

运行详情、取消反馈、网络错误、空态、成员权限和移动布局没有对应状态画板，依据现有组件和接口契约实现，均标记为推断状态。保留财务角色的原导航范围与私有运行 403 拒绝，没有添加后端不支持的操作。

共用侧栏维持 64px 头部、240px 展开宽度和 64px 收起宽度。收起时只由平台 Z Logo 点击展开，没有额外展开按钮或悬停文案；跨概览、运行记录、Agent 页面保留展开/收起状态。

## 截图与视觉结果

原稿和页面均在 1280 × 1024 视口捕获。插件等待字体与图片解码后禁用动画并隐藏 caret，记录 Chrome `154.0.8037.93`、macOS arm64、DOM 盒子、资源尺寸、实际渲染字体、原像素区域裁片与差异图。

| 页面 | 改造前 | Figma 参考 | 最终截图 | 最终差异 |
| --- | --- | --- | --- | --- |
| 概览 | [before/overview.png](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/before/overview.png) | [10:104 原图](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/reference-10-104.png) | [overview.png](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/actual-4-resource-check/overview.png) | 0.784% · FAIL |
| 我的运行记录 | [before/runs.png](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/before/runs.png) | [10:105 原图](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/reference-10-105.png) | [runs.png](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/actual-4-resource-check/runs.png) | 0.536% · FAIL |

初次差异为 1.311% / 1.040%，两轮修正后为 0.784% / 0.546%。两轮完成后，资源尺寸审计又发现浏览器把原始 540.129 × 0.964516 的细线 SVG 按比例缩短约 6.4px；随后作一次限定资源尺寸与刷新按钮 CSS 优先级修正并独立补拍复验，形成 `actual-4-resource-check` / `report-4-resource-check`。每轮原图、配置和阈值均保留，未覆盖参考图。

最终报告：[HTML](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/report-4-resource-check/report.html)、[JSON](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/report-4-resource-check/report.json)。字体检查均 PASS；资源槽位尺寸与 SHA 检查 PASS，且共用收起图标与原 Agent 图标字节一致。见 [资源审计](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/asset-slot-audit-final.json)、[字体审计](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/font-resource-audit.json)。

剩余差异分为以下几类，不能统一归因于抗锯齿：

- 原稿趋势轴本身不满足单一线性刻度：250→150、150→50 间距约 38.58px，50→0 却为 28.935px，而应为 19.29px。页面保留真实自洽的线性轴，因此网格与原稿相差约 9.645px；验收库最后一天为 132 次，曲线也与样例终点不同。
- 右上角保留真实组织菜单“工作空间”，而非写死“演示数据”；菜单外观为 66 × 26，原稿标记为 62 × 24。后端无下一游标时加载更多处于真实禁用状态。
- 混合中英文字的基线、状态徽标文字、Run ID 渲染、品牌文字度量和部分表格边界仍有细小差异。两张内部表格有约半像素的 border-collapse 偏移；尚未逐项消除。

入口技能写明：“脚本 `run` 默认最多修复两轮，达到预算后报告剩余差异和具体资源/视觉决定”。来源：[Design to UI SKILL.md](/Users/jie/.codex/plugins/cache/jie-design-tools/design-to-ui/0.2.0/skills/design-to-ui/SKILL.md)。本次手动完成两轮像素修正，并在独立资源检查发现失败后补修和复验；没有启动无限修复循环。上述 FAIL 供评审，不是批准申请，也没有执行 `approve` 或更新基准。

共用影响检查：同一夹具、同一视口下，Agent 列表 [改造前](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/before/agents.png) 与 [改造后](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/impact/agents.png) 的严格像素差异及排除抗锯齿差异均为 **0**。该检查是前后影响证据，不是批准的视觉基准；没有逐页验收其他并行任务的模块。

## 功能与工程验证

完整工作空间 E2E **8/8 通过，11.4s**。随后加强取消详情字段的即时保持断言，单项再验 **1/1 通过，11.0s**，不用轮询等待掩盖瞬时丢失。SQLite 夹具经过真实迁移，所有页面 HTTP 来自未修改的真实 API，没有 route.fulfill / HTTP mocking；测试登录获取真实 HttpOnly Cookie，并在独立浏览器上下文复用该 Cookie以减少真实登录限流。

| 覆盖 | 实际核验 |
| --- | --- |
| 概览 | 1280 次 / 1,284,500 Tokens / $12.4800 / 96.4% / 活跃 2；7 个真实日期、最高点、896/384 模型分布；组织汇总与本人近期记录分离。 |
| 成员 | 本人统计范围、本人记录隔离、缺少钱包权限时不造金额、不显示管理员三条记录。 |
| 列表与详情 | 本地搜索不发新列表请求、无匹配状态、详情真实 GET、查看会话路由。 |
| 游标 | 50→60 行、断网追加失败保留 50 行、重试、实际重叠响应按 ID 去重、刷新复位为 50 行。 |
| 取消 | queued→cancelled、running→cancelling、断网取消失败与重试；即时保留模型、费用和输入消息。 |
| 错误与空态 | 列表/概览断网反馈与恢复、空概览和无私有记录。 |
| 权限 | 财务角色私有运行读取与取消被真实 API 403 拒绝。 |
| 布局与共用状态 | 390px 无页面溢出、跨工作空间/Agent 保留侧栏状态、收起时只 Logo 展开、Agent 三行列表仍可用。 |

完整功能截图 11 张：[playwright-workspace](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/playwright-workspace)。取消字段补充截图 2 张：[playwright-cancel-detail-preservation](/Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/playwright-cancel-detail-preservation)。测试只取消独立分页组织的验收运行，之后恢复夹具；主概览组织记录和统计保持不变。

`npm run build`、`npx tsc --noEmit`、`git diff --check`、验收脚本 `py_compile` 通过。`npm run lint` 返回 Missing script；项目没有 lint 脚本/配置，本次未自行增加 lint 工具，也不把此项记为通过。

未验证：用户原有部署、外部模型实际请求/费用结算、worker 把 cancelling 最终推进为 cancelled、所有组织/资源页面的视觉和业务、缺失状态画板的设计一致性、人为视觉批准。独立数据库的模型调用记录是验收夹具，不代表本次实际请求了外部模型。

## 运行方式

本次服务继续运行：前端 [概览](http://127.0.0.1:5181/#overview) / [我的运行记录](http://127.0.0.1:5181/#runs)，代理到独立真实 API `127.0.0.1:8036`。测试账号 `admin@example.test`，密码 `Agent-ui-acceptance-2026`；成员 `member@example.test`、财务 `finance@example.test` 同密码。原 `5178` 服务保留。

使用既有业务 API 时在前端目录运行：

```sh
cd /Users/jie/Github/Z/agent_platform/apps/web
PLATFORM_API_PROXY=http://127.0.0.1:8000 npm run dev -- --port 5181 --strictPort
```

重建独立验收环境需使用尚不存在的新目录，示例采用其他空闲端口：

```sh
cd /Users/jie/Github/Z
PYTHONPATH=/Users/jie/Github/Z .venv/bin/python agent_platform/apps/web/tests/workspace_acceptance_server.py --directory /tmp/workspace-ui-review-new --port 8037
```

另一终端启动 Vite，随后运行测试：

```sh
cd /Users/jie/Github/Z/agent_platform/apps/web
PLATFORM_API_PROXY=http://127.0.0.1:8037 npm run dev -- --port 5182 --strictPort
```

```sh
cd /Users/jie/Github/Z/agent_platform/apps/web
PLATFORM_WORKSPACE_ACCEPTANCE=1 PLATFORM_WORKSPACE_ACCEPTANCE_DIRECTORY=/tmp/workspace-ui-review-new PLATFORM_E2E_BASE_URL=http://127.0.0.1:5182 PLATFORM_E2E_BROWSER_CHANNEL=chrome npx playwright test tests/workspace.spec.ts
```

本次完整测试使用的命令为 `PLATFORM_WORKSPACE_ACCEPTANCE=1 PLATFORM_E2E_BASE_URL=http://127.0.0.1:5181 PLATFORM_E2E_BROWSER_CHANNEL=chrome npx playwright test tests/workspace.spec.ts --output=.design-to-ui/runs/20261003-node10-103/playwright-workspace`。

## 插件实际执行与可用性

实际安装版本 **Design to UI 0.2.0**；本机 plugin.json、package.json 与缓存路径一致，`config.toml` 的 `design-to-ui@jie-design-tools` 为 `enabled=true`，会话实际加载了入口 `design-to-ui:design-to-ui`。同时按 Figma 工具要求读取 `figma:figma-design-to-code` 技能。

本次执行流程为：检查项目规范/Git/已有工作 → 插件 CLI parse 定位 `10:103` → 真实 `figma_get_design_context` 读取 section、具体画板和细分节点 → `figma_get_screenshot` 获取原图 → 下载原始资源并记录槽位 → 审计既有业务代码/接口 → 捕获改造前页面 → 在已有体系实现 → 启动前端和独立真实 API → CLI capture/compare → 查看差异原像素、实际字体、DOM 盒子 → 两轮修正 → 资源检查补修复验 → 真实功能验收及构建检查。

实际捕获/比较入口（配置的 baseUrl、账号和夹具需对应环境）：

```sh
node /Users/jie/.codex/plugins/cache/jie-design-tools/design-to-ui/0.2.0/scripts/cli.mjs capture --config /Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/visual.config.json --project /Users/jie/Github/Z/agent_platform/apps/web --output /tmp/workspace-ui-capture-new
node /Users/jie/.codex/plugins/cache/jie-design-tools/design-to-ui/0.2.0/scripts/cli.mjs compare --config /Users/jie/Github/Z/agent_platform/apps/web/.design-to-ui/runs/20261003-node10-103/visual.config.json --project /Users/jie/Github/Z/agent_platform/apps/web --actual /tmp/workspace-ui-capture-new --output /tmp/workspace-ui-report-new
```

| 遇到的问题 | 处理与实际结果 |
| --- | --- |
| section / 概览上下文稀疏 | 下钻 6 个区域节点取得高保真数据，没有按链接猜布局。 |
| before capture 配置缺少合法 designUrl | 按插件 schema 补齐各画板链接，之后实际成功捕获。 |
| 等待多个趋势点触发 strict selector 错误 | 等待唯一首行数据单元格，确保已加载真实列表及概览，不绕过数据就绪。 |
| 并行模块 CSS 临时缺失、类型错误 | 记录捕获失败并等待原任务完成对应修复；保留其工作，后续捕获和全项目类型检查通过。 |
| 原始 Google Fonts 下载端点 TLS reset / timeout | 改用同一官方公开仓库 contents API，解码原始字节并验证官方 Git SHA；保存 OFL。 |
| 共用样式优先级盖过当前外壳 | 限定 workspace 提高优先级，修复面包屑字重、背景、侧栏子项底部间距及刷新按钮。 |
| Noto 字体实际加载但中文基线偏上 | 核对字体 hhea 度量；纯中文使用 Noto-first，保留字号和块几何。字体检查不只依赖 CSS 声明或 fonts.ready。 |
| 小数高度 SVG 被浏览器缩短 | 指定原始细线 width / height，补拍后全部资源尺寸与 SHA 校验通过，保留初次 FAIL 审计。 |
| 取消响应缺少详情字段 | 合并现有详情与响应；真实取消后立即 DOM 核验费用/模型/消息仍在。 |
| 视觉比较返回非零 | 保留 FAIL 和剩余差异，没有提高阈值、修改参考图或批准基准。 |

判断：插件在本项目宿主会话中可用于真实设计读取、原始资源取得、字体/资源审计、运行截图和差异迭代。本次没有达到自动视觉验收阈值。独立 CLI `run` 的 Figma 连接、完整自动生成流程以及批准基准的回归工作流未验证；不能从宿主连接可用推导为一键全流程已验证。前次 bundled Chromium 安装不完整，本次沿用实际安装的 Google Chrome channel，没有冒称安装修复。

证据目录被项目 gitignore 忽略，保存在当前本机，普通 Git 提交不会自动带走截图、私有数据库与原始工具响应。
