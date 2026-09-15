/* Conversation state for one role: the stored thread (web + Telegram), the live streamed reply and the
   unsent draft. Streams are aborted on unmount and when the role changes; nothing is ever re-sent silently. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, describeError } from "../../lib/api";
import { getThread, newRequestId, sendChat } from "./api";
import type { AgentRole, ChatMessage, ChatReply, ThreadTurn, ToolTrailItem } from "./types";

const DRAFT_PREFIX = "azkt.agents.draft.";

export function readDraft(role: AgentRole): string {
  try { return window.sessionStorage.getItem(DRAFT_PREFIX + role) || ""; } catch { return ""; }
}
export function writeDraft(role: AgentRole, value: string): void {
  try {
    if (value) window.sessionStorage.setItem(DRAFT_PREFIX + role, value);
    else window.sessionStorage.removeItem(DRAFT_PREFIX + role);
  } catch { /* private mode: the draft simply is not remembered */ }
}

/** Turn the stored blocks of a thread turn into the chips the screen shows. */
function fromTurn(t: ThreadTurn, i: number): ChatMessage {
  const blocks = Array.isArray(t.blocks) ? t.blocks : [];
  const attachments = blocks.filter((b) => b.type === "attachment" && b.asset_id).map((b) => String(b.asset_id));
  const changed = blocks.filter((b) => b.type === "record").map((b) => ({ kind: b.kind, id: b.id, label: b.label }));
  const mission = blocks.find((b) => b.type === "mission");
  return {
    key: t.id || `turn-${i}`,
    who: t.role === "user" ? "me" : "agent",
    text: t.content || "",
    channel: t.channel || "web",
    at: t.at,
    state: t.state || "final",
    changed,
    attachments,
    missionId: t.mission_id || mission?.id || null,
    runId: t.run_id || null,
    context: t.context || {},
  };
}

function replyIntoMessage(m: ChatMessage, r: ChatReply): ChatMessage {
  return {
    ...m,
    text: r.text || m.text,
    streaming: false,
    failure: null,
    state: r.status || "final",
    approvals: (r.approvals || []).filter((a) => a && a.id),
    changed: r.changed || [],
    neededInput: r.needed_input || null,
    missionId: r.mission_id,
    runId: r.run_id,
    cursor: r.cursor,
    context: r.context || m.context,
    usedModel: r.used_model ?? null,
    reasons: r.reasons || [],
  };
}

export interface SendPayload {
  text: string;
  attachments: string[];
  context: Record<string, unknown>;
}

export interface ChatState {
  messages: ChatMessage[];
  loading: boolean;
  error: unknown;
  sending: boolean;
  /** Last live reply, so the mission panel can follow the run it started. */
  live: ChatReply | null;
  reload: () => void;
  send: (p: SendPayload) => Promise<void>;
  /** Restores the composer after a message that never left the device. */
  restored: string;
  clearRestored: () => void;
}

export function useChat(role: AgentRole, enabled: boolean): ChatState {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [sending, setSending] = useState(false);
  const [live, setLive] = useState<ChatReply | null>(null);
  const [restored, setRestored] = useState("");
  const [tick, setTick] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => { aliveRef.current = true; return () => { aliveRef.current = false; }; }, []);

  // Load the stored conversation. Abort any stream in flight when the role changes or the screen closes.
  useEffect(() => {
    if (!enabled) { setLoading(false); setMessages([]); return; }
    const c = new AbortController();
    setLoading(true);
    setError(null);
    setLive(null);
    getThread(role, c.signal)
      .then((r) => {
        if (c.signal.aborted) return;
        setMessages((r.turns || []).map(fromTurn));
        setLoading(false);
      })
      .catch((e) => {
        if (c.signal.aborted || (e instanceof DOMException && e.name === "AbortError")) return;
        setError(e);
        setLoading(false);
      });
    return () => c.abort();
  }, [role, enabled, tick]);

  useEffect(() => () => { abortRef.current?.abort(); }, []);
  useEffect(() => { abortRef.current?.abort(); abortRef.current = null; }, [role]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const clearRestored = useCallback(() => setRestored(""), []);

  const send = useCallback(async ({ text, attachments, context }: SendPayload) => {
    const body = text.trim();
    if (!body && attachments.length === 0) return;
    const stamp = Date.now().toString(36);
    const mineKey = `me-${stamp}`;
    const replyKey = `agent-${stamp}`;
    const requestId = newRequestId();

    setSending(true);
    setLive(null);
    setMessages((prev) => [
      ...prev,
      { key: mineKey, who: "me", text: body, channel: "web", at: new Date().toISOString(), state: "final", attachments, context },
      { key: replyKey, who: "agent", text: "", channel: "web", at: null, state: "streaming", streaming: true, trail: [], context },
    ]);

    const patch = (fn: (m: ChatMessage) => ChatMessage) =>
      setMessages((prev) => prev.map((m) => (m.key === replyKey ? fn(m) : m)));

    const chunks: string[] = [];
    const c = new AbortController();
    abortRef.current?.abort();
    abortRef.current = c;

    try {
      const reply = await sendChat(
        { message: body, role, context, attachments, request_id: requestId },
        {
          onText: (t) => { chunks.push(t); patch((m) => ({ ...m, text: chunks.join("\n\n") })); },
          onToolStarted: (tool, id) => patch((m) => ({
            ...m, trail: [...(m.trail || []), { id: id || `${tool}-${(m.trail || []).length}`, tool, status: "running" } as ToolTrailItem],
          })),
          onToolFinished: (tool, status) => patch((m) => {
            const trail = [...(m.trail || [])];
            for (let i = trail.length - 1; i >= 0; i--) {
              if (trail[i].tool === tool && trail[i].status === "running") { trail[i] = { ...trail[i], status }; break; }
            }
            return { ...m, trail };
          }),
          onNeedsReview: (_tool, approval) => patch((m) => ({
            ...m, approvals: [...(m.approvals || []).filter((a) => a.id !== approval.id), approval],
          })),
        },
        c.signal,
      );
      if (!aliveRef.current || c.signal.aborted) return;
      patch((m) => replyIntoMessage(m, reply));
      setLive(reply);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      if (!aliveRef.current) return;
      const message = describeError(e);
      if (e instanceof ApiError && e.code === "not_sent") {
        // Nothing reached AZKT: take the message back so the person can send it again.
        setMessages((prev) => prev.filter((m) => m.key !== mineKey && m.key !== replyKey));
        setRestored(body);
        throw new Error(message);
      }
      patch((m) => ({ ...m, streaming: false, state: "interrupted", failure: message }));
    } finally {
      if (aliveRef.current) setSending(false);
      if (abortRef.current === c) abortRef.current = null;
    }
  }, [role]);

  return useMemo(() => ({ messages, loading, error, sending, live, reload, send, restored, clearRestored }),
    [messages, loading, error, sending, live, reload, send, restored, clearRestored]);
}
