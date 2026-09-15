/* Renders receipts, details and sources truthfully: scalars inline, nested values as a compact JSON
   block, and money values the server scrubbed (money_hidden) as "hidden" rather than blank. */
import type { ReactNode } from "react";
import { KeyValues, NotRecorded } from "../../ui";
import { humanize } from "../../lib/links";

const MONEY_KEYS = ["amount", "price", "cost", "total", "margin", "profit", "landed", "asking", "sold_price", "purchase_amount",
  "balance", "fee", "fees", "subtotal", "net", "gross", "payout", "deposit_amount", "per_action", "cumulative"];

export function isMoneyKey(key: string): boolean {
  const k = key.toLowerCase();
  return MONEY_KEYS.some((m) => k === m || k.endsWith("_" + m) || k.startsWith(m + "_") || k.split("_").includes(m));
}

export function isEmptyValue(v: unknown): boolean {
  if (v === null || v === undefined || v === "") return true;
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === "object") return Object.keys(v as object).length === 0;
  return false;
}

export function scalarText(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") return new Intl.NumberFormat("en-US").format(v);
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

export function JsonBlock({ value }: { value: unknown }) {
  return <pre className="act-pre">{JSON.stringify(value, null, 2)}</pre>;
}

/** Key/value view of a plain object. `hideKeys` are omitted; money keys with null values show "hidden". */
export function JsonDetail({ value, hideKeys = [], emptyText = "Nothing recorded" }: { value: unknown; hideKeys?: string[]; emptyText?: string }) {
  if (isEmptyValue(value)) return <NotRecorded text={emptyText} />;
  if (Array.isArray(value)) {
    if (value.every((x) => typeof x !== "object" || x === null)) return <span>{value.map(scalarText).join(", ")}</span>;
    return <JsonBlock value={value} />;
  }
  if (typeof value !== "object") return <span>{scalarText(value)}</span>;
  const obj = value as Record<string, unknown>;
  const scrubbed = obj.money_hidden === true;
  const items: Array<[ReactNode, ReactNode]> = [];
  for (const [k, v] of Object.entries(obj)) {
    if (k === "money_hidden" || hideKeys.includes(k)) continue;
    let node: ReactNode;
    if (scrubbed && v === null && isMoneyKey(k)) node = <span className="money money--hidden" aria-label="Amount hidden">hidden</span>;
    else if (isEmptyValue(v)) node = <NotRecorded text="—" />;
    else if (typeof v === "object") node = <JsonDetail value={v} />;
    else node = <span className={typeof v === "number" ? "tnum" : undefined}>{scalarText(v)}</span>;
    items.push([humanize(k), node]);
  }
  if (!items.length) return <NotRecorded text={emptyText} />;
  return <KeyValues items={items} />;
}
