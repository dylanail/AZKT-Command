import { cloneElement, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactElement, type ReactNode } from "react";
import { Link } from "react-router-dom";

export interface MenuItem {
  label: ReactNode;
  meta?: ReactNode;
  onSelect?: () => void;
  to?: string;
  disabled?: boolean;
  disabledReason?: string;
  current?: boolean;
  /** Renders a separator above this item. */
  sepBefore?: boolean;
}
export interface MenuProps {
  /** The trigger; receives aria-haspopup/expanded, onClick and a ref. Must forward refs (Button/IconButton do). */
  trigger: ReactElement;
  items: MenuItem[];
  align?: "left" | "right";
  label: string;
  heading?: ReactNode;
  className?: string;
}

/** Popover menu with roving focus (arrows, Home/End, Esc, click-outside). */
export function Menu({ trigger, items, align = "left", label, heading, className = "" }: MenuProps) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const id = useId();

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); triggerRef.current?.focus(); } };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    const t = window.setTimeout(() => listRef.current?.querySelector<HTMLElement>('[role="menuitem"]:not([disabled])')?.focus(), 0);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); window.clearTimeout(t); };
  }, [open]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const els = Array.from(listRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') || []);
    if (!els.length) return;
    const i = els.indexOf(document.activeElement as HTMLElement);
    if (e.key === "ArrowDown") { e.preventDefault(); els[(i + 1) % els.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); els[(i - 1 + els.length) % els.length].focus(); }
    else if (e.key === "Home") { e.preventDefault(); els[0].focus(); }
    else if (e.key === "End") { e.preventDefault(); els[els.length - 1].focus(); }
    else if (e.key === "Tab") { setOpen(false); }
  };

  const trig = cloneElement(trigger as ReactElement<Record<string, unknown>>, {
    ref: (el: HTMLElement | null) => { triggerRef.current = el; },
    "aria-haspopup": "menu",
    "aria-expanded": open,
    "aria-controls": open ? `${id}-menu` : undefined,
    onClick: (e: MouseEvent) => { (trigger.props as { onClick?: (e: MouseEvent) => void }).onClick?.(e); setOpen((o) => !o); },
  });

  return (
    <div ref={wrap} className={["stage-menu", className].filter(Boolean).join(" ")} style={{ position: "relative", display: "inline-flex" }}>
      {trig}
      {open ? (
        <div ref={listRef} id={`${id}-menu`} role="menu" aria-label={label} className={["menu", align === "right" ? "menu--right" : ""].filter(Boolean).join(" ")} style={{ top: "calc(100% + 6px)" }} onKeyDown={onKeyDown}>
          {heading ? <div className="menu__head">{heading}</div> : null}
          {items.map((it, i) => {
            const common = { role: "menuitem" as const, className: "menu__item", "aria-current": it.current || undefined, title: it.disabled ? it.disabledReason : undefined };
            const inner = (<><span className="truncate">{it.label}</span>{it.meta ? <span className="menu__meta">{it.meta}</span> : null}</>);
            return (
              <div key={i} style={{ display: "contents" }}>
                {it.sepBefore ? <div className="menu__sep" role="separator" /> : null}
                {it.to && !it.disabled ? (
                  <Link to={it.to} {...common} onClick={() => { it.onSelect?.(); setOpen(false); }}>{inner}</Link>
                ) : (
                  <button type="button" {...common} disabled={it.disabled} onClick={() => { it.onSelect?.(); setOpen(false); }}>{inner}</button>
                )}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
