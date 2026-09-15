/* Formatting: money (tabular), dates/times in Phoenix (default) or Tokyo, relative time, ids. */

export const TZ = {
  phoenix: "America/Phoenix",
  tokyo: "Asia/Tokyo",
} as const;
export type TzKey = keyof typeof TZ;
export type TzName = (typeof TZ)[TzKey] | string;

export const TZ_LABEL: Record<string, string> = {
  "America/Phoenix": "AZ",
  "Asia/Tokyo": "JST",
};

export function tzLabel(tz: TzName): string {
  return TZ_LABEL[tz] || tz.split("/").pop() || tz;
}

const ZERO_DECIMAL = new Set(["JPY", "KRW", "VND", "CLP", "ISK", "HUF"]);

export interface MoneyOptions {
  currency?: string;
  /** Show "••••" instead of the amount (role hides prices). */
  hidden?: boolean;
  /** Include the ISO code after the number ("1,150 USD") instead of a symbol. Default true, matching the prototype. */
  code?: boolean;
  /** Force decimals; default 0 for zero-decimal currencies, 2 otherwise. */
  fractionDigits?: number;
  signDisplay?: "auto" | "always" | "never" | "exceptZero";
}

export function formatMoney(amount: number | string | null | undefined, opts: MoneyOptions = {}): string {
  const { currency = "USD", hidden = false, code = true } = opts;
  if (hidden) return "••••";
  if (amount === null || amount === undefined || amount === "") return "Not recorded";
  const n = typeof amount === "string" ? Number(amount) : amount;
  if (!Number.isFinite(n)) return "Not recorded";
  const digits = opts.fractionDigits ?? (ZERO_DECIMAL.has(currency.toUpperCase()) ? 0 : 2);
  const num = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    signDisplay: opts.signDisplay ?? "auto",
  }).format(n);
  if (code) return `${num} ${currency.toUpperCase()}`;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: currency.toUpperCase(),
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    signDisplay: opts.signDisplay ?? "auto",
  }).format(n);
}

export type WhenStyle = "time" | "date" | "datetime" | "short" | "long" | "weekday";

function toDate(iso: string | number | Date | null | undefined): Date | null {
  if (iso === null || iso === undefined || iso === "") return null;
  const d = iso instanceof Date ? iso : new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * Format a moment in a zone. Defaults to Phoenix with the "AZ" suffix, e.g. "Thu 17:00 AZ".
 * style: time "17:00 AZ" · date "Sep 10" · datetime "Sep 10, 17:00 AZ" · short "Thu 17:00 AZ" · long "Thu Sep 10, 17:00 AZ" · weekday "Thu".
 */
export function formatWhen(
  iso: string | number | Date | null | undefined,
  opts: { tz?: TzName; style?: WhenStyle; suffix?: boolean } = {},
): string {
  const d = toDate(iso);
  if (!d) return "Not recorded";
  const tz = opts.tz || TZ.phoenix;
  const style = opts.style || "short";
  const suffix = opts.suffix ?? (style !== "date" && style !== "weekday");
  const base: Intl.DateTimeFormatOptions = { timeZone: tz, hour12: false };
  let o: Intl.DateTimeFormatOptions;
  switch (style) {
    case "time": o = { ...base, hour: "2-digit", minute: "2-digit" }; break;
    case "date": o = { ...base, month: "short", day: "numeric" }; break;
    case "datetime": o = { ...base, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }; break;
    case "long": o = { ...base, weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }; break;
    case "weekday": o = { ...base, weekday: "short" }; break;
    default: o = { ...base, weekday: "short", hour: "2-digit", minute: "2-digit" };
  }
  let s = new Intl.DateTimeFormat("en-US", o).format(d).replace(",", "");
  // Intl renders midnight as "24:00" in some engines with hour12:false; normalise.
  s = s.replace(/\b24:(\d\d)/, "00:$1");
  return suffix ? `${s} ${tzLabel(tz)}` : s;
}

/** "in 22h", "3 min ago", "yesterday", "in 3 days". */
export function relativeTime(iso: string | number | Date | null | undefined, now: Date = new Date()): string {
  const d = toDate(iso);
  if (!d) return "Not recorded";
  const diff = d.getTime() - now.getTime();
  const abs = Math.abs(diff);
  const future = diff > 0;
  const min = Math.round(abs / 60000);
  const hr = Math.round(abs / 3600000);
  const day = Math.round(abs / 86400000);
  let core: string;
  if (min < 1) return "now";
  if (min < 60) core = `${min} min`;
  else if (hr < 24) core = `${hr}h`;
  else if (day === 1) return future ? "tomorrow" : "yesterday";
  else if (day < 14) core = `${day} days`;
  else if (day < 60) core = `${Math.round(day / 7)} weeks`;
  else core = formatWhen(d, { style: "date" });
  if (day >= 60) return core;
  return future ? `in ${core}` : `${core} ago`;
}

/** Days until a moment, negative when past. */
export function daysUntil(iso: string | number | Date | null | undefined, now: Date = new Date()): number | null {
  const d = toDate(iso);
  if (!d) return null;
  return Math.ceil((d.getTime() - now.getTime()) / 86400000);
}

export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined) return "";
  return new Intl.NumberFormat("en-US").format(n);
}

/** Initial for the account button. */
export function initialOf(name: string | null | undefined): string {
  const s = (name || "").trim();
  return s ? s[0].toUpperCase() : "?";
}

export function pluralize(n: number, one: string, many?: string): string {
  return `${formatCount(n)} ${n === 1 ? one : many || one + "s"}`;
}
