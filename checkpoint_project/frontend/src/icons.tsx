import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

const Icon = ({ children, ...props }: IconProps) => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    {...props}
  >
    {children}
  </svg>
);

export const PlusIcon = (props: IconProps) => (
  <Icon {...props}><path d="M12 5v14M5 12h14" /></Icon>
);

export const SendIcon = (props: IconProps) => (
  <Icon {...props}><path d="m22 2-7 20-4-9-9-4Z" /><path d="M22 2 11 13" /></Icon>
);

export const BranchIcon = (props: IconProps) => (
  <Icon {...props}><circle cx="6" cy="5" r="2" /><circle cx="18" cy="6" r="2" /><circle cx="6" cy="19" r="2" /><path d="M6 7v10M8 9h5a5 5 0 0 0 5-1" /></Icon>
);

export const ClockIcon = (props: IconProps) => (
  <Icon {...props}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></Icon>
);

export const ShieldIcon = (props: IconProps) => (
  <Icon {...props}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" /><path d="m9 12 2 2 4-4" /></Icon>
);

export const MenuIcon = (props: IconProps) => (
  <Icon {...props}><path d="M4 7h16M4 12h16M4 17h16" /></Icon>
);

export const CloseIcon = (props: IconProps) => (
  <Icon {...props}><path d="m6 6 12 12M18 6 6 18" /></Icon>
);

export const DatabaseIcon = (props: IconProps) => (
  <Icon {...props}><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></Icon>
);

export const FileIcon = (props: IconProps) => (
  <Icon {...props}><path d="M6 2h8l4 4v16H6Z" /><path d="M14 2v5h5M9 13h6M9 17h4" /></Icon>
);

export const SparkIcon = (props: IconProps) => (
  <Icon {...props}><path d="m12 3 1.4 4.1L17 9l-3.6 1.9L12 15l-1.4-4.1L7 9l3.6-1.9Z" /><path d="m18.5 15 .8 2.2 1.7.8-1.7.8-.8 2.2-.8-2.2L16 18l1.7-.8Z" /></Icon>
);

export const RefreshIcon = (props: IconProps) => (
  <Icon {...props}><path d="M20 7v5h-5" /><path d="M19 12a7 7 0 1 1-2-5l3 3" /></Icon>
);
