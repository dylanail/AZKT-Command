import type { HTMLAttributes } from "react";
import { formatMoney, formatWhen, relativeTime, TZ, type MoneyOptions, type TzName, type WhenStyle } from "../lib/format";

export interface MoneyProps extends Omit<HTMLAttributes<HTMLSpanElement>, "children">, MoneyOptions {
  amount: number | string | null | undefined;
  /** Hide the value entirely (role without costs.read). Renders "••••". */
  hidden?: boolean;
}
/** Tabular-nums money. Negative values are tinted. */
export function Money({ amount, currency = "USD", hidden = false, code, fractionDigits, signDisplay, className = "", ...rest }: MoneyProps) {
  const text = formatMoney(amount, { currency, hidden, code, fractionDigits, signDisplay });
  const n = typeof amount === "string" ? Number(amount) : amount;
  const neg = !hidden && typeof n === "number" && Number.isFinite(n) && n < 0;
  const cls = ["money", hidden ? "money--hidden" : "", neg ? "money--neg" : "", className].filter(Boolean).join(" ");
  return <span className={cls} aria-label={hidden ? "Amount hidden" : undefined} {...rest}>{text}</span>;
}

export interface WhenProps extends Omit<HTMLAttributes<HTMLTimeElement>, "children"> {
  iso: string | number | Date | null | undefined;
  /** IANA zone; default Phoenix. Pass TZ.tokyo for auction times. */
  tz?: TzName;
  /** Which fields to show; default "short" ("Thu 17:00 AZ"). */
  format?: WhenStyle;
  /** Show "in 22h" / "3 min ago" instead; the absolute time goes in the tooltip. */
  relative?: boolean;
  /** Also show the Tokyo time after the Phoenix one ("Thu 17:00 AZ · Fri 09:00 JST"). */
  withTokyo?: boolean;
}
export function When({ iso, tz = TZ.phoenix, format = "short", relative = false, withTokyo = false, className = "", ...rest }: WhenProps) {
  const abs = formatWhen(iso, { tz, style: format });
  const text = relative ? relativeTime(iso) : withTokyo ? `${abs} · ${formatWhen(iso, { tz: TZ.tokyo, style: format })}` : abs;
  const dt = iso instanceof Date ? iso.toISOString() : iso != null ? String(iso) : undefined;
  return <time dateTime={dt} title={relative ? abs : undefined} className={["when", className].filter(Boolean).join(" ")} {...rest}>{text}</time>;
}
