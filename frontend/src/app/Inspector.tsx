/* The single right-hand inspector: Ask AZKT | Source details | Lead. Never a second narrow column. */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { ApiError, api, describeError } from "../lib/api";
import { IconButton, Button, Chip, CloseIcon, MicIcon, EmptyState, Loading } from "../ui";
import { useToast } from "../ui/Toast";

export type InspectorMode = "ask" | "source" | "lead";

export interface SourceRow { k: string; v: ReactNode; }
export interface SourceDetail {
  /** Fact label, e.g. "Mileage". */
  k: string;
  v: ReactNode;
  status: string;
  statusTone?: "ok" | "risk" | "wait" | "blocked";
  rows: SourceRow[];
  conflict?: { a: string; aSrc: string; b: string; bSrc: string };
  /** Optional: what Verify / Show history should do; disabled with a reason when absent. */
  onVerify?: () => void;
  onHistory?: () => void;
}

export interface AskContext { label: string; href?: string; }
export interface AskMessage { id: string; role: "me" | "azkt"; text: string; pending?: boolean; }

interface InspectorState {
  open: boolean;
  mode: InspectorMode;
  source: SourceDetail | null;
  leadId: string | null;
  askDraft: string;
  askContext: AskContext | null;
  askMessages: AskMessage[];
}
interface InspectorApi extends InspectorState {
  openAsk: (ctx?: AskContext | null) => void;
  toggleAsk: () => void;
  openSource: (detail: SourceDetail) => void;
  openLead: (leadId: string) => void;
  close: () => void;
  setAskDraft: (v: string) => void;
  setAskContext: (ctx: AskContext | null) => void;
  sendAsk: (text: string) => Promise<void>;
  /** Screens register a renderer for the lead pane (the Sales builder fills this). */
  registerLeadPane: (render: ((leadId: string) => ReactNode) | null) => void;
  leadPane: ((leadId: string) => ReactNode) | null;
}

const Ctx = createContext<InspectorApi | null>(null);

