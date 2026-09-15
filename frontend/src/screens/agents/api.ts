/* The agent endpoints (backend/app/routers/agent.py) plus a small, robust SSE reader.
   EventSource cannot POST, so chat streams come from fetch + ReadableStream. The parser splits on blank
   lines, joins multi-line `data:` fields and ignores comments, exactly as the SSE format requires. */
import { ApiError, api, newIdempotencyKey } from "../../lib/api";
import type {
  AgentRole, AgentStatus, ApprovalBrief, ChatReply, ChatRequest, CoverageResp, MissionBrief, MissionUpdate,
  RunBrief, ThreadResp,
} from "./types";

/* ---------- SSE ---------- */

export interface SseEvent { event: string; data: string }

/** Index of the next event boundary, tolerating \n\n, \r\n\r\n and \r\r. */
function nextBreak(s: string): { at: number; len: number } | null {
  let best: { at: number; len: number } | null = null;
  for (const sep of ["\r\n\r\n", "\n\n", "\r\r"]) {
    const at = s.indexOf(sep);
    if (at === -1) continue;
    if (!best || at < best.at || (at === best.at && sep.length > best.len)) best = { at, len: sep.length };
  }
  return best;
}

function parseBlock(block: string): SseEvent | null {
  let event = "message";
  const data: string[] = [];
  let sawData = false;
  for (const raw of block.split(/\r\n|\r|\n/)) {
    if (!raw || raw.startsWith(":")) continue; // blank line or comment/keep-alive
    const colon = raw.indexOf(":");
    const field = colon === -1 ? raw : raw.slice(0, colon);
    let value = colon === -1 ? "" : raw.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value || "message";
    else if (field === "data") { data.push(value); sawData = true; }
    // id / retry are not used by these streams
  }
  if (!sawData && event === "message") return null;
  return { event, data: data.join("\n") };
}

/** Read an SSE body to completion, calling `onEvent` for each event. Aborting rejects with AbortError. */
export async function readSse(body: ReadableStream<Uint8Array>, onEvent: (e: SseEvent) => void): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const flush = (block: string) => {
    const ev = parseBlock(block);
    if (ev) onEvent(ev);
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      for (let br = nextBreak(buf); br; br = nextBreak(buf)) {
        const block = buf.slice(0, br.at);
        buf = buf.slice(br.at + br.len);
        flush(block);
      }
    }
    buf += decoder.decode();
    if (buf.trim()) flush(buf);
  } finally {
    try { await reader.cancel(); } catch { /* the stream is already gone */ }
  }
}

/* ---------- chat ---------- */

export interface ChatHandlers {
  onStart?: (role: string) => void;
  onText?: (text: string) => void;
  onToolStarted?: (tool: string, id: string) => void;
  onToolFinished?: (tool: string, status: string) => void;
  onNeedsReview?: (tool: string, approval: ApprovalBrief) => void;
  /** True once at least one event arrived, so callers know a retry is not safe. */
  onAnyEvent?: () => void;
}

function asReply(raw: unknown): ChatReply {
  const o = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    text: typeof o.text === "string" ? o.text : "",
    status: typeof o.status === "string" ? o.status : "answered",
    mission_id: (o.mission_id as string | null) ?? null,
    run_id: (o.run_id as string | null) ?? null,
    cursor: Number(o.cursor) || 0,
    changed: Array.isArray(o.changed) ? (o.changed as ChatReply["changed"]) : [],
    approvals: Array.isArray(o.approvals) ? (o.approvals as ApprovalBrief[]) : [],
    needed_input: (o.needed_input as ChatReply["needed_input"]) ?? null,
    run_status: (o.run_status as string | null) ?? null,
    used_model: o.used_model === undefined ? null : !!o.used_model,
    error: (o.error as string | null) ?? null,
    wrote: !!o.wrote,
    fast_path: (o.fast_path as string | null) ?? null,
    thread_key: o.thread_key as string | undefined,
    role: o.role as string | undefined,
    context: (o.context as Record<string, unknown>) || {},
    reasons: Array.isArray(o.reasons) ? (o.reasons as string[]) : [],
    blocks: Array.isArray(o.blocks) ? (o.blocks as ChatReply["blocks"]) : [],
    intake_id: (o.intake_id as string | null) ?? null,
    replayed: !!o.replayed,
  };
}

async function errorFromResponse(r: Response): Promise<ApiError> {
  let body: unknown = null;
  try { body = await r.json(); } catch { /* not JSON */ }
  const b = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  const code = (typeof b.error === "string" && b.error) || (r.status === 401 ? "unauthorized" : "http");
  const message = (typeof b.message === "string" && b.message) || (typeof b.detail === "string" && b.detail)
    || (typeof b.error === "string" && b.error) || `Request failed (${r.status}).`;
  return new ApiError(r.status, code, message, b);
}

/**
 * POST /api/agent/chat, streaming when the server honours it.
 * Falls back to reading the plain JSON reply from the very same response when the server did not stream,
 * so nothing is posted twice. A stream that dies after events have arrived is reported, never re-sent:
 * the deterministic fast paths write without an idempotency key, so a silent retry could write twice.
 */
