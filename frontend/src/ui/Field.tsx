import { createContext, forwardRef, useContext, useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes, type ButtonHTMLAttributes } from "react";

interface FieldCtx { id: string; describedBy?: string; invalid: boolean; }
const Ctx = createContext<FieldCtx | null>(null);

export interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  /** Optional right-hand slot in the label row (e.g. a link). */
  aside?: ReactNode;
  id?: string;
  className?: string;
  children: ReactNode;
}

/** Label + control + hint/error. Wrap exactly one Input/Textarea/Select (they pick up the id). */
export function Field({ label, hint, error, required, aside, id, className = "", children }: FieldProps) {
  const auto = useId();
  const fid = id || auto;
  const hintId = hint ? `${fid}-hint` : undefined;
  const errId = error ? `${fid}-err` : undefined;
  const describedBy = [errId, hintId].filter(Boolean).join(" ") || undefined;
  return (
    <div className={["field", className].filter(Boolean).join(" ")}>
      <label htmlFor={fid} className="field__label">
        <span>{label}{required ? <span className="field__req" aria-hidden="true"> *</span> : null}</span>
        {aside}
      </label>
      <Ctx.Provider value={{ id: fid, describedBy, invalid: !!error }}>{children}</Ctx.Provider>
      {error ? <span id={errId} className="field__error" role="alert">{error}</span> : null}
      {hint ? <span id={hintId} className="field__hint">{hint}</span> : null}
    </div>
  );
}

function useFieldAttrs(explicitId?: string) {
  const ctx = useContext(Ctx);
  return {
    id: explicitId || ctx?.id,
    "aria-describedby": ctx?.describedBy,
    "aria-invalid": ctx?.invalid || undefined,
  };
}

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> { pill?: boolean; }
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input({ className = "", pill, id, ...rest }, ref) {
  const a = useFieldAttrs(id);
  return <input ref={ref} className={["input", pill ? "input--pill" : "", className].filter(Boolean).join(" ")} {...a} {...rest} />;
});

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea({ className = "", id, ...rest }, ref) {
  const a = useFieldAttrs(id);
  return <textarea ref={ref} className={["textarea", className].filter(Boolean).join(" ")} {...a} {...rest} />;
});

export type SelectProps = SelectHTMLAttributes<HTMLSelectElement>;
export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select({ className = "", id, children, ...rest }, ref) {
  const a = useFieldAttrs(id);
  return <select ref={ref} className={["select", className].filter(Boolean).join(" ")} {...a} {...rest}>{children}</select>;
});

export interface SwitchProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onChange" | "children"> {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: ReactNode;
  meta?: ReactNode;
  disabledReason?: string;
}
/** Row switch (role=switch) as used in Settings and per-person permissions. */
export function Switch({ checked, onChange, label, meta, disabled, disabledReason, className = "", title, ...rest }: SwitchProps) {
  return (
    <button type="button" role="switch" aria-checked={checked} className={["switch", className].filter(Boolean).join(" ")} disabled={disabled} title={disabled && disabledReason ? disabledReason : title} onClick={() => onChange(!checked)} {...rest}>
      <span className="truncate">{label}{meta ? <span className="t4 fs12"> · {meta}</span> : null}</span>
      <span className="switch__track" aria-hidden="true"><span className="switch__knob" /></span>
    </button>
  );
}