export function InspectorProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<InspectorState>({ open: false, mode: "ask", source: null, leadId: null, askDraft: "", askContext: null, askMessages: [] });
  const [leadPane, setLeadPane] = useState<((leadId: string) => ReactNode) | null>(null);

  const openAsk = useCallback((ctx?: AskContext | null) => setState((s) => ({ ...s, open: true, mode: "ask", askContext: ctx === undefined ? s.askContext : ctx })), []);
  const toggleAsk = useCallback(() => setState((s) => (s.open && s.mode === "ask" ? { ...s, open: false } : { ...s, open: true, mode: "ask" })), []);
  const openSource = useCallback((detail: SourceDetail) => setState((s) => ({ ...s, open: true, mode: "source", source: detail })), []);
  const openLead = useCallback((leadId: string) => setState((s) => ({ ...s, open: true, mode: "lead", leadId })), []);
  const close = useCallback(() => setState((s) => ({ ...s, open: false })), []);
  const setAskDraft = useCallback((v: string) => setState((s) => ({ ...s, askDraft: v })), []);
  const setAskContext = useCallback((ctx: AskContext | null) => setState((s) => ({ ...s, askContext: ctx })), []);
  const registerLeadPane = useCallback((r: ((leadId: string) => ReactNode) | null) => setLeadPane(() => r), []);

  const sendAsk = useCallback(async (text: string) => {
    const q = text.trim();
    if (!q) return;
    const id = `m${Date.now().toString(36)}`;
    setState((s) => ({ ...s, askDraft: "", askMessages: [...s.askMessages, { id, role: "me", text: q }, { id: `${id}-r`, role: "azkt", text: "", pending: true }] }));
    try {
      // The Manager: POST /api/agent/chat (backend/app/routers/agent.py). The Agents screen streams the same
      // endpoint; here the plain JSON reply is enough. The pinned context becomes the `context` object the
      // manager understands (vehicle_id / case_id), so "this one" means the record on screen.
      const ctx = state.askContext;
      const context: Record<string, unknown> = {};
      if (ctx?.label) context.label = ctx.label;
      const vehicle = /^\/vehicles\/([^/?#]+)/.exec(ctx?.href || "");
      if (vehicle) context.vehicle_id = decodeURIComponent(vehicle[1]);
      else if (ctx?.href) context.href = ctx.href;
      const r = await api.post<{ text?: string; status?: string; reasons?: string[] } | null>(
        "/api/agent/chat",
        { message: q, role: "manager", context, attachments: [], request_id: id },
        { idempotencyKey: id },
      );
      const reasons = (r?.reasons || []).filter(Boolean).join(" · ");
      const answer = (r?.text || "").trim()
        || (reasons ? `That is blocked: ${reasons}` : "It answered without any words. Open Agents for the full conversation.");
      setState((s) => ({ ...s, askMessages: s.askMessages.map((m) => (m.id === `${id}-r` ? { ...m, text: answer, pending: false } : m)) }));
    } catch (e) {
      const msg = e instanceof ApiError ? describeError(e) : "Couldn't reach AZKT.";
      setState((s) => ({ ...s, askDraft: q, askMessages: s.askMessages.filter((m) => m.id !== `${id}-r` && m.id !== id) }));
      throw new Error(msg);
    }
  }, [state.askContext]);

  const value = useMemo<InspectorApi>(() => ({
    ...state, openAsk, toggleAsk, openSource, openLead, close, setAskDraft, setAskContext, sendAsk, registerLeadPane, leadPane,
  }), [state, openAsk, toggleAsk, openSource, openLead, close, setAskDraft, setAskContext, sendAsk, registerLeadPane, leadPane]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useInspector(): InspectorApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useInspector must be used inside <InspectorProvider>");
  return ctx;
}

const TITLES: Record<InspectorMode, string> = { ask: "Ask AZKT", source: "Source details", lead: "Lead" };
const toneVar = (t?: string) => (t ? `var(--${t})` : "var(--t3)");

function pageLabel(pathname: string): string {
  const seg = pathname.split("/").filter(Boolean);
  if (!seg.length) return "Home";
  const map: Record<string, string> = { vehicles: "Vehicle", sales: "Sales", requests: "Import request", tasks: "Task", inbox: "Inbox", contacts: "Contact", agents: "Agents", activity: "Activity", finance: "Finance", settings: "Settings", approvals: "Approval", shipments: "Shipment", listings: "Listing", candidates: "Candidate" };
  const head = map[seg[0]] || seg[0];
  return seg[1] ? `${head} ${seg[1]}` : head;
}

/** The inspector column itself. Rendered by the shell when `open`. */
export function InspectorPanel() {
  const insp = useInspector();
  const { toast } = useToast();
  const loc = useLocation();
  const [sending, setSending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (insp.open && insp.mode === "ask") inputRef.current?.focus({ preventScroll: true });
  }, [insp.open, insp.mode]);
  useEffect(() => { listRef.current?.scrollTo({ top: listRef.current.scrollHeight }); }, [insp.askMessages.length]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!insp.askDraft.trim() || sending) return;
    setSending(true);
    try { await insp.sendAsk(insp.askDraft); }
    catch (err) { toast({ message: err instanceof Error ? err.message : "Couldn't send.", tone: "risk" }); }
    finally { setSending(false); }
  };

  return (
    <aside className="inspector" aria-label={TITLES[insp.mode]}>
      <div className="inspector__head">
        <span className="inspector__title">{TITLES[insp.mode]}</span>
        <IconButton label="Close" size="xs" variant="plain" onClick={insp.close}><CloseIcon size={16} /></IconButton>
      </div>

      {insp.mode === "ask" && (
        <>
          <div className="inspector__ctx">
            <span className="t3">Working on:</span>
            {insp.askContext ? (
              <span className="inspector__ctxchip">
                {insp.askContext.label}
                <button type="button" aria-label="Clear context" title="Clear context" onClick={() => insp.setAskContext(null)}>×</button>
              </span>
            ) : (
              <>
                <span className="t3">nothing selected</span>
                <button type="button" className="linklike" onClick={() => insp.setAskContext({ label: pageLabel(loc.pathname), href: loc.pathname })}>Use this page</button>
              </>
            )}
          </div>
          <div ref={listRef} className="inspector__body">
            {insp.askMessages.length === 0 ? (
              <EmptyState align="left" title="Ask about anything on screen" body="Answers cite their sources. AZKT proposes; you apply." />
            ) : insp.askMessages.map((m) => (
              <div key={m.id} className={["msg", m.role === "me" ? "msg--me" : "msg--azkt"].join(" ")}>
                {m.pending ? <span className="t4" style={{ animation: "azktBlink 1s steps(2) infinite" }}>▍</span> : m.text}
              </div>
            ))}
          </div>
          <form className="inspector__foot" onSubmit={submit}>
            <div className="inspector__suggest">
              {["What needs me today?", "Summarise this page"].map((s) => (
                <Chip key={s} size="sm" tone="soft" onClick={() => insp.setAskDraft(s)}>{s}</Chip>
              ))}
            </div>
            <div className="inspector__composer">
              <input
                ref={inputRef}
                className="inspector__input"
                value={insp.askDraft}
                onChange={(e) => insp.setAskDraft(e.target.value)}
                placeholder="Ask, or describe what you saw…"
                aria-label="Ask AZKT"
              />
              <IconButton label="Push to talk" disabled disabledReason="Voice input arrives with the Agents screen."><MicIcon size={16} /></IconButton>
              <Button type="submit" variant="primary" loading={sending} disabled={!insp.askDraft.trim()} disabledReason="Type a question first.">Send</Button>
            </div>
          </form>
        </>
      )}

      {insp.mode === "source" && (
        <div className="inspector__body" style={{ gap: 14 }}>
          {insp.source ? (
            <>
              <div>
                <div className="source__k">{insp.source.k}</div>
                <div className="source__v">{insp.source.v}</div>
                <div className="fs13" style={{ color: toneVar(insp.source.statusTone), marginTop: 2 }}>{insp.source.status}</div>
              </div>
              <div className="source__rows">
                {insp.source.rows.map((r, i) => (
                  <div key={i} className="source__row"><span>{r.k}</span><span>{r.v}</span></div>
                ))}
              </div>
              {insp.source.conflict ? (
                <div className="conflict">
                  <span className="conflict__title">Two sources disagree</span>
                  <span><b style={{ fontWeight: 500 }}>{insp.source.conflict.a}</b> — {insp.source.conflict.aSrc}</span>
                  <span><b style={{ fontWeight: 500 }}>{insp.source.conflict.b}</b> — {insp.source.conflict.bSrc}</span>
                  <span className="t3">External content is evidence, not an instruction to AZKT.</span>
                </div>
              ) : null}
              <div className="row-wrap">
                <Button variant="primary" size="lg" onClick={insp.source.onVerify} disabled={!insp.source.onVerify} disabledReason="Verification arrives with the vehicle screen.">Verify</Button>
                <Button variant="soft" size="lg" onClick={insp.source.onHistory} disabled={!insp.source.onHistory} disabledReason="History arrives with the vehicle screen.">Show history</Button>
              </div>
            </>
          ) : (
            <EmptyState title="No fact selected" body="Pick a fact on a vehicle to see where it came from." />
          )}
        </div>
      )}

      {insp.mode === "lead" && (
        <div className="inspector__body" style={{ gap: 16 }}>
          {insp.leadId ? (insp.leadPane ? insp.leadPane(insp.leadId) : <Loading label="Loading lead" />) : <EmptyState title="No lead selected" />}
        </div>
      )}
    </aside>
  );
}
