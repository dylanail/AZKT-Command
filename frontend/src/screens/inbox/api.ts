/* Paths for the inbox API (backend/app/routers/inbox.py). Every write goes through useCommand;
   these builders only assemble the URL so no caller hand-writes a path. */
import type { ThreadFilter } from "./types";

export const COVERAGE_PATH = "/api/inbox/coverage";
export const PASTE_PATH = "/api/inbox/paste";

export const threadPath = (id: string) => `/api/inbox/threads/${encodeURIComponent(id)}`;
export const draftVersionsPath = (draftId: string) => `/api/drafts/${encodeURIComponent(draftId)}/versions`;

/** POST /api/inbox/threads/{id}/{action} — action is a key of the router's ACTIONS map. */
export type ThreadAction =
  | "prepare" | "edit" | "submit" | "take-over" | "resume" | "link" | "unlink"
  | "classify" | "spam" | "not-spam" | "archive" | "manual-reply" | "reconcile";
export const actionPath = (id: string, action: ThreadAction) => `${threadPath(id)}/${action}`;

export interface ThreadListQuery {
  filter: ThreadFilter;
  account?: string | null;
  q?: string | null;
  limit?: number;
  offset?: number;
}

export function threadsPath({ filter, account, q, limit = 50, offset = 0 }: ThreadListQuery): string {
  const p = new URLSearchParams();
  p.set("filter", filter);
  if (account) p.set("account", account);
  if (q) p.set("q", q);
  p.set("limit", String(limit));
  if (offset) p.set("offset", String(offset));
  return `/api/inbox/threads?${p.toString()}`;
}

/** Search paths for the Link picker. Requests live at /api/import-requests, leads at /api/sales/opportunities. */
export function pickerPath(kind: string, q: string): string {
  const term = q.trim();
  const enc = encodeURIComponent(term);
  switch (kind) {
    case "vehicle": return `/api/vehicles?limit=20${term ? `&q=${enc}` : ""}`;
    case "opportunity": return `/api/sales/opportunities?limit=20${term ? `&q=${enc}` : ""}`;
    case "import_request": return `/api/import-requests?limit=20${term ? `&q=${enc}` : ""}`;
    case "contact": return `/api/contacts?limit=20${term ? `&q=${enc}` : ""}`;
    default: return "";
  }
}

/** Per-thread sessionStorage key for an unsent edit, so a reload or a tab switch never loses typing. */
export const draftCacheKey = (conversationId: string, draftId: string) => `azkt.inbox.draft.${conversationId}.${draftId}`;

export function readCachedBody(key: string): string | null {
  try { return window.sessionStorage.getItem(key); } catch { return null; }
}
export function writeCachedBody(key: string, body: string): void {
  try { window.sessionStorage.setItem(key, body); } catch { /* private mode / full quota */ }
}
export function clearCachedBody(key: string): void {
  try { window.sessionStorage.removeItem(key); } catch { /* ignore */ }
}
