import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { Portal } from "./Dialog";

export type ToastTone = "neutral" | "ok" | "risk" | "blocked" | "wait" | "amber";
export interface ToastAction { label: string; onClick: () => void; primary?: boolean; }
export interface ToastInput {
  title?: ReactNode;
  /** Short message. Plain toasts show only this. */
  message: ReactNode;
  tone?: ToastTone;
  actions?: ToastAction[];
  /** ms; default 4000 (8000 when actions are present); 0 = sticky. */
  duration?: number;
  id?: string;
}
interface ToastItem extends ToastInput { id: string; }

interface ToastCtx {
  toast: (t: ToastInput | string) => string;
  dismiss: (id: string) => void;
}
const Ctx = createContext<ToastCtx | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const timers = useRef(new Map<string, number>());

  const dismiss = useCallback((id: string) => {
    setItems((xs) => xs.filter((x) => x.id !== id));
    const t = timers.current.get(id);
    if (t) { window.clearTimeout(t); timers.current.delete(id); }
  }, []);

  const toast = useCallback((input: ToastInput | string) => {
    const t: ToastInput = typeof input === "string" ? { message: input } : input;
    const id = t.id || `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    const duration = t.duration ?? (t.actions?.length ? 8000 : 4000);
    setItems((xs) => [...xs.filter((x) => x.id !== id).slice(-3), { ...t, id }]);
    if (duration > 0) {
      const h = window.setTimeout(() => dismiss(id), duration);
      timers.current.set(id, h);
    }
    return id;
  }, [dismiss]);

  const value = useMemo(() => ({ toast, dismiss }), [toast, dismiss]);
  return (
    <Ctx.Provider value={value}>
      {children}
      <ToastHost items={items} onDismiss={dismiss} />
    </Ctx.Provider>
  );
}

export function useToast(): ToastCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}

const toneVar: Record<ToastTone, string> = { neutral: "var(--t3)", ok: "var(--ok)", risk: "var(--risk)", blocked: "var(--blocked)", wait: "var(--wait)", amber: "var(--amber)" };

export function ToastHost({ items, onDismiss }: { items: ToastItem[]; onDismiss: (id: string) => void }) {
  if (!items.length) return null;
  return (
    <Portal>
      <div className="toasts" aria-live="polite" aria-relevant="additions">
        {items.map((t) => {
          const rich = !!(t.title || t.actions?.length);
          return (
            <div key={t.id} role={t.tone === "blocked" ? "alert" : "status"} className={["toast", rich ? "toast--rich" : ""].filter(Boolean).join(" ")}>
              {rich ? <span className="toast__dot" style={{ background: toneVar[t.tone || "amber"] }} aria-hidden="true" /> : null}
              <div className="toast__body">
                {t.title ? <span className="toast__title">{t.title}</span> : null}
                <span className={rich ? "toast__text" : undefined}>{t.message}</span>
                {t.actions?.length ? (
                  <div className="toast__actions">
                    {t.actions.map((a) => (
                      <button key={a.label} type="button" className={["btn", "btn--xs", a.primary ? "btn--primary" : "btn--soft"].join(" ")} onClick={() => { a.onClick(); onDismiss(t.id); }}>{a.label}</button>
                    ))}
                  </div>
                ) : null}
              </div>
              <button type="button" className="toast__x" aria-label="Dismiss" onClick={() => onDismiss(t.id)}>×</button>
            </div>
          );
        })}
      </div>
    </Portal>
  );
}
