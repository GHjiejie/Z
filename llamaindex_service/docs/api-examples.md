# API 请求样例

默认服务地址为 `http://127.0.0.1:8090`。下面示例面向本机开发身份；JWT 模式在每次请求中加入 `Authorization: Bearer <token>`。所有 ID 从服务响应取得，不允许使用请求体中的 tenant/user 字段改变身份。完整契约见 `/docs` 和 `/openapi.json`。

## 1. 创建知识库与上传

```bash
curl -sS http://127.0.0.1:8090/api/v1/knowledge-bases \
  -H 'Content-Type: application/json' \
  -d '{"name":"研发文档","description":"服务说明与操作手册"}'
```

将响应的 `id` 填入后续路径：

```bash
curl -sS http://127.0.0.1:8090/api/v1/knowledge-bases/KB_ID/documents \
  -H 'Idempotency-Key: guide-upload-1' \
  -F 'file=@llamaindex_service/evaluation/fixtures/atlas-service.md;type=text/markdown'

curl -sS http://127.0.0.1:8090/api/v1/jobs/JOB_ID
```

上传返回 `202`，包含 `document_id/version_id/job_id`。Worker 完成后状态为 `READY`；失败返回 `error_code` 与经过脱敏的错误说明。相同幂等键和内容回放原结果；同键不同内容返回 `409`。

## 2. 检索和问答

```bash
curl -sS http://127.0.0.1:8090/api/v1/retrieval/search \
  -H 'Content-Type: application/json' \
  -d '{"knowledge_base_ids":["KB_ID"],"query":"Atlas 的默认超时是多少？","top_k":8}'

curl -N http://127.0.0.1:8090/api/v1/answers \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: question-1' \
  -d '{"knowledge_base_ids":["KB_ID"],"query":"Atlas 的默认超时是多少？","stream":true}'
```

SSE 顺序为 `retrieval → citations → delta → done`，错误以 `error` 结束。每个事件有序号和 `request_id`。`delta` 是草稿，只有 `done` 表示已完成；最终引用无效、提交时撤权或取消均不会被报告为成功。`usage: null` 表示上游未提供用量。

设置 `stream:false` 得到 JSON。追加 `document_ids:["DOCUMENT_ID"]` 即指定文档问答。过滤器目前支持 `filename` 与 `content_type`。`citations[].download_url` 指向需要鉴权的版本下载接口。

重复问答幂等键返回保存的运行状态/结果 JSON，不重新调用模型，也不会重放 SSE。可通过 `GET /api/v1/answers/REQUEST_ID` 查询结果，仍会复核来源权限。

## 3. 会话与反馈

```bash
curl -sS http://127.0.0.1:8090/api/v1/conversations \
  -H 'Content-Type: application/json' \
  -d '{"knowledge_base_ids":["KB_ID"],"title":"了解服务配置"}'

curl -sS http://127.0.0.1:8090/api/v1/answers \
  -H 'Content-Type: application/json' \
  -d '{"knowledge_base_ids":["KB_ID"],"conversation_id":"CONVERSATION_ID","query":"它允许重试几次？"}'

curl -sS http://127.0.0.1:8090/api/v1/answers/REQUEST_ID/feedback \
  -H 'Content-Type: application/json' \
  -d '{"rating":"positive","category":"answer","comment":"引用位置正确"}'
```

会话绑定知识库集合，后续问题必须使用相同集合。会话仅本人可见；来源被撤权、删除或不再属于活动版本时，对应历史会保守隐藏，不能作为后续问题上下文。

## 4. 权限、替换与删除

知识库权限仅 Owner 可设置，必须保留 Owner。文档权限在知识库权限基础上收窄；空列表表示继承知识库。

```bash
curl -X PUT http://127.0.0.1:8090/api/v1/knowledge-bases/KB_ID/permissions \
  -H 'Content-Type: application/json' \
  -d '{"members":{"developer":"owner","reader-user":"reader"}}'

curl -X PATCH http://127.0.0.1:8090/api/v1/documents/DOCUMENT_ID/permissions \
  -H 'Content-Type: application/json' \
  -d '{"allowed_users":["developer"],"allowed_roles":[]}'

curl http://127.0.0.1:8090/api/v1/documents/DOCUMENT_ID/versions \
  -H 'Idempotency-Key: guide-v2' -F 'file=@updated-guide.md'

curl -X DELETE http://127.0.0.1:8090/api/v1/documents/DOCUMENT_ID
```

替换成功前旧版持续可查。删除提交后新检索立即不可见，返回清理任务；原文、向量的后台清理状态通过任务接口查询。

## 5. 错误

统一错误字段为 `code/message/request_id/details`。典型状态：`401` 身份无效；`403` 已知可见资源的操作权限不足；`404` 不存在或不可见；`409` 幂等冲突、配置错配或状态变化；`413` 上传过大；`422` 参数/格式错误；`429` 限流，带 `Retry-After`。已开始的 SSE 无法改 HTTP 状态，通过 `error` 事件报告失败。
