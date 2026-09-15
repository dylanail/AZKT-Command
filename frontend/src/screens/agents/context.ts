/* The pinned context chip. A record opens the chat with ?context=vehicle:<id> (&label=…); that becomes the
   `context` object POST /api/agent/chat understands (manager.py reads vehicle_id, case_id and intake_id).
   The reply echoes the context back — when it differs, the screen says so instead of pretending. */
import { entityHref, entityLabel, humanize, shortId } from "../../lib/links";

export interface PinnedContext {
  /** The ?context= value, kept so the URL and the chip never drift apart. */
  raw: string;
  kind: string;
  id: string;
  label: string;
  href: string | null;
  context: Record<string, unknown>;
}

const KEY_FOR: Record<string, string> = { vehicle: "vehicle_id", case: "case_id", intake: "intake_id" };

export function parsePinnedContext(raw: string | null, label: string | null): PinnedContext | null {
  const value = (raw || "").trim();
  if (!value) return null;
  const at = value.indexOf(":");
  const kind = (at === -1 ? value : value.slice(0, at)).trim();
  const id = (at === -1 ? "" : value.slice(at + 1)).trim();
  if (!kind) return null;
  const nice = (label || "").trim() || `${humanize(entityLabel(kind))}${id ? ` ${shortId(id, 8)}` : ""}`;
  const context: Record<string, unknown> = { label: nice };
  const key = KEY_FOR[kind];
  if (key && id) context[key] = id;
  else if (id) { context.kind = kind; context.id = id; }
  return { raw: value, kind, id, label: nice, href: entityHref(kind, id), context };
}

/** Plain-language summary of whatever context the server echoed back. */
export function describeContext(ctx: Record<string, unknown> | null | undefined): string | null {
  if (!ctx || typeof ctx !== "object") return null;
  const label = typeof ctx.label === "string" && ctx.label ? ctx.label : null;
  if (label) return label;
  if (typeof ctx.vehicle_id === "string" && ctx.vehicle_id) return `Vehicle ${shortId(ctx.vehicle_id, 8)}`;
  if (typeof ctx.case_id === "string" && ctx.case_id) return `Case ${shortId(ctx.case_id, 8)}`;
  if (typeof ctx.intake_id === "string" && ctx.intake_id) return `Intake ${shortId(ctx.intake_id, 8)}`;
  if (typeof ctx.kind === "string" && typeof ctx.id === "string") return `${humanize(entityLabel(ctx.kind))} ${shortId(ctx.id, 8)}`;
  return null;
}

/** The identity part only: label alone is decoration, the ids are what the server acted on. */
function identity(ctx: Record<string, unknown> | null | undefined): string {
  if (!ctx) return "";
  const keys = ["vehicle_id", "case_id", "intake_id", "kind", "id"];
  return keys.map((k) => (typeof ctx[k] === "string" ? `${k}=${ctx[k] as string}` : "")).filter(Boolean).join("&");
}

/** True when the reply acted on a different record than the chip claimed. */
export function contextDiffers(sent: Record<string, unknown> | null | undefined, echoed: Record<string, unknown> | null | undefined): boolean {
  const a = identity(sent);
  const b = identity(echoed);
  if (!a && !b) return false;
  return a !== b;
}

/** Deep link back to the record the chip points at. */
export function contextHref(ctx: Record<string, unknown> | null | undefined): string | null {
  if (!ctx) return null;
  if (typeof ctx.vehicle_id === "string") return entityHref("vehicle", ctx.vehicle_id);
  if (typeof ctx.kind === "string" && typeof ctx.id === "string") return entityHref(ctx.kind, ctx.id);
  return null;
}
