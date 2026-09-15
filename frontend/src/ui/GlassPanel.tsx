import { forwardRef, type ElementType, type HTMLAttributes, type ReactNode } from "react";

export interface GlassPanelProps extends HTMLAttributes<HTMLElement> {
  /** Element to render (section, aside, nav…). Default div. */
  as?: ElementType;
  /** Corner radius family: md 24px (default) · lg 28px · xl 30px · sm 16px */
  radius?: "sm" | "md" | "lg" | "xl";
  /** Surface: surface (default) · side · sheet · strong */
  tone?: "surface" | "side" | "sheet" | "strong";
  /** Apply the standard inner padding (16px 18px). Rows-only panels should leave this off and use `clip`. */
  padded?: boolean;
  /** overflow:hidden so row separators and hover tints stay inside the rounded corners. */
  clip?: boolean;
  interactive?: boolean;
  children?: ReactNode;
}

export const GlassPanel = forwardRef<HTMLElement, GlassPanelProps>(function GlassPanel(
  { as: Tag = "div", radius = "md", tone = "surface", padded = false, clip = false, interactive = false, className = "", children, ...rest },
  ref,
) {
  const cls = [
    "glass",
    radius !== "md" ? `glass--${radius}` : "",
    tone !== "surface" ? `glass--${tone}` : "",
    padded ? "glass--pad" : "",
    clip ? "glass--clip" : "",
    interactive ? "glass--interactive" : "",
    className,
  ].filter(Boolean).join(" ");
  const Comp = Tag as ElementType;
  return <Comp ref={ref} className={cls} {...rest}>{children}</Comp>;
});
