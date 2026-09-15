import type { HTMLAttributes, MouseEvent, ReactNode } from "react";
import { Link } from "react-router-dom";
import { HealthLabel, type Health } from "./Health";
import { ChevronRight } from "./Icons";
import { GlassPanel } from "./GlassPanel";

export interface ListRowProps extends Omit<HTMLAttributes<HTMLElement>, "title" | "onClick"> {
  title: ReactNode;
  meta?: ReactNode;
  /** Right slot: a button, a value, a chip… */
  right?: ReactNode;
  /** Leading slot: avatar, thumbnail. */
  leading?: ReactNode;
  /** Health label rendered beside the title (colour + text). */
  health?: Health;
  healthLabel?: string;
  /** Extra inline labels beside the title (chips). */
  tags?: ReactNode;
  /** Navigate on click. */
  to?: string;
  onClick?: (e: MouseEvent<HTMLElement>) => void;
  /** Show a › affordance (default when to/onClick and no right slot). */
  chevron?: boolean;
}

/** One row of a list panel: title/meta on the left, one slot on the right. Wrap rows in <ListGroup>. */
export function ListRow({ title, meta, right, leading, health, healthLabel, tags, to, onClick, chevron, className = "", ...rest }: ListRowProps) {
  const interactive = !!(to || onClick);
  const showChevron = chevron ?? (interactive && !right);
  const cls = ["list-row", leading ? "list-row--leading" : "", className].filter(Boolean).join(" ");
  const body = (
    <>
      {leading}
      <div className="list-row__main">
        <div className="list-row__title">
          <span className="truncate" style={{ minWidth: 0 }}>{title}</span>
          {health ? <HealthLabel health={health} label={healthLabel} /> : null}
          {tags}
        </div>
        {meta ? <div className="list-row__meta">{meta}</div> : null}
      </div>
      {(right || showChevron) ? (
        <div className="list-row__right">
          {right}
          {showChevron ? <span className="list-row__chev" aria-hidden="true"><ChevronRight /></span> : null}
        </div>
      ) : null}
    </>
  );
  if (to) return <Link to={to} className={cls} onClick={onClick as ((e: MouseEvent<HTMLAnchorElement>) => void) | undefined} {...(rest as HTMLAttributes<HTMLAnchorElement>)}>{body}</Link>;
  if (onClick) return <button type="button" className={cls} onClick={onClick} {...(rest as HTMLAttributes<HTMLButtonElement>)}>{body}</button>;
  return <div className={cls} {...(rest as HTMLAttributes<HTMLDivElement>)}>{body}</div>;
}

/** Glass panel that clips rows. */
export function ListGroup({ children, className = "", ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <GlassPanel clip className={["list", className].filter(Boolean).join(" ")} {...rest}>{children}</GlassPanel>;
}
