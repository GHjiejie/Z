# HTML Artifact 后端设计方案

- 状态：已实现并通过后端、前端与浏览器验收
- 日期：2026-08-24
- 适用项目：`checkpoint_project`

## 1. 背景与目标

当前应用使用 LangGraph、FastAPI、SQLite checkpoint 和 SSE 实现持久化对话、工具调用、人工审批、失败恢复与会话分支。现有 SSE 只推送模型文本 token，并在流结束时推送最终会话 state。

本方案为应用增加“模型生成可运行 HTML，并在前端直接预览”的后端能力，要求同时满足：

- 不通过正则或字符串特征猜测模型输出是否为可运行 HTML。
- 由模型理解用户的自然语言意图，并通过明确工具调用表达“建议创建预览页面”；用户
  不需要说出“预览”或内部工具名。
- 在运行任何模型生成的 HTML 前，通过 LangGraph `interrupt()` 让用户明确选择
  “运行实时预览”或“仅查看代码”。
- 后端只校验、保存和分发 HTML，不在服务端执行 HTML 或 JavaScript。
- 前端能够实时收到新页面事件。
- 页面刷新、SSE 断线、checkpoint 历史和 fork 后仍可恢复页面。
- retry 或 LangGraph 节点重放不会重复创建页面。
- 用户拒绝预览或只需要概念/语法时，HTML 仍作为 Markdown 代码块显示，不自动运行。

## 2. 核心决策

采用以下架构：

```text
模型调用 render_html
    → LangGraph interrupt(html_preview_approval)
    ├─ 用户拒绝 → 不创建 artifact → 模型返回说明和 Markdown 代码
    └─ 用户批准
         → 后端校验 HTML
    → 幂等写入 ArtifactStore
    → 写入带 artifact 引用的 ToolMessage
    → 通过 LangGraph custom stream 推送 artifact_ready
    → 最终 state 再次携带 artifact 引用
         → 前端获取 HTML 并在隔离 iframe 中运行
```

不采用以下方式作为正式实现：

- 后端检测回答中是否包含 ` ```html `。
- 后端检测 `<html>`、`<!doctype html>` 或 HTML 标签。
- 将模型返回的任意 HTML 代码块自动运行。
- 把完整 HTML 重复保存进每一个 LangGraph checkpoint。
- 直接在应用主域名下以 `text/html` 响应不可信页面。

## 3. 职责边界

### 3.1 模型

- 识别用户是否希望看到页面、组件、视觉效果、动画或交互的实现/代码/演示。
- 完整可运行演示明显更有帮助时调用 `render_html`，即使用户没有明确提到预览。
- 仅解释语法/概念、明确不要运行或无法组成页面时输出普通 Markdown。
- 用户拒绝实时预览后输出说明和 Markdown 代码，不重复发起预览确认。
- 尽量生成自包含的 HTML、CSS 和 JavaScript。

### 3.2 后端

- 注册 `render_html` 工具。
- 在工具执行前生成精简 `html_preview_approval` payload 并调用 `interrupt()`。
- 验证工具参数。
- 将 HTML 作为不可信文本持久化。
- 生成不可变 artifact 和 ToolMessage 引用。
- 通过 SSE 推送 artifact 事件。
- 提供有会话访问控制的 artifact 查询接口。
- 处理 retry、checkpoint、历史和 fork 一致性。

### 3.3 前端

- 根据 `artifact_ready` 或最终 state 渲染预览卡片。
- 根据 pending approval 渲染“运行实时预览 / 仅查看代码”确认卡。
- 从 artifact API 获取 HTML。
- 使用 sandbox iframe 和固定 CSP 执行 HTML。
- 按 `artifact_id` 对重复事件去重。

## 4. 模型工具契约

### 4.1 输入模型

```python
class RenderHtmlInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    html: str = Field(min_length=1, max_length=262_144)
    parent_artifact_id: str | None = None