export async function sendChat(body: ChatRequest, h: ChatHandlers, signal?: AbortSignal): Promise<ChatReply> {
  let res: Response;
  try {
    res = await fetch("/api/agent/chat", {
      method: "POST",
      credentials: "include",
      signal,
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream, application/json",
        "Idempotency-Key": body.request_id,
      },
      body: JSON.stringify(body),
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, "not_sent", "Could not reach AZKT. Your message was not sent — try again.");
  }
  if (!res.ok) throw await errorFromResponse(res);

  const ctype = res.headers.get("content-type") || "";
  if (!ctype.includes("text/event-stream") || !res.body) {
    // The server answered with the plain JSON reply. That is the documented non-streaming shape.
    return asReply(await res.json());
  }

  // Collected in arrays: a value assigned inside a callback cannot be narrowed away by the compiler.
  const finished: ChatReply[] = [];
  const streamErrors: string[] = [];
  const seen: string[] = [];
  await readSse(res.body, (ev) => {
    seen.push(ev.event);
    h.onAnyEvent?.();
    let data: Record<string, unknown> = {};
    if (ev.data) { try { data = JSON.parse(ev.data) as Record<string, unknown>; } catch { data = { raw: ev.data }; } }
    switch (ev.event) {
      case "start": h.onStart?.(String(data.role || body.role)); break;
      case "text": if (typeof data.text === "string" && data.text) h.onText?.(data.text); break;
      case "tool_started": h.onToolStarted?.(String(data.tool || "tool"), String(data.id || "")); break;
      case "tool_finished": h.onToolFinished?.(String(data.tool || "tool"), String(data.status || "ok")); break;
      case "needs_review": h.onNeedsReview?.(String(data.tool || "tool"), (data.approval || {}) as ApprovalBrief); break;
      case "done": finished.push(asReply(data)); break;
      case "error": streamErrors.push(typeof data.error === "string" ? data.error : "The answer stopped early."); break;
      default: break;
    }
  });

  if (finished.length) return finished[finished.length - 1];
  if (seen.length === 0) {
    // Nothing streamed at all: ask once more without streaming, reusing the same request id so a mission
    // that did start replays instead of running twice.
    const raw = await api.post<unknown>("/api/agent/chat", body, { idempotencyKey: body.request_id, signal });
    return asReply(raw);
  }
  throw new ApiError(0, "stream_lost", streamErrors[0]
    || "The connection dropped before the answer finished. Nothing was lost — reload the conversation to see what was recorded.");
}

/* ---------- reads ---------- */
export const getThread = (role: AgentRole, signal?: AbortSignal, limit = 60) =>
  api.get<ThreadResp>(`/api/agent/threads/${encodeURIComponent(role)}?limit=${limit}`, { signal });

export const getStatus = (signal?: AbortSignal) => api.get<AgentStatus>("/api/agent/status", { signal });

/** Owner only; 403 for everyone else, which the screen shows as "not available to your role". */
export const getCoverage = (signal?: AbortSignal) => api.get<CoverageResp>("/api/agent/coverage", { signal });

export const getMission = (id: string, cursor = 0, signal?: AbortSignal) =>
  api.get<{ mission: MissionBrief; updates: MissionUpdate[]; runs: RunBrief[] }>(
    `/api/missions/${encodeURIComponent(id)}?cursor=${cursor}`, { signal });

export const getRun = (id: string, signal?: AbortSignal) =>
  api.get<{ run: RunBrief; mission: MissionBrief; steps: Array<Record<string, unknown>> }>(
    `/api/runs/${encodeURIComponent(id)}`, { signal });

/* ---------- run progress stream ---------- */
export interface RunStreamHandlers {
  onOpen?: (cursor: number) => void;
  onUpdate?: (u: MissionUpdate) => void;
  onDone?: (d: { mission?: MissionBrief; run?: RunBrief | null; cursor?: number }) => void;
  /** The server closed a long-lived stream on purpose; reconnect from the cursor. */
  onIdle?: (cursor: number, note: string) => void;
  onError?: (message: string) => void;
}

/** GET /api/runs/{id}/events?cursor= — one connection; the caller reconnects with the cursor it kept. */
export async function streamRunEvents(runId: string, cursor: number, h: RunStreamHandlers, signal?: AbortSignal): Promise<void> {
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/events?cursor=${cursor}`, {
    credentials: "include",
    signal,
    headers: { Accept: "text/event-stream" },
  });
  if (!res.ok) throw await errorFromResponse(res);
  if (!res.body) throw new ApiError(0, "network", "Progress updates are not streaming.");
  await readSse(res.body, (ev) => {
    let data: Record<string, unknown> = {};
    if (ev.data) { try { data = JSON.parse(ev.data) as Record<string, unknown>; } catch { data = {}; } }
    switch (ev.event) {
      case "open": h.onOpen?.(Number(data.cursor) || 0); break;
      case "update": h.onUpdate?.(data as unknown as MissionUpdate); break;
      case "done": h.onDone?.(data as { mission?: MissionBrief; run?: RunBrief | null; cursor?: number }); break;
      case "idle": h.onIdle?.(Number(data.cursor) || cursor, String(data.note || "still running")); break;
      case "error": h.onError?.(String(data.error || "The progress stream stopped.")); break;
      default: break;
    }
  });
}

/* ---------- writes ---------- */
export const cancelRunPath = (runId: string) => `/api/runs/${encodeURIComponent(runId)}/cancel`;
export const cancelMissionPath = (missionId: string) => `/api/missions/${encodeURIComponent(missionId)}/cancel`;

/** A fresh id per sent message: a retried POST is the same logical request, never a second mission. */
export const newRequestId = () => newIdempotencyKey();
