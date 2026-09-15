import { useId, type KeyboardEvent, type ReactNode } from "react";

export interface TabItem<K extends string> {
  id: K;
  label: string;
  count?: number | string;
  disabled?: boolean;
  disabledReason?: string;
}
export interface TabsProps<K extends string> {
  tabs: TabItem<K>[];
  value: K;
  onChange: (id: K) => void;
  label: string;
  className?: string;
  /** Id prefix shared with <TabPanel> so aria-controls/labelledby link up. */
  idPrefix?: string;
}

/** Pill tablist. Arrow keys move focus + selection (automatic activation). */
export function Tabs<K extends string>({ tabs, value, onChange, label, className = "", idPrefix }: TabsProps<K>) {
  const auto = useId();
  const prefix = idPrefix || auto;
  const onKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const enabled = tabs.filter((t) => !t.disabled);
    const cur = enabled.findIndex((t) => t.id === value);
    let next = cur;
    if (e.key === "ArrowRight") next = (cur + 1) % enabled.length;
    if (e.key === "ArrowLeft") next = (cur - 1 + enabled.length) % enabled.length;
    if (e.key === "Home") next = 0;
    if (e.key === "End") next = enabled.length - 1;
    const t = enabled[next];
    if (!t) return;
    onChange(t.id);
    document.getElementById(`${prefix}-tab-${t.id}`)?.focus();
  };
  return (
    <div role="tablist" aria-label={label} className={["tabs", className].filter(Boolean).join(" ")}>
      {tabs.map((t) => {
        const on = t.id === value;
        return (
          <button
            key={t.id}
            id={`${prefix}-tab-${t.id}`}
            type="button"
            role="tab"
            className="tab"
            aria-selected={on}
            aria-controls={`${prefix}-panel-${t.id}`}
            tabIndex={on ? 0 : -1}
            disabled={t.disabled}
            title={t.disabled ? t.disabledReason : undefined}
            onClick={() => onChange(t.id)}
            onKeyDown={onKey}
          >
            {t.label}
            {t.count !== undefined ? <span className="tab__count">{t.count}</span> : null}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({ id, idPrefix, active, children, className = "" }: { id: string; idPrefix: string; active: boolean; children: ReactNode; className?: string }) {
  if (!active) return null;
  return (
    <div role="tabpanel" id={`${idPrefix}-panel-${id}`} aria-labelledby={`${idPrefix}-tab-${id}`} className={["tabpanel", className].filter(Boolean).join(" ")} tabIndex={0}>
      {children}
    </div>
  );
}
