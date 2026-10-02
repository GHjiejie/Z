# 资源与费用 UI 评审记录

本次依据用户新增的 Figma 节点 `10:106`，实现模型策略、费用中心、调用记录、导出任务四页。功能验证通过；**视觉对比仍为 FAIL，待评审**。没有批准或更新网页基准，没有提交、推送、部署，没有修改后端源码。

## 范围与已有工作

- 分支保持 `master`。开始时已有 Agent 管理相关未提交修改，已保存差异和源码快照至本轮证据目录。
- 同一工作区同时出现组织管理、工作空间和运行记录的其他会话修改。接入在最新 `App.tsx` 上合并，没有回退这些工作，也没有将它们列为本轮交付。
- 本轮新增 `ResourcesPages.tsx`、`ResourcesUI.tsx`、`resources.css`、41 个原始 SVG、资源模块验收测试与独立测试 API 启动脚本。
- `App.tsx` 接入现有 `models`、`billing`、`usage` 路由。导出使用 `usage?view=exports`，沿用组织路径与权限过滤。
- `PlatformResources.tsx` 仅导出已有 `ModelPolicyEditor`；`OperationsPanel.tsx` 为已有导出面板增加可选设计呈现，默认分支保留。
- CSS 使用资源页面专用类；没有改全局 `styles.css`。Agent CSS 与本轮开始时相同。Agent 源码的侧栏扩展来自同工作区的其他会话，本轮没有覆盖。测试验证 Agent 页没有资源页面类和导航。

## 设计来源

[用户提供的资源与费用 section](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI-%25C2%25B7-Approved-Sidebar-States?node-id=10-106&t=bpNQmFr1kJzywDNM-0)。实际读取 file key：`dojPnHY6X67FVc1Zx7OVCc`。

| 页面 | Figma 节点 | 网页路由 | 设计尺寸 |
| --- | --- | --- | --- |
| M01 模型策略 | [10:107](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-107) | `#models` | 1280×1024 |
| B01 费用中心 | [10:108](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-108) | `#billing` | 1280×1024 |
| U01 调用记录 | [10:109](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-109) | `#usage` | 1280×1024 |
| U02 导出任务 | [10:110](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=10-110) | `#usage?view=exports` | 1280×1024 |