```

工具定义：

```json
{
  "name": "render_html",
  "description": "为页面、组件、视觉效果、动画或交互需求创建可运行 HTML。当完整演示明显有帮助时，即使用户未提到预览也使用；仅解释语法/概念时不使用。执行前系统会自动请求用户确认。",
  "parameters": {
    "type": "object",
    "properties": {
      "title": {
        "type": "string",
        "description": "页面标题"
      },
      "html": {
        "type": "string",
        "description": "完整、自包含的 HTML"
      },
      "parent_artifact_id": {
        "type": ["string", "null"],
        "description": "修改已有页面时填写原 artifact ID"
      }
    },
    "required": ["title", "html"]
  }
}
```

### 4.2 系统提示词规则

系统提示词应加入：

```text
主动识别用户的真实意图，不等待用户说出“预览”或 render_html。
当用户请求前端页面、组件、动画、交互及其代码示例，并且完整演示更有帮助时，
生成完整 HTML 并调用 render_html。
不要在对话中先追问；系统会自动触发 Human-in-the-loop 确认。
用户拒绝后提供 Markdown 代码，不重复调用 render_html。
HTML 应尽量自包含，不依赖外部网络资源。
调用成功后只需简短说明页面已经生成，不要再次输出完整 HTML。
```

`render_html` 必须经过人工确认。这里的确认不是因为它修改工作区，而是因为后续会在
浏览器沙箱里运行模型生成的 JavaScript；用户应对“是否运行”保有最终决定权。

## 5. Artifact 数据模型

采用通用 artifact 表，便于未来扩展 SVG、Markdown、React bundle 或图表。

```sql
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    owner_thread_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    parent_artifact_id TEXT,
    created_at TEXT NOT NULL,

    UNIQUE(owner_thread_id, tool_call_id)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_owner
ON artifacts(owner_thread_id, created_at);

CREATE TABLE IF NOT EXISTS session_artifacts (
    thread_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    created_at TEXT NOT NULL,

    PRIMARY KEY(thread_id, artifact_id)
);
```

示例：

```json
{
  "artifact_id": "art_52f8c9d4",
  "owner_thread_id": "main",
  "tool_call_id": "call_xxx",
  "kind": "html",
  "mime_type": "text/html",
  "title": "登录页面",
  "content": "<!doctype html>...",
  "content_sha256": "8bb2...",
  "byte_size": 18240,
  "parent_artifact_id": null,
  "created_at": "2026-08-24T12:30:00+00:00"
}
```

约束：

- Artifact 创建后不可原地修改。
- 修改页面时创建新 artifact，并设置 `parent_artifact_id`。
- LangGraph message/checkpoint 只保存 artifact 引用，不保存 HTML 正文。
- `session_artifacts` 表负责会话访问授权和 fork 后的引用共享。

## 6. ArtifactStore

新增 `artifact_store.py`，包含：

```python
class ArtifactStore:
    def create_or_get(
        self,
        *,
        thread_id: str,
        tool_call_id: str,
        title: str,
        html: str,
        parent_artifact_id: str | None,
    ) -> Artifact: ...

    def get(self, thread_id: str, artifact_id: str) -> Artifact | None: ...

    def list_for_session(self, thread_id: str) -> list[Artifact]: ...

    def grant_to_session(
        self,
        thread_id: str,
        artifact_ids: list[str],
    ) -> None: ...
```

`create_or_get` 必须是幂等操作：

1. 使用 `owner_thread_id + tool_call_id` 查找已有记录。
2. 不存在时计算 SHA-256 并创建 artifact。
3. 已存在且 hash 相同时返回原 artifact。
4. 已存在但 hash 不同时抛出一致性冲突，不覆盖原记录。
5. 创建或取得 artifact 后，确保 `session_artifacts` 中存在授权记录。

这可以覆盖以下故障场景：

```text
artifact 已写入 SQLite
    → LangGraph 尚未写入 checkpoint
    → 进程崩溃
    → 用户 retry
    → 使用同一 tool_call_id 返回原 artifact
