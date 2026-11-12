import {
  useEffect,
  useId,
  useRef,
  useState,
  type InputHTMLAttributes,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Eye,
  EyeOff,
  LoaderCircle,
  MoreHorizontal,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { ApiError, errorMessage, type User } from "./api";

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className="brand">
      <span className="brand-mark" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      <div>
        <strong>Agent Console</strong>
        {!compact && <small>身份与访问管理</small>}
      </div>
    </div>
  );
}
export function Spinner({ label = "正在加载" }: { label?: string }) {
  return (
    <span className="loading-inline" role="status">
      <LoaderCircle size={16} className="spin" aria-hidden="true" />
      {label}
    </span>
  );
}
export function Avatar({
  user,
  large = false,
}: {
  user: User;
  large?: boolean;
}) {
  return (
    <span
      className={`avatar${large ? " avatar-large" : ""}`}
      aria-hidden="true"
    >
      {Array.from(user.display_name)[0]?.toUpperCase() || "U"}
    </span>
  );
}
export function RoleBadge({ admin }: { admin: boolean }) {
  return (
    <span className={`role-badge${admin ? " role-admin" : ""}`}>
      {admin && <ShieldCheck size={16} aria-hidden="true" />}
      {admin ? "管理员" : "普通用户"}
    </span>
  );
}
export function StatusBadge({ status }: { status: User["status"] }) {
  return (
    <span className={`status-badge status-${status}`}>
      <span />
      {status === "active" ? "已启用" : "已禁用"}
    </span>
  );
}
export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label>
        {label}
        {children}
      </label>
      {error ? (
        <small className="field-error" role="alert">
          {error}
        </small>
      ) : (
        hint && <small className="field-hint">{hint}</small>
      )}
    </div>
  );
}
export function PasswordField({
  label,
  hint,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string }) {
  const [visible, setVisible] = useState(false);
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className="password-control">
        <input {...props} id={id} type={visible ? "text" : "password"} />
        <button
          type="button"
          className="icon-button"
          aria-label={`${visible ? "隐藏" : "显示"}${label}`}
          onClick={() => setVisible((value) => !value)}
        >
          {visible ? <EyeOff size={16} /> : <Eye size={16} />}
        </button>
      </div>
      {hint && <small className="field-hint">{hint}</small>}
    </div>
  );
}
export function ErrorBanner({
  error,
  onRetry,
  onReload,
}: {
  error: unknown;
  onRetry?: () => void;
  onReload?: () => void;
}) {
  const conflict =
    error instanceof ApiError && error.code === "VERSION_CONFLICT";
  return (
    <div className="error-banner" role="alert">
      <AlertCircle size={18} aria-hidden="true" />
      <div>
        <strong>{errorMessage(error)}</strong>
        {error instanceof ApiError && error.details.length > 0 && (
          <ul>
            {error.details.map((detail, index) => (
              <li key={`${detail.field}-${index}`}>
                {/^[\x00-\x7F]*$/.test(detail.message)
                  ? "请检查表单中的字段格式与填写要求"
                  : detail.message}
              </li>
            ))}
          </ul>
        )}
        {conflict && onReload && (
          <button type="button" className="text-button" onClick={onReload}>
            <RefreshCw size={16} />
            载入最新信息
          </button>
        )}
        {onRetry && (
          <button type="button" className="text-button" onClick={onRetry}>
            重试
          </button>
        )}
      </div>
    </div>
  );
}
export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-symbol">
        <AlertCircle size={18} />
      </div>
      <h3>{title}</h3>
      {description && <p>{description}</p>}
      {action}
    </div>
  );
}
export function Modal({
  title,
  description,
  busy,
  onClose,
  children,
  footer,
  destructive = false,
}: {
  title: string;
  description?: string;
  busy: boolean;
  onClose: () => void;
  children: ReactNode;
  footer: ReactNode;
  destructive?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  useEffect(() => {
    const dialog = ref.current;
    const previous = document.activeElement;
    dialog?.showModal();
    return () => {
      dialog?.close();
      queueMicrotask(() => {
        if (
          previous instanceof HTMLElement &&
          previous.isConnected &&
          !document.querySelector("dialog[open]")
        )
          previous.focus();
      });
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={`modal${destructive ? " modal-danger" : ""}`}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      aria-busy={busy}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <div className="modal-surface">
        <header className="modal-header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && <p id={descriptionId}>{description}</p>}
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="关闭弹窗"
            disabled={busy}
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </header>
        <div className="modal-body">{children}</div>
        <footer className="modal-footer">{footer}</footer>
      </div>
    </dialog>
  );
}

export interface MenuAction {
  label: string;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
  reason?: string;
}
export function ActionMenu({
  user,
  actions,
}: {
  user: User;
  actions: MenuAction[];
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const menuId = useId();
  useEffect(() => {
    if (!open) return;
    menu.current
      ?.querySelector<HTMLButtonElement>("button:not(:disabled)")
      ?.focus();
    function outside(event: PointerEvent) {
      if (
        event.target instanceof Node &&
        !root.current?.contains(event.target) &&
        !menu.current?.contains(event.target)
      )
        setOpen(false);
    }
    document.addEventListener("pointerdown", outside);
    const close = () => setOpen(false);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("pointerdown", outside);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [open]);
  return (
    <div
      className="action-menu"
      ref={root}
      onBlur={(event) => {
        if (
          !event.currentTarget.contains(event.relatedTarget) &&
          !menu.current?.contains(event.relatedTarget)
        )
          setOpen(false);
      }}
    >
      <button
        ref={trigger}
        className="icon-button"
        aria-label={`更多操作：${user.username}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => {
          const rect = trigger.current?.getBoundingClientRect();
          if (rect) {
            const height = actions.length * 38 + 14;
            setPosition({
              left: Math.max(
                8,
                Math.min(window.innerWidth - 168, rect.right - 160),
              ),
              top:
                window.innerHeight - rect.bottom > height + 10
                  ? rect.bottom + 6
                  : Math.max(8, rect.top - height - 6),
            });
          }
          setOpen((value) => !value);
        }}
      >
        <MoreHorizontal size={18} />
      </button>
      {open &&
        createPortal(
          <div
            ref={menu}
            id={menuId}
            className="menu-popover"
            style={position}
            role="menu"
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setOpen(false);
                trigger.current?.focus();
              }
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                const items = Array.from(
                  event.currentTarget.querySelectorAll<HTMLButtonElement>(
                    "button:not(:disabled)",
                  ),
                );
                const index = items.findIndex(
                  (item) => item === document.activeElement,
                );
                items[
                  (index +
                    (event.key === "ArrowDown" ? 1 : -1) +
                    items.length) %
                    items.length
                ]?.focus();
              }
            }}
          >
            {actions.map((action) => (
              <button
                key={action.label}
                type="button"
                role="menuitem"
                className={action.danger ? "danger-text" : ""}
                disabled={action.disabled}
                title={action.reason}
                onClick={() => {
                  setOpen(false);
                  trigger.current?.focus();
                  action.onClick();
                }}
              >
                {action.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
}

export interface ToastValue {
  id: number;
  text: string;
}
export function Toast({
  value,
  onDismiss,
}: {
  value: ToastValue | null;
  onDismiss: () => void;
}) {
  useEffect(() => {
    if (!value) return;
    const timer = window.setTimeout(onDismiss, 5000);
    return () => window.clearTimeout(timer);
  }, [value, onDismiss]);
  if (!value) return null;
  return (
    <div className="toast" role="status">
      <CheckCircle2 size={18} />
      <span>{value.text}</span>
      <button className="icon-button" aria-label="关闭提示" onClick={onDismiss}>
        <X size={16} />
      </button>
    </div>
  );
}
export const permissionLabels: Record<string, string> = {
  "user.self.read": "查看自己的信息与权限",
  "user.self.update": "更新自己的资料",
  "user.self.password.change": "修改自己的密码",
  "user.create": "新增用户",
  "user.read": "查询系统用户",
  "user.update": "编辑用户资料",
  "user.delete": "删除用户",
  "user.status.update": "启用或禁用用户",
  "user.password.reset": "重置用户密码",
  "user.roles.assign": "查看与分配系统角色",
};
export function PermissionList({ permissions }: { permissions: string[] }) {
  return (
    <ul className="permission-list">
      {permissions.map((permission) => (
        <li key={permission}>
          <Check size={16} aria-hidden="true" />
          <span>{permissionLabels[permission] || permission}</span>
        </li>
      ))}
    </ul>
  );
}
export function formatDate(value: string | null, dateOnly = false): string {
  if (!value) return "从未登录";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(dateOnly ? {} : { hour: "2-digit", minute: "2-digit", hour12: false }),
  }).format(new Date(value));
}
