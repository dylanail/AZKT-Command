import { useId, useState, type ReactNode } from "react";

export interface ExpanderProps {
  /** Default: the standard label technical terms hide behind. */
  title?: ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onToggle?: (open: boolean) => void;
  children: ReactNode;
  className?: string;
}

/** "Sources and technical details" disclosure. Plain language outside, payload/run/policy inside. */
export function Expander({ title = "Sources and technical details", defaultOpen = false, open: controlled, onToggle, children, className = "" }: ExpanderProps) {
  const [inner, setInner] = useState(defaultOpen);
  const open = controlled ?? inner;
  const id = useId();
  const toggle = () => { const n = !open; if (controlled === undefined) setInner(n); onToggle?.(n); };
  return (
    <div className={["expander", className].filter(Boolean).join(" ")}>
      <button type="button" className="expander__btn" aria-expanded={open} aria-controls={`${id}-body`} onClick={toggle}>
        <span className="expander__caret" aria-hidden="true">▶</span>
        <span>{title}</span>
      </button>
      {open ? <div id={`${id}-body`} className="expander__body">{children}</div> : null}
    </div>
  );
}

/** Key/value grid for inside expanders and detail panels. */
export function KeyValues({ items, className = "" }: { items: Array<[ReactNode, ReactNode]>; className?: string }) {
  return (
    <dl className={["kv", className].filter(Boolean).join(" ")}>
      {items.map(([k, v], i) => (
        <div key={i} style={{ display: "contents" }}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}
