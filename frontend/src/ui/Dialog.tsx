import { useCallback, useEffect, useId, useRef, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { IconButton } from "./Button";
import { CloseIcon } from "./Icons";

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function portalRoot(): HTMLElement {
  let el = document.getElementById("azkt-portal");
  if (!el) {
    el = document.createElement("div");
    el.id = "azkt-portal";
    document.body.appendChild(el);
  }
  return el;
}

export function Portal({ children }: { children: ReactNode }) {
  if (typeof document === "undefined") return null;
  return createPortal(children, portalRoot());
}

/**
 * Focus trap: moves focus in on open, cycles Tab inside, restores focus on close.
 * Returns an onKeyDown handler for the container.
 */
export function useFocusTrap(ref: RefObject<HTMLElement>, active: boolean, initialFocus?: RefObject<HTMLElement>) {
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!active) return;
    restoreRef.current = (document.activeElement as HTMLElement) || null;
    const node = ref.current;
    const first = initialFocus?.current || node?.querySelector<HTMLElement>(FOCUSABLE) || node;
    // Defer so the portal content exists and animations don't fight the scroll.
    const t = window.setTimeout(() => first?.focus({ preventScroll: true }), 0);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.clearTimeout(t);
      document.body.style.overflow = prevOverflow;
      restoreRef.current?.focus?.({ preventScroll: true });
    };
  }, [active, ref, initialFocus]);

  return useCallback((e: ReactKeyboardEvent<HTMLElement>) => {
    if (e.key !== "Tab" || !ref.current) return;
    const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (!items.length) { e.preventDefault(); return; }
    const first = items[0], last = items[items.length - 1];
    const activeEl = document.activeElement as HTMLElement | null;
    if (e.shiftKey && (activeEl === first || !ref.current.contains(activeEl))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && activeEl === last) { e.preventDefault(); first.focus(); }
  }, [ref]);
}

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  /** Small uppercase line above the title (e.g. "Approval · send message"). */
  eyebrow?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  /** sm 440 · md 540 (default) · lg 720 · xl 900 */
  size?: "sm" | "md" | "lg" | "xl";
  /** Anchor to the top (long approval reviews) instead of centring. */
  align?: "center" | "top";
  /** Set false for destructive flows that must be answered. */
  dismissible?: boolean;
  /** Accessible name when no title is rendered. */
  label?: string;
  initialFocusRef?: RefObject<HTMLElement>;
  className?: string;
}

/** Desktop overlay dialog: var(--dim) scrim with blur, focus trap, Esc closes, click outside closes. */
export function Dialog({ open, onClose, title, eyebrow, description, children, footer, size = "md", align = "center", dismissible = true, label, initialFocusRef, className = "" }: DialogProps) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  const trap = useFocusTrap(ref, open, initialFocusRef);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && dismissible) { e.stopPropagation(); onClose(); } };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose, dismissible]);

  if (!open) return null;
  const onScrim = (e: MouseEvent<HTMLDivElement>) => { if (dismissible && e.target === e.currentTarget) onClose(); };
  return (
    <Portal>
      <div className={["overlay", align === "top" ? "overlay--top" : ""].filter(Boolean).join(" ")} onMouseDown={onScrim}>
        <div
          ref={ref}
          role="dialog"
          aria-modal="true"
          aria-labelledby={title ? `${id}-title` : undefined}
          aria-label={!title ? label : undefined}
          aria-describedby={description ? `${id}-desc` : undefined}
          className={["dialog", size !== "md" ? `dialog--${size}` : "", className].filter(Boolean).join(" ")}
          onKeyDown={trap}
        >
          {(title || dismissible) && (
            <div className="dialog__head">
              <div className="grow">
                {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
                {title ? <div id={`${id}-title`} className="dialog__title">{title}</div> : null}
                {description ? <div id={`${id}-desc`} className="dialog__desc">{description}</div> : null}
              </div>
              {dismissible ? (
                <IconButton label="Close" size="sm" variant="plain" onClick={onClose}><CloseIcon size={16} /></IconButton>
              ) : null}
            </div>
          )}
          <div className="dialog__body">{children}</div>
          {footer ? <div className="dialog__foot">{footer}</div> : null}
        </div>
      </div>
    </Portal>
  );
}

export interface SheetProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  dismissible?: boolean;
  label?: string;
  initialFocusRef?: RefObject<HTMLElement>;
  className?: string;
}

/** Mobile bottom sheet (36px top radius). Same trap/Esc behaviour as Dialog. */
export function Sheet({ open, onClose, title, children, footer, dismissible = true, label, initialFocusRef, className = "" }: SheetProps) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  const trap = useFocusTrap(ref, open, initialFocusRef);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && dismissible) { e.stopPropagation(); onClose(); } };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose, dismissible]);

  if (!open) return null;
  const onScrim = (e: MouseEvent<HTMLDivElement>) => { if (dismissible && e.target === e.currentTarget) onClose(); };
  return (
    <Portal>
      <div className="overlay overlay--bottom" onMouseDown={onScrim}>
        <div
          ref={ref}
          role="dialog"
          aria-modal="true"
          aria-labelledby={title ? `${id}-title` : undefined}
          aria-label={!title ? label : undefined}
          className={["sheet", className].filter(Boolean).join(" ")}
          onKeyDown={trap}
        >
          <div className="sheet__grip" aria-hidden="true" />
          {(title || dismissible) && (
            <div className="sheet__head">
              {title ? <div id={`${id}-title`} className="sheet__title">{title}</div> : <span />}
              {dismissible ? <IconButton label="Close" size="sm" variant="plain" onClick={onClose}><CloseIcon size={18} /></IconButton> : null}
            </div>
          )}
          <div className="sheet__body">{children}</div>
          {footer ? <div className="sheet__foot">{footer}</div> : null}
        </div>
      </div>
    </Portal>
  );
}

/** Picks Sheet on phones and Dialog on desktop. */
export function ResponsiveDialog({ mobile, ...props }: DialogProps & { mobile: boolean }) {
  if (mobile) {
    const { open, onClose, title, children, footer, dismissible, label, initialFocusRef, className } = props;
    return <Sheet open={open} onClose={onClose} title={title} footer={footer} dismissible={dismissible} label={label} initialFocusRef={initialFocusRef} className={className}>{children}</Sheet>;
  }
  return <Dialog {...props} />;
}