模型策略整帧上下文返回稀疏 XML，继续读取了 [侧栏 18:763](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=18-763) 与 [主内容 18:1053](https://www.figma.com/design/dojPnHY6X67FVc1Zx7OVCc/AgentPlatform-UI?node-id=18-1053)。其余三页读取了完整设计上下文、截图及 metadata。

下载原始 1280×1024 PNG 作为参考，没有拉伸 MCP 内嵌的缩略图。资源清单、SVG 原始尺寸、节点数据与截图保存在证据目录；生产页面只引用本地 SVG，没有临时 Figma 资源 URL。字体复用此前经用户授权自托管的 Noto Sans SC、Roboto、Roboto Mono。

## 功能与接口

页面通过原有 `api()`、Cookie 登录、CSRF、组织作用域、请求取消及 capability 控制访问，没有生产模拟数据。

| 功能 | 真实接口/行为 |
| --- | --- |
| 模型目录与策略 | GET `/models`、`/model-policy`；PUT `/model-policy` 保留 `expected_version`、默认模型及候选顺序 |
| 模型启停 | PATCH `/models/{id}`，仅发送 `active`；确认框不修改价格、不删除或撤回授权 |
| 费用 | GET `/billing/wallet`、`/billing/ledger`、`/billing/reservations`；展示真实余额、可用余额、预占及阻断状态 |
| 调用记录 | GET `/usage/calls?limit=100`；只有 API 返回 `next_cursor` 才追加下一页，去重并防止旧组织响应混入 |
| 调用详情 | 展示当前已返回、当前权限可见的元数据；没有新增后端不支持的详情请求 |
| 导出 | GET/POST `/exports`；POST 保留幂等键；GET `/exports/{id}/download` 返回真实 CSV，保留权限复核、取消及 Blob 清理 |

搜索仅筛选已加载数据；保留原有当前筛选结果 CSV、调用状态筛选和刷新，在加载数量的展开菜单中使用。模型和费用没有添加后端不支持的分页、充值、退款或对账操作。

侧栏保留统一 Z Logo、64px 头部、240px 展开宽度、64px 收起宽度。收起态仅 Logo 按钮展开，没有额外展开按钮或 `title` 悬停文案；键盘 Enter 可展开。

## 无对应状态稿的实现

所读取 section 提供四个主页面，导出页含 running、ready、expired，调用页含 running、confirmed、unresolved。以下状态没有找到独立画板，沿用已有组件和业务逻辑，不声称来自 Figma：

- 策略编辑、候选排序、版本冲突和保存失败；启停模型确认。
- 调用详情、空态、本地筛选无结果、首屏与游标请求失败及重试。
- 预占记录、钱包阻断提示、导出申请、失败反馈与申请成功通知。
- member / finance_viewer 权限、移动端布局及资源页收起侧栏。

这些状态的截图文件名使用 `inferred` 标记。此次范围没有 Agent 发布流程的新增改动。

## 验证结果

测试使用未修改的真实 Python API、新建 SQLite 数据库和持久化夹具；真实 Cookie 登录和请求，没有 `route.fulfill` 数据替代。故障测试仅中断网络以验证错误恢复。未触碰现有业务数据库。

最新 `tests/resources.spec.ts`：**9/9 通过**，覆盖：

1. 策略真实保存与版本递增；409 冲突保留表单输入。
2. 模型搜索、启停确认、PATCH 仅包含 `active`，并恢复启用。
3. 钱包、流水筛选、预占、刷新。
4. 真实 100→103 条游标追加；分页失败保留已有记录并可重试；详情、无结果和当前 CSV 下载。
5. 首屏网络失败后重试恢复。
6. ready CSV 真下载、running/expired 不可下载；导出请求失败保留选择；重试复用幂等键并获得 202。
7. member 模型只读、仅本人调用、导出列表和下载隔离，禁止组织范围导出。
8. finance_viewer 账务/调用可读、模型入口不可见；Logo 展开、移动布局。
9. 调用页直接打开导出申请；Agent 页资源样式隔离。

另外执行现有 `OperationsService.process_batch()`，使真实申请任务 **queued→ready**，生成 1 个 CSV 分块、3 条运行记录；再次经浏览器下载得到 200，校验三条 Run ID。记录见 `maintenance-verification.json`、`generated-download-verification.json` 与 `export-generated-ready.png`。此操作仅运行于独立验收数据库。

`npm run build`（包含 `tsc -b`）、独立 `npx tsc --noEmit`、`git diff --check`、验收脚本的 Ruff 均通过。项目没有前端 lint script / ESLint 配置，**前端 lint 未执行**，未自行添加全项目 lint 配置。离线尝试 Prettier 时发现未缓存，没有下载或引入该依赖。

未验证生产数据、生产部署、真实模型供应商请求、组织切换与权限撤销发生在下载过程中的全部竞态；这些路径保留已有作用域和取消机制。其他模块没有进行完整视觉回归，因为当前工作区存在并行实现；本轮验证其样式作用域隔离。

## 视觉验收

Design to UI capture 使用本机 Chrome 154.0.8037.93、1280×1024、DPR=1。等待字体与图片解码，关闭动画、隐藏 caret，最后将鼠标移到标题，避免登录按钮坐标在新页面意外触发表格 hover。

| 页面 | 首轮差异 | 最终差异 | 字体检查 | 结果 |
| --- | ---: | ---: | --- | --- |
| 模型策略 | 2.0884% | 1.8089% | PASS | FAIL |
| 费用中心 | 1.9109% | 1.4645% | PASS | FAIL |
| 调用记录 | 1.9093% | 0.9719% | PASS | FAIL |
| 导出任务 | 1.5994% | 1.0896% | PASS | FAIL |

阈值一直为 `threshold=0.1`、`maxDiffRatio=0.005`（0.5%）。执行初始捕获和两轮修正；没有放宽阈值、替换参考图、把构建通过当视觉通过，也没有运行 `approve`。

停止本轮视觉修正的依据是 [Design to UI 入口技能](/Users/jie/.codex/plugins/cache/jie-design-tools/design-to-ui/0.2.0/skills/design-to-ui/SKILL.md)：“脚本 `run` 默认最多修复两轮，达到预算后报告剩余差异和具体资源/视觉决定”。本轮配置同样采用两轮修正预算；因此交付剩余差异供评审，不声称视觉工作已通过。

修正了全局表格字色渗入、上下文单位换行、模型策略列宽与高度、侧栏指标、调用页标签栏间距、图标对应关系、按钮和表格字体。检查了最终 report 的原像素裁片、DOM 盒子与 renderedFonts。实际字形来自 NotoSansSC / Roboto / RobotoMono 的对应 PostScript 字重；Noto 字体内部 family 名称为 `Noto Sans SC Thin`，并非回退成系统字体。

剩余视觉差异包含导航行距/字重与基线、模型目录表头及数值位置、部分表格文字字距、边框和图标细节，不能统称为抗锯齿。另有明确业务差异：

- 右上角保留真实组织/账户入口，显示“工作空间”，不伪装成“演示数据”。
- API 未返回资金流水对应的 Run ID，不添加设计示例中的 `run_demo_*` 标签。
- 没有下一页时“加载更多”禁用；模型目录说明使用真实核算语义，移除“示例”措辞。
- 稳定的功能测试默认使用未来有效期；视觉预览数据库使用原稿示例有效期，届时会按真实时间变为 expired。

## 截图与报告

证据根目录：`apps/web/.design-to-ui/runs/20261003-resources-review/`。

- [前后截图与设计参照总览](../apps/web/.design-to-ui/runs/20261003-resources-review/review.html)
- [最终插件差异报告](../apps/web/.design-to-ui/runs/20261003-resources-review/report-3/report.html)
- [视觉配置](../apps/web/.design-to-ui/runs/20261003-resources-review/visual.config.json)
- [设计事实](../apps/web/.design-to-ui/runs/20261003-resources-review/design-source.json)
- `before-final/`：同 API 夹具下的改造前页面；旧 UI 的导出面板原本与调用统计在同页，因此导出 before 截图滚动至该面板。
- `actual-1/2/3` 与 `report-1/2/3`：三轮原始截图、字体/图片审计、差异及裁片。
- `functional-verified/`：最新功能状态截图；测试输出为 9 passed。

## 插件实际执行与可用性

- 实际版本：**Design to UI 0.2.0**；安装目录 `/Users/jie/.codex/plugins/cache/jie-design-tools/design-to-ui/0.2.0`，manifest 与 `config.toml` 中启用状态已核验。
- 入口：读取 `skills/design-to-ui/SKILL.md` 并按宿主会话流程执行；Figma 前置技能也已读取。
- 实际调用 Figma `get_design_context`、`get_screenshot`、`get_metadata`；保存原始工具结果、节点清单、资源清单和原图。不是仅解析链接或猜测页面。
- 执行 CLI `parse`、`doctor`、三轮 `capture` / `compare`，并实际运行前端、真实 API、构建和测试。独立 CLI 的 `doctor` 显示 `figmaListed=false`，因此没有宣称独立 `run` 可读取 Figma；本轮使用已连接的宿主 Figma 工具。
- 问题与处理：整帧稀疏返回后下钻子节点；MCP 缩略图改取原尺寸； bundled Chromium 下载未完成，配置已有 Chrome；发现旧全局表格样式后在模块内隔离；独立夹具初始化的字段/钱包缺失与端口占用修复后重建新库，未改真实后端或杀死其他会话服务。
- 判断：**宿主入口在本项目可用**，能够读取设计、取回资产、实现页面并提供真实字体与像素对比证据；当前没有达到视觉 PASS，不能声称自动实现已完成视觉验收。独立终端入口的 Figma 连接未就绪。

## 运行方式

当前评审服务保留在 [http://127.0.0.1:5217/#models](http://127.0.0.1:5217/#models)，可切换四页。验收账号：`admin@example.test`；密码：`Agent-ui-acceptance-2026`。这是独立测试环境账号。

使用已有真实后端启动前端：

```sh
cd /Users/jie/Github/Z/agent_platform/apps/web
PLATFORM_API_PROXY=http://127.0.0.1:8000 npm run dev -- --port 5217 --strictPort
```

复现隔离功能验收（目录必须是新的；两个服务分别在终端持续运行）：

```sh
cd /Users/jie/Github/Z
PYTHONPATH=/Users/jie/Github/Z .venv/bin/python agent_platform/apps/web/tests/resources_acceptance_server.py --directory /tmp/z-resource-review-new --port 8138 --extra-calls 100

cd /Users/jie/Github/Z/agent_platform/apps/web
PLATFORM_API_PROXY=http://127.0.0.1:8138 npm run dev -- --port 5219 --strictPort
PLATFORM_RESOURCES_ACCEPTANCE=1 PLATFORM_E2E_BROWSER_CHANNEL=chrome PLATFORM_E2E_BASE_URL=http://127.0.0.1:5219 npx playwright test tests/resources.spec.ts
```

视觉夹具使用 `--extra-calls 0 --reference-expiry` 与原稿的样例时间；必须与已有服务错开端口。可用本轮配置执行插件 `capture`、`compare` 复验，仍需单独评审差异；没有已批准的视觉基准。

## 后续提交授权与范围核验（2026-10-03）

用户后续明确授权提交并推送，并选择“仅提交本会话的 Agent 管理、资源与费用”。共用 App.tsx、AgentManagement.tsx、pages.tsx 按模块分离暂存；组织管理、概览、运行记录及根目录 index.html 的其他工作保留在原工作区，没有纳入提交。待提交树在独立目录构建通过，并重新通过 6 项 Agent 和 9 项资源与费用的真实 API 浏览器测试。原始 OFL 许可证仅清理行末空格。视觉验收仍未通过，没有批准或更新基准。
