import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Link } from "react-router-dom";

export type ButtonVariant = "primary" | "glass" | "soft" | "ghost" | "danger" | "ok";
export type ButtonSize = "xs" | "sm" | "md" | "lg" | "xl";

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type"> {
  variant?: ButtonVariant;
  /** xs 28 · sm 32 · md 36 · lg 40 (default) · xl 44 */
  size?: ButtonSize;
  type?: "button" | "submit" | "reset";
  loading?: boolean;
  /** Plain-language reason shown as tooltip when disabled (every disabled control says why). */
  disabledReason?: string;
  iconLeft?: ReactNode;
  iconRight?: ReactNode;
  block?: boolean;
  /** Render as a router link instead of a button. */
  to?: string;
  children?: ReactNode;
}

const sizeClass: Record<ButtonSize, string> = { xs: "btn--xs", sm: "btn--sm", md: "btn--md", lg: "", xl: "btn--lg" };

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "glass", size = "lg", type = "button", loading = false, disabled, disabledReason, iconLeft, iconRight, block, to, className = "", children, title, ...rest },
  ref,
) {
  const isDisabled = disabled || loading;
  const cls = ["btn", `btn--${variant}`, sizeClass[size], block ? "btn--block" : "", className].filter(Boolean).join(" ");
  const tip = isDisabled && disabledReason ? disabledReason : title;
  const content = (
    <>
      {loading ? <span className="btn__spinner" aria-hidden="true" /> : iconLeft}
      {children}
      {iconRight}
    </>
  );
  if (to && !isDisabled) {
    return <Link to={to} className={cls} title={tip}>{content}</Link>;
  }
  return (
    <button
      ref={ref}
      type={type}
      className={cls}
      disabled={isDisabled}
      aria-disabled={isDisabled || undefined}
      aria-busy={loading || undefined}
      title={tip}
      {...rest}
    >
      {content}
    </button>
  );
});

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type" | "children"> {
  /** Accessible name; required. */
  label: string;
  size?: "xs" | "sm" | "md";
  /** plain = no glass chrome (for inline × buttons). */
  variant?: "glass" | "plain";
  active?: boolean;
  badge?: number | string | null;
  badgeTone?: "blocked" | "risk" | "ok" | "wait" | "act" | "amber";
  disabledReason?: string;
  children: ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, size = "md", variant = "glass", active, badge, badgeTone = "wait", disabled, disabledReason, className = "", title, children, ...rest },
  ref,
) {
  const cls = ["iconbtn", size !== "md" ? `iconbtn--${size}` : "", variant === "plain" ? "iconbtn--plain" : "", active ? "iconbtn--active" : "", className].filter(Boolean).join(" ");
  return (
    <button ref={ref} type="button" className={cls} aria-label={label} title={disabled && disabledReason ? disabledReason : title ?? label} disabled={disabled} aria-pressed={active === undefined ? undefined : active} {...rest}>
      {children}
      {badge !== null && badge !== undefined && badge !== 0 && badge !== "" ? (
        <span className="iconbtn__badge" style={{ background: `var(--${badgeTone})` }} aria-hidden="true">{badge}</span>
      ) : null}
    </button>
  );
});
