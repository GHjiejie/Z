# Atlas service handbook / Atlas 服务手册

This is synthetic test material, not a production policy.

Atlas 的默认请求超时为 30 秒，最多自动重试 3 次。重试等待依次为 1 秒、2 秒和 4 秒。
Atlas requests time out after 30 seconds by default. They are retried at most 3 times, with delays of 1, 2 and 4 seconds.

查询任务状态使用 GET /api/v1/tasks/{task_id}。任务标识可从创建任务的响应读取。
Use GET /api/v1/tasks/{task_id} to retrieve task status. The task ID comes from the create-task response.

错误码 ATLAS-TIMEOUT 表示上游服务没有在超时时间内返回。检查网关日志和上游延迟后，再确认是否需要重试。
ATLAS-TIMEOUT means the upstream service did not respond before the deadline. Check gateway logs and upstream latency before retrying.

Atlas 由平台工程组维护；版本发布窗口为周二 10:00–12:00。
The Platform Engineering team maintains Atlas. The release window is Tuesday 10:00–12:00.