```

## 7. LangGraph 集成

### 7.1 工具注册

新增 `artifact_tools.py`，通过 `@tool(response_format="content_and_artifact")` 定义工具。

```python
@tool(response_format="content_and_artifact")
def render_html(
    title: str,
    html: str,
    parent_artifact_id: str | None,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> tuple[str, dict[str, object]]:
    thread_id = config["configurable"]["thread_id"]
    artifact = store.create_or_get(
        thread_id=thread_id,
        tool_call_id=tool_call_id,
        title=title,
        html=html,
        parent_artifact_id=parent_artifact_id,
    )

    get_stream_writer()(
        {
            "type": "artifact_ready",
            "artifact": artifact.public_ref(),
        }
    )

    return f"已创建可预览页面：{title}", artifact.public_ref()
```

### 7.2 预览确认中断

工具节点把 `render_html` 与文件变更一起纳入审批集合，但发送独立的预览确认协议：

```json
{
  "kind": "html_preview_approval",
  "tool": "render_html",
  "title": "鼠标跟随动画",
  "characters": 4231,
  "byte_size": 4518,
  "preview": "<!doctype html>...",
  "tool_call_id": "call_xxx"
}
```

`interrupt(payload)` 必须发生在 ArtifactStore 写入和 `artifact_ready` 事件之前。恢复时：

- `approved=true`：执行 `render_html`，随后产生 artifact 与 SSE 事件。
- `approved=false`：写入 error 状态的 ToolMessage，明确告知模型“仅查看代码”，不创建
  artifact；模型继续生成 Markdown 回答。

### 7.3 工具执行器

现有工具执行器只调用 `selected.invoke(call["args"])`。应调整为接收 `RunnableConfig` 并传入完整 ToolCall：

```python
def execute_tools(
    state: ChatState,
    config: RunnableConfig,
) -> dict[str, list[ToolMessage]]:
    ...
    output = selected.invoke(call, config=config)

    if isinstance(output, ToolMessage):
        results.append(output)
    else:
        results.append(
            ToolMessage(
                content=str(output),
                tool_call_id=call["id"],
                name=name,
            )
        )
```

工具成功后得到：

```python
ToolMessage(
    name="render_html",
    tool_call_id="call_xxx",
    content="已创建可预览页面：登录页面",
    artifact={
        "artifact_id": "art_52f8c9d4",
        "kind": "html",
        "title": "登录页面",
    },
)
```

### 7.4 Stream Mode

将 LangGraph stream mode 从：

```python
stream_mode=["messages"]
```

改为：

```python
stream_mode=["messages", "custom"]
```

API 层处理：

- `messages`：继续转换为 `token`。
- `custom`：只允许预定义的内部事件，并转换为 `artifact_ready`。
- 其他未知 custom event 不直接透传。

## 8. SSE 协议

### 8.1 start

```json
{
  "type": "start",
  "protocol_version": 2,
  "run_id": "run_abc123",
  "thread_id": "main"
}
```

### 8.2 token

```json
{
  "type": "token",
  "run_id": "run_abc123",
  "content": "页面已经生成"
}
```

### 8.3 artifact_ready

```json
{
  "type": "artifact_ready",
  "run_id": "run_abc123",
  "artifact": {
    "artifact_id": "art_52f8c9d4",
    "kind": "html",
    "mime_type": "text/html",
    "title": "登录页面",
    "byte_size": 18240,
    "content_url": "/api/sessions/main/artifacts/art_52f8c9d4"
  }
}
```

### 8.4 state

```json
{
  "type": "state",
  "state": {
    "thread_id": "main",
    "messages": [
      {
        "type": "tool",
        "name": "render_html",
        "content": "已创建可预览页面：登录页面",
        "artifact": {
          "artifact_id": "art_52f8c9d4",
          "kind": "html",
          "title": "登录页面"
        }
      }
    ]
  }
}
```

### 8.5 error

```json
{
  "type": "error",
  "code": "GRAPH_EXECUTION_FAILED",
  "detail": "模型或图执行失败"
}
```

HTML 不逐 token 推送和执行。后端必须等待工具参数完整、校验和持久化成功后，再发送一次 `artifact_ready`。

最终 `state` 是持久化事实来源。即使前端错过 SSE artifact 事件，也能通过最终 state 或刷新会话恢复预览。

## 9. HTTP API

新增：

```text
GET /api/sessions/{thread_id}/artifacts
GET /api/sessions/{thread_id}/artifacts/{artifact_id}
```

获取 artifact：

```json
{
  "artifact_id": "art_52f8c9d4",
  "kind": "html",
  "mime_type": "text/html",
  "title": "登录页面",
  "content": "<!doctype html>...",
  "byte_size": 18240,
  "content_sha256": "8bb2...",
  "parent_artifact_id": null,
  "created_at": "2026-08-24T12:30:00+00:00"
}
```

接口规则：

- 先验证 session 是否存在。
- 再验证 `session_artifacts` 是否授权当前 session 访问该 artifact。
- Artifact 不存在或无权访问时统一返回 404，避免资源枚举。
- 内容以 JSON 返回，不在主站同源直接返回 `text/html`。
- 第一版不提供前端直接创建、修改或删除 artifact 的接口。

## 10. Message 序列化

现有 `_serialize_message()` 需要暴露受控的 artifact 引用：

```python
if isinstance(message, ToolMessage) and message.artifact:
    result["artifact"] = serialize_artifact_ref(message.artifact)
```

只允许序列化以下字段：

```text
artifact_id
kind
mime_type
title
byte_size
parent_artifact_id
content_url
```

不要把任意 `ToolMessage.artifact` 对象直接透传给浏览器，也不要在 state 中包含 HTML 正文。

## 11. Checkpoint、历史与 Fork

### 11.1 Checkpoint

`ToolMessage.artifact` 中只保存引用，因此 checkpoint 可以恢复页面卡片，同时不会重复保存完整 HTML。

### 11.2 历史

Artifact 不可变。历史 checkpoint 始终指向当时创建的 artifact 版本；修改页面会创建新 artifact。

### 11.3 Fork

创建分支时：

1. 从源 checkpoint 的 messages 中收集 artifact ID。
2. 复制原有 message state。
3. 调用 `grant_to_session(new_thread_id, artifact_ids)`。

预期行为：

- 从生成 HTML 之前的 checkpoint fork：新会话看不到该页面。
- 从生成 HTML 之后的 checkpoint fork：新会话可访问当时版本。
- 新分支修改页面：创建新的 artifact，不影响源会话。

## 12. 校验与安全

后端将 HTML 视为不可信文本，至少执行以下校验：

- 标题去除首尾空白，长度不超过 120。
- HTML 非空，UTF-8 大小不超过 256 KiB。
- 拒绝 `\0`。
- 计算并保存 SHA-256。
- `parent_artifact_id` 必须属于当前会话且类型为 HTML。
- 不主动下载 HTML 引用的远程资源。
- 不在服务端执行 JavaScript。
- 不把 HTML 作为主站同源文档直接响应。

后端校验不是执行沙箱。真正执行必须由前端固定配置的 iframe 完成，例如：

```html
<iframe sandbox="allow-scripts"></iframe>
```

建议第一版注入固定 CSP：

```text
default-src 'none';
script-src 'unsafe-inline';
style-src 'unsafe-inline';
img-src data: blob:;
connect-src 'none';
font-src data:;
```

第一版禁止外部网络。以后如需 CDN、图片或 API 请求，应引入明确的资源白名单，而不是允许任意网络访问。

## 13. 错误处理

工具参数或内容错误应作为 ToolMessage 错误返回，让模型有机会自行修正，不应立即终止整个 SSE：

```python
ToolMessage(
    name="render_html",
    tool_call_id=call_id,
    status="error",
    content="HTML 超过 256 KiB，未创建预览。",
)
```

只有数据库不可用、模型服务失败或 LangGraph 执行失败等运行级错误才发送 SSE `error`。

建议错误码：

```text
ARTIFACT_TOO_LARGE
ARTIFACT_INVALID
ARTIFACT_NOT_FOUND
ARTIFACT_ACCESS_DENIED
ARTIFACT_CONFLICT
ARTIFACT_STORAGE_FAILED
GRAPH_EXECUTION_FAILED
```

## 14. 代码改造清单

### 新增文件

- `checkpoint_project/artifact_store.py`
- `checkpoint_project/artifact_tools.py`
- Artifact 相关单元测试或集成测试文件

### 修改文件

- `checkpoint_project/graph.py`
  - 初始化 ArtifactStore。
  - 注册 `render_html`。
  - 对 `render_html` 调用 `interrupt()`，区分批准与仅查看代码。
  - 工具节点接收 RunnableConfig。
  - 使用完整 ToolCall 调用工具。
  - 支持 `messages + custom` stream mode。
  - fork 时复制 artifact 访问授权。
- `checkpoint_project/api.py`
  - 增加 artifact 查询 API。
  - 序列化 ToolMessage artifact 引用。
  - 将 custom stream 转换为 SSE `artifact_ready`。
  - 增加错误码和响应校验。
- `checkpoint_project/test_api.py`
  - 增加预览批准/拒绝、artifact API、SSE、刷新与访问控制测试。
- `checkpoint_project/test_checkpoint_project.py`
  - 增加 retry 幂等、checkpoint 和 fork 测试。
- `checkpoint_project/README.md`
  - 记录 HTML Artifact 的使用方式、安全边界和运行限制。

## 15. 实施顺序

1. 实现 Artifact 数据模型与 ArtifactStore。
2. 实现 `render_html` 工具、意图规则及参数校验。
3. 接入 LangGraph `interrupt()` 与批准/拒绝分支。
4. 接入工具节点、ToolMessage artifact、custom stream 和 `artifact_ready` SSE。
5. 增加 artifact 查询 API。
6. 完成 checkpoint、retry 和 fork 一致性处理。
7. 补齐离线后端测试。
8. 再实现前端 artifact 卡片和 sandbox iframe。

## 16. 验收标准

后端完成时必须满足：

1. 普通问答不会创建 artifact。
2. 可视化前端需求即使未提到预览，也能触发 `html_preview_approval`。
3. 用户批准前不会创建 artifact 或发送 `artifact_ready`。
4. 用户拒绝后不创建 artifact，并继续得到 Markdown 代码。
5. `render_html` 在批准后能创建一条可查询的数据库记录。
6. 批准恢复的 SSE 包含一个可去重的 `artifact_ready` 事件和最终 `state`。
7. 页面刷新后，session state 仍包含 artifact 引用。
8. retry 同一工具调用不会产生重复记录。
9. 超大或非法 HTML 返回工具错误。
10. 其他会话不能读取未授权 artifact。
11. 从生成前 checkpoint fork 时不包含 artifact。
12. 从生成后 checkpoint fork 时可以读取对应 artifact。
13. 分支修改 artifact 不影响源会话。
14. 原有文件审批、故障恢复、checkpoint 和 SSE token 测试继续通过。

## 17. 最终结论

模型负责从自然语言识别可视化意图，`render_html` 工具负责提交完整页面候选，LangGraph
`interrupt()` 负责把是否运行的最终决定交给用户。批准后 ArtifactStore 负责不可变持久化，
ToolMessage 负责进入 checkpoint，custom stream 负责实时通知，最终 state 负责断线和刷新
恢复；拒绝后则安全回退到 Markdown 代码。该方案能够自然融入当前项目的 memory、retry、
time travel 和 fork 机制，并为未来扩展更多可运行 artifact 类型保留空间。
