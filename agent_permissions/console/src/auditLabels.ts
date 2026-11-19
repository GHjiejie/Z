import type { AuditOperation, AuditOutcome } from "./auditRecords";
import { permissionLabels } from "./components";

const auditClock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

export function auditTime(stamp: string) {
  return auditClock.format(new Date(stamp));
}

const operationLabels: Readonly<Partial<Record<string, string>>> = {
  "auth.login": "登录",
  "auth.logout": "退出当前登录",
  "user.list": "查询用户列表",
  "user.read": "查看用户详情",
  "user.self.read": "查看自己的资料",
  "user.self.permissions": "查看自己的权限",
  "role.list": "查询角色列表",
  "role.read": "查看角色详情",
  "role.options": "查询角色选项",
  "permission.list": "查看权限目录",
};

export function auditOperationLabel(operationId?: string) {
  if (!operationId) return "工具调用";
  return (
    auditLabel(operationLabels, operationId) ??
    auditLabel(permissionLabels, operationId) ??
    operationId
  );
}

export function auditLabel(
  labels: Readonly<Partial<Record<string, string>>>,
  value: string,
) {
  return Object.hasOwn(labels, value) ? labels[value] : undefined;
}

export const auditOutcomeLabels: Record<AuditOutcome, string> = {
  succeeded: "成功",
  failed: "失败",
  not_executed: "未执行",
  unknown: "未知",
};

const workflowLabels: Readonly<Partial<Record<string, string>>> = {
  requested: "已申请",
  executing: "已派发",
  waiting: "等待确认",
  confirmed: "已确认",
  rejected: "已拒绝",
  cancelled: "已取消",
  expired: "已过期",
  needs_reconciliation: "待核对",
};

export function auditStatus(operation: AuditOperation) {
  return (
    auditLabel(workflowLabels, operation.status) ??
    auditOutcomeLabels[operation.business_outcome]
  );
}
