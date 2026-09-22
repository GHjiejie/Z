# Incident runbook / 故障响应手册

This is synthetic test material, not a production policy.

S1 级故障必须在 5 分钟内由值班工程师确认，并每隔 15 分钟更新一次处理进展。
The on-call engineer must acknowledge an S1 incident within 5 minutes and publish a progress update every 15 minutes.

值班工程师是初始事件指挥人，必要时交接给服务负责人。每次交接都要记录当前影响和下一步操作。
The on-call engineer initially acts as incident commander and may hand over to the service owner. Every handover records current impact and next actions.

通过功能开关完成回退后，持续观察至少 10 分钟，确认错误率回到正常范围后再宣布恢复。
After rolling back through a feature flag, monitor for at least 10 minutes and confirm the error rate returns to normal before declaring recovery.

故障复盘报告应在恢复后的 48 小时内完成，包含时间线、根因、用户影响和改进事项。
Complete the incident review within 48 hours of recovery, including the timeline, root cause, user impact and follow-up actions.
