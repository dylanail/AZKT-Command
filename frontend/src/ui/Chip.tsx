import { forwardRef, type ButtonHTMLAttributes, type HTMLAttributes, type ReactNode, type Ref } from "react";

export type ChipTone = "neutral" | "soft" | "act" | "amber" | "blocked" | "risk" | "ok" | "wait";

interface ChipBase {
  tone?: ChipTone;
  size?: "sm" | "md";
  selected?: boolean;
  count?: number | string;
  /** Show a × that calls onRemove. */
  onRemove?: () => void;
  removeLabel?: string;
  disabled?: boolean;
  /** Reason shown as tooltip when disabled. */
  disabledReason?: string;
  children?: ReactNode;
}
export type ChipProps = ChipBase & (
  | ({ onClick: ButtonHTMLAttributes<HTMLButtonElement>["onClick"] } & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onClick" | "children">)
  | ({ onClick?: undefined } & Omit<HTMLAttributes<HTMLSpanElement>, "children">)
);

/** Never wraps internally (white-space:nowrap; flex:none). Interactive when onClick is given. */
export const Chip = forwardRef<HTMLElement, ChipProps>(function Chip(props, ref) {
  const { tone = "neutral", size = "md", selected, count, onRemove, removeLabel = "Remove", disabled, disabledReason, children, className = "", ...rest } = props;
  const cls = ["chip", size === "sm" ? "chip--sm" : "", tone !== "neutral" ? `chip--${tone}` : "", selected ? "chip--selected" : "", className].filter(Boolean).join(" ");
  const inner = (
    <>
      {children}
      {count !== undefined ? <span className="chip__count">{count}</span> : null}
      {onRemove ? (
        <button type="button" className="chip__x" aria-label={removeLabel} onClick={(e) => { e.stopPropagation(); onRemove(); }}>×</button>
      ) : null}
    </>
  );
  if ("onClick" in props && props.onClick) {
    const { onClick, ...btn } = rest as ButtonHTMLAttributes<HTMLButtonElement>;
    return (
      <button ref={ref as Ref<HTMLButtonElement>} type="button" className={cls} onClick={onClick} aria-pressed={selected === undefined ? undefined : selected} {...btn} disabled={disabled} title={disabled && disabledReason ? disabledReason : btn.title}>
        {inner}
      </button>
    );
  }
  return <span ref={ref as Ref<HTMLSpanElement>} className={cls} {...(rest as HTMLAttributes<HTMLSpanElement>)}>{inner}</span>;
});

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: "blocked" | "risk" | "ok" | "wait" | "act" | "amber" | "soft" | "neutral";
  children?: ReactNode;
}
/** Small count/status pill. Never wraps. */
export function Badge({ tone = "neutral", className = "", children, ...rest }: BadgeProps) {
  const cls = ["badge", tone !== "neutral" ? `badge--${tone}` : "", className].filter(Boolean).join(" ");
  return <span className={cls} {...rest}>{children}</span>;
}
