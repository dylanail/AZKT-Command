import { useId, type KeyboardEvent } from "react";

export interface SegmentOption<V extends string> {
  value: V;
  label: string;
  count?: number | string;
  disabled?: boolean;
  disabledReason?: string;
}
export interface SegmentedControlProps<V extends string> {
  options: SegmentOption<V>[];
  value: V;
  onChange: (value: V) => void;
  /** Accessible group name. */
  label: string;
  size?: "sm" | "md";
  /** Stretch items to fill the row (mobile). */
  block?: boolean;
  className?: string;
}

/** Radio-group semantics; arrow keys move selection. */
export function SegmentedControl<V extends string>({ options, value, onChange, label, size = "md", block = false, className = "" }: SegmentedControlProps<V>) {
  const id = useId();
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, idx: number) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const enabled = options.filter((o) => !o.disabled);
    if (!enabled.length) return;
    const cur = enabled.findIndex((o) => o.value === options[idx].value);
    let next = cur;
    if (e.key === "ArrowRight") next = (cur + 1) % enabled.length;
    if (e.key === "ArrowLeft") next = (cur - 1 + enabled.length) % enabled.length;
    if (e.key === "Home") next = 0;
    if (e.key === "End") next = enabled.length - 1;
    onChange(enabled[next].value);
    const el = document.getElementById(`${id}-${enabled[next].value}`);
    el?.focus();
  };
  const cls = ["seg", size === "sm" ? "seg--sm" : "", block ? "seg--block" : "", className].filter(Boolean).join(" ");
  return (
    <div role="radiogroup" aria-label={label} className={cls}>
      {options.map((o, i) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            id={`${id}-${o.value}`}
            type="button"
            role="radio"
            aria-checked={on}
            className="seg__item"
            tabIndex={on ? 0 : -1}
            disabled={o.disabled}
            title={o.disabled ? o.disabledReason : undefined}
            onClick={() => onChange(o.value)}
            onKeyDown={(e) => onKey(e, i)}
          >
            {o.label}
            {o.count !== undefined ? <span className="seg__count">{o.count}</span> : null}
          </button>
        );
      })}
    </div>
  );
}
