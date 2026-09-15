import type { HTMLAttributes, ReactNode, TableHTMLAttributes } from "react";

export interface TableProps extends TableHTMLAttributes<HTMLTableElement> {
  caption?: ReactNode;
  /** Minimum table width before the wrapper scrolls horizontally. */
  minWidth?: number | string;
  wrapClassName?: string;
  children: ReactNode;
}
/** Table inside an overflow-x wrapper; the page itself never scrolls sideways. */
export function Table({ caption, minWidth, wrapClassName = "", className = "", children, ...rest }: TableProps) {
  return (
    <div className={["table-wrap", wrapClassName].filter(Boolean).join(" ")}>
      <table className={["table", className].filter(Boolean).join(" ")} style={minWidth ? { minWidth } : undefined} {...rest}>
        {caption ? <caption>{caption}</caption> : null}
        {children}
      </table>
    </div>
  );
}

export function Tr({ clickable, onClick, children, ...rest }: HTMLAttributes<HTMLTableRowElement> & { clickable?: boolean }) {
  return (
    <tr
      data-clickable={clickable || undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={onClick}
      onKeyDown={clickable && onClick ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); (onClick as (e: unknown) => void)(e); } } : undefined}
      {...rest}
    >
      {children}
    </tr>
  );
}
