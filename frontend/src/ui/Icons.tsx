import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };
const base = (size: number, p: P) => ({
  width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
  strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true, ...p,
});

export const SearchIcon = ({ size = 14, ...p }: P) => (
  <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.6} aria-hidden="true" {...p}>
    <circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5 14 14" />
  </svg>
);
export const BellIcon = ({ size = 16, ...p }: P) => (
  <svg {...base(size, p)}><path d="M6 17V11a6 6 0 0112 0v6l1.5 2h-15zM10 21h4" /></svg>
);
export const ThemeIcon = ({ size = 14, ...p }: P) => (
  <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.5} aria-hidden="true" {...p}>
    <circle cx="8" cy="8" r="6" /><path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor" stroke="none" />
  </svg>
);
export const MicIcon = ({ size = 18, ...p }: P) => (
  <svg {...base(size, p)}><path d="M12 3a3 3 0 013 3v6a3 3 0 01-6 0V6a3 3 0 013-3zM6 11a6 6 0 0012 0M12 17v4M9 21h6" /></svg>
);
export const CloseIcon = ({ size = 16, ...p }: P) => (
  <svg {...base(size, p)}><path d="M6 6l12 12M18 6L6 18" /></svg>
);
export const ChevronDown = ({ size = 14, ...p }: P) => (
  <svg {...base(size, p)}><path d="M6 9l6 6 6-6" /></svg>
);
export const ChevronRight = ({ size = 14, ...p }: P) => (
  <svg {...base(size, p)}><path d="M9 6l6 6-6 6" /></svg>
);
export const PlusIcon = ({ size = 14, ...p }: P) => (
  <svg {...base(size, p)}><path d="M12 5v14M5 12h14" /></svg>
);
export const CheckIcon = ({ size = 14, ...p }: P) => (
  <svg {...base(size, p)}><path d="M5 12l5 5L20 7" /></svg>
);
export const RefreshIcon = ({ size = 14, ...p }: P) => (
  <svg {...base(size, p)}><path d="M20 12a8 8 0 01-14.5 4.6M4 12a8 8 0 0114.5-4.6M20 4v4h-4M4 20v-4h4" /></svg>
);

/* Path data from the prototype's navIcons; rendered as 24px strokes in the mobile nav. */
export const NAV_PATHS: Record<string, string> = {
  home: "M3 11l9-7 9 7M5 10v10h14V10",
  vehicles: "M2 6h11v10H2zM13 9h4l4 4v3h-8M5.5 19a2 2 0 100-4 2 2 0 000 4zM17.5 19a2 2 0 100-4 2 2 0 000 4zM8 16h7",
  team: "M16 19v-1a4 4 0 00-4-4H7a4 4 0 00-4 4v1M9.5 11a3.5 3.5 0 100-7 3.5 3.5 0 000 7zM21 19v-1a3 3 0 00-2.3-2.9M15 4.2a3.5 3.5 0 010 6.6",
  agents: "M4 5h16v11H9l-5 4zM8 10h.01M12 10h.01M16 10h.01",
  sales: "M20 12l-8 8-9-9V4h7l10 8zM7.5 7.5h.01",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  tasks: "M9 6h11M9 12h11M9 18h11M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2",
};
export const NavPathIcon = ({ name, size = 24 }: { name: string; size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={NAV_PATHS[name] || NAV_PATHS.more} />
  </svg>
);
