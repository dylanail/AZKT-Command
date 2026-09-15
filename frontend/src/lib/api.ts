/* Fetch wrapper. Same-origin cookies, JSON errors mapped from the backend envelope
   {error, message, ...detail} into ApiError, Idempotency-Key for POST commands,
   and a `command()` helper that returns the CommandResult envelope
   {status: ok|needs_review|blocked, data, changed, approval_id, decision, request_id}. */

export type ErrorCode =
  | "not_found" | "conflict" | "validation_failed" | "denied" | "blocked"
  | "unsupported" | "provider_error" | "domain_error" | "unknown_command"
  | "unauthorized" | "network" | "http" | string;

export class ApiError extends Error {
  status: number;
  code: ErrorCode;
  detail: Record<string, unknown>;
  requestId?: string;
  constructor(status: number, code: ErrorCode, message: string, detail: Record<string, unknown> = {}) {
    super(message || code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    const rid = detail["request_id"];
    if (typeof rid === "string") this.requestId = rid;
  }
  get isUnauthorized() { return this.status === 401; }
  get isDenied() { return this.status === 403; }
  get isNotFound() { return this.status === 404; }
  /** True for outcomes the UI should explain rather than treat as failures. */
  get isBusinessGate() { return this.code === "blocked" || this.code === "needs_review"; }
}

export type CommandStatus = "ok" | "needs_review" | "blocked";
export interface Decision {
  outcome: "allowed" | "needs_review" | "blocked";
  reasons: string[];
  policy_version: string;
  permission_id: string | null;
}
export interface CommandResult<T = unknown> {
  status: CommandStatus;
  data: T | null;
  changed: string[];
  approval_id: string | null;
  decision: Decision;
  request_id: string | null;
}

export type UnauthorizedListener = (path: string) => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();
/** AuthProvider subscribes here; a 401 anywhere flips the session to "expired". */
export function onUnauthorized(fn: UnauthorizedListener): () => void {
  unauthorizedListeners.add(fn);
  return () => { unauthorizedListeners.delete(fn); };
}

/** RFC 4122 v4 id for Idempotency-Key. Falls back when crypto.randomUUID is unavailable (older WebViews). */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === "function") c.getRandomValues(bytes);
  else for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const h = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  idempotencyKey?: string;
  /** Statuses to return as `null` instead of throwing (e.g. [404] while an endpoint is still being built). */
  tolerate?: number[];
}

async function parseBody(r: Response): Promise<unknown> {
  const ct = r.headers.get("content-type") || "";
  if (r.status === 204) return null;
  if (ct.includes("application/json")) {
    try { return await r.json(); } catch { return null; }
  }
  const text = await r.text();
  return text.length ? text : null;
}

function toApiError(status: number, body: unknown): ApiError {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const code = (typeof b.error === "string" && b.error) || (status === 401 ? "unauthorized" : "http");
    // FastAPI's default HTTPException shape is {detail: "..."}; map it too.
    const message =
      (typeof b.message === "string" && b.message) ||
      (typeof b.detail === "string" && b.detail) ||
      (typeof b.error === "string" && b.error) ||
      `HTTP ${status}`;
    const { error: _e, message: _m, ...rest } = b;
    return new ApiError(status, code, message, rest);
  }
  const msg = typeof body === "string" && body ? body : `HTTP ${status}`;
  return new ApiError(status, status === 401 ? "unauthorized" : "http", msg);
}

export async function request<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = opts.method || "GET";
  const headers: Record<string, string> = { Accept: "application/json", ...(opts.headers || {}) };
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;

  let r: Response;
  try {
    r = await fetch(path, { method, headers, body, credentials: "include", signal: opts.signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, "network", "Could not reach AZKT. Check the connection and try again.");
  }
  if (r.ok) return (await parseBody(r)) as T;
  if (opts.tolerate && opts.tolerate.includes(r.status)) return null as T;
  const parsed = await parseBody(r);
  const err = toApiError(r.status, parsed);
  if (r.status === 401) unauthorizedListeners.forEach((fn) => fn(path));
  throw err;
}

export const api = {
  get: <T = unknown>(path: string, opts: Omit<RequestOptions, "method" | "body"> = {}) =>
    request<T>(path, { ...opts, method: "GET" }),
  post: <T = unknown>(path: string, body?: unknown, opts: Omit<RequestOptions, "method" | "body"> = {}) =>
    request<T>(path, { ...opts, method: "POST", body, idempotencyKey: opts.idempotencyKey ?? newIdempotencyKey() }),
  put: <T = unknown>(path: string, body?: unknown, opts: Omit<RequestOptions, "method" | "body"> = {}) =>
    request<T>(path, { ...opts, method: "PUT", body }),
  patch: <T = unknown>(path: string, body?: unknown, opts: Omit<RequestOptions, "method" | "body"> = {}) =>
    request<T>(path, { ...opts, method: "PATCH", body }),
  del: <T = unknown>(path: string, opts: Omit<RequestOptions, "method" | "body"> = {}) =>
    request<T>(path, { ...opts, method: "DELETE" }),
};

/** POST a command. Every call carries a fresh Idempotency-Key unless one is supplied (retry the same key to retry the same command). */
export async function command<T = unknown>(
  path: string,
  body: unknown,
  opts: { idempotencyKey?: string; signal?: AbortSignal } = {},
): Promise<CommandResult<T>> {
  const raw = await request<Partial<CommandResult<T>> | null>(path, {
    method: "POST",
    body,
    idempotencyKey: opts.idempotencyKey ?? newIdempotencyKey(),
    signal: opts.signal,
  });
  // Normalise: some endpoints may answer with bare data until they adopt the envelope.
  if (raw && typeof raw === "object" && typeof (raw as CommandResult).status === "string") {
    const r = raw as CommandResult<T>;
    return {
      status: r.status,
      data: r.data ?? null,
      changed: r.changed ?? [],
      approval_id: r.approval_id ?? null,
      decision: r.decision ?? { outcome: "allowed", reasons: [], policy_version: "", permission_id: null },
      request_id: r.request_id ?? null,
    };
  }
  return {
    status: "ok",
    data: (raw as T) ?? null,
    changed: [],
    approval_id: null,
    decision: { outcome: "allowed", reasons: [], policy_version: "", permission_id: null },
    request_id: null,
  };
}

/** Plain-language message for an error, for toasts and ErrorState. */
export function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 0) return e.message;
    if (e.status === 401) return "Your session has ended. Unlock again to continue.";
    if (e.status === 403) return e.message || "You don't have permission for that.";
    if (e.status === 404) return e.message === "not_found" || !e.message ? "Not found." : e.message;
    if (e.code === "blocked") return e.message || "A check is blocking this action.";
    if (e.code === "validation_failed") return e.message || "Some details need fixing.";
    return e.message || `Request failed (${e.status}).`;
  }
  if (e instanceof Error) return e.message;
  return "Something went wrong.";
}
