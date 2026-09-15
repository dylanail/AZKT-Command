/* Agents — the Manager and its six specialists (/agents and /agents/:agentId).
   Reads:  GET /api/agent/status · GET /api/agent/threads/{role} · GET /api/agent/coverage (owner)
           GET /api/runs/{id} · GET /api/runs/{id}/events (SSE)
   Writes: POST /api/agent/chat (SSE when the server streams it) · POST /api/runs/{id}/cancel
   The agent proposes; anything consequential returns an approval and waits for a signed-in review. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import "../../styles/agents.css";
import { describeError } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { useIsMobile } from "../../lib/viewport";
import { Button, EmptyState, ErrorState, GlassPanel, Loading, Notice, PageHeader, Tabs, When, useToast } from "../../ui";
import { getStatus } from "./api";
import { parsePinnedContext } from "./context";
import { contextDiffers } from "./context";
import { Composer } from "./components/Composer";
import { Coverage } from "./components/Coverage";
import { MissionPanel } from "./components/MissionPanel";
import { RoleRail, RoleStatusPanel } from "./components/RoleRail";
import { Turn } from "./components/Turn";
import { useChat } from "./useChat";
import { AGENT_ROLES, ROLE_META, TERMINAL_MISSION, isAgentRole, type AgentRole, type AgentStatus } from "./types";

type TabId = "chat" | "coverage";
const STATUS_REFRESH_MS = 30000;

/** One plain sentence about the model, taken from /api/agent/status — never a red error. */
function modelNote(status: AgentStatus | null): string | null {
  const m = status?.model;
  if (!m) return null;
  if (!m.available) return "The AI model is unavailable right now. Straight lookups — vehicle status, today, overdue, photo and voice intake — still work from your records.";
  if (m.over_cap) return "AI is off (budget). Today's model spend has reached the cap, so answers come from your records only.";
  if (!m.budget_configured) return "The model budget isn't set up yet, so the AI stays off. Straight lookups still work from your records.";
  return null;
}

export default function Agents() {
  const { agentId } = useParams();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const { user } = useAuth();
  const { toast } = useToast();
  const isMobile = useIsMobile();

  const owner = user?.role === "owner";
  const mayChat = can(user, "agents.chat");
  const role: AgentRole = isAgentRole(agentId) ? agentId : "manager";
  const tab: TabId = params.get("tab") === "coverage" && owner ? "coverage" : "chat";

  const [pinnedCleared, setPinnedCleared] = useState(false);
  const pinned = useMemo(
    () => (pinnedCleared ? null : parsePinnedContext(params.get("context"), params.get("label"))),
    [params, pinnedCleared],
  );

  const [statusTick, setStatusTick] = useState(0);
  const statusQ = useQuery<AgentStatus>((signal) => getStatus(signal), [statusTick]);
  const status = statusQ.data;

  useEffect(() => {
    const h = window.setInterval(() => {
      if (document.visibilityState === "visible") setStatusTick((t) => t + 1);
    }, STATUS_REFRESH_MS);
    return () => window.clearInterval(h);
  }, []);

  const chat = useChat(role, tab === "chat");
  const [focusSignal, setFocusSignal] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  const lastAgentKey = useMemo(() => {
    for (let i = chat.messages.length - 1; i >= 0; i--) if (chat.messages[i].who === "agent") return chat.messages[i].key;
    return null;
  }, [chat.messages]);
  const lastText = chat.messages.length ? chat.messages[chat.messages.length - 1].text : "";

  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [chat.messages.length, lastText, chat.loading]);

  // A question from the agent puts the cursor back in the box.
  useEffect(() => {
    if (chat.live?.needed_input) setFocusSignal((n) => n + 1);
  }, [chat.live]);

  const selectRole = useCallback((next: AgentRole) => {
    const q = params.toString();
    nav(`/agents/${next}${q ? `?${q}` : ""}`);
  }, [nav, params]);

  const setTab = useCallback((next: TabId) => {
    const p = new URLSearchParams(params);
    if (next === "chat") p.delete("tab"); else p.set("tab", next);
    setParams(p, { replace: true });
  }, [params, setParams]);

  const clearPinned = useCallback(() => {
    setPinnedCleared(true);
    const p = new URLSearchParams(params);
    p.delete("context");
    p.delete("label");
    setParams(p, { replace: true });
  }, [params, setParams]);

  const handleSend = useCallback(async (text: string, attachments: string[]) => {
    try {
      await chat.send({ text, attachments, context: pinned?.context || {} });
      setStatusTick((t) => t + 1);
    } catch (e) {
      toast({ title: "Not sent", message: describeError(e), tone: "risk", duration: 6000 });
    }
  }, [chat, pinned, toast]);

  const note = modelNote(status);
  const live = chat.live;
  const missionOpen = !!live?.mission_id && !TERMINAL_MISSION.has(live.status);
  const echoDiffers = !!live && contextDiffers(pinned?.context, live.context);

  const tabs = owner
    ? [{ id: "chat" as const, label: "Chat" }, { id: "coverage" as const, label: "Coverage" }]
    : [{ id: "chat" as const, label: "Chat" }];

  return (
    <div className="page page-wide ag-page">
      <PageHeader
        title="Agents"
        subtitle={`${ROLE_META[role].label} — ${ROLE_META[role].blurb} It proposes; anything consequential waits for your approval.`}
        actions={status?.as_of ? <span className="fs12 t4 nowrap">Checked <When iso={status.as_of} relative /></span> : undefined}
      >
        {owner ? <Tabs<TabId> label="Agents sections" value={tab} onChange={setTab} tabs={tabs} /> : null}
      </PageHeader>

      {statusQ.error ? (
        <Notice tone="risk" lead="Status unavailable" action={<Button size="sm" variant="soft" onClick={statusQ.reload}>Try again</Button>}>
          {describeError(statusQ.error)} The conversation below still works.
        </Notice>
      ) : null}
      {note ? <Notice tone="wait" lead="AI">{note}</Notice> : null}
      {!mayChat ? (
        <Notice tone="neutral" lead="Read only">{whyNot("agents.chat")} You can read this conversation but not add to it.</Notice>
      ) : null}

      {tab === "coverage" ? (
        <Coverage />
      ) : (
        <div className={isMobile ? "ag-layout ag-layout--mobile" : "ag-layout"}>
          <div className="ag-side">
            <RoleRail value={role} onSelect={selectRole} status={status ?? null} mobile={isMobile} />
            {!isMobile ? <RoleStatusPanel role={role} status={status ?? null} /> : null}
          </div>

          <section className="ag-main" aria-label={`Conversation with ${ROLE_META[role].label}`}>
            {isMobile ? <RoleStatusPanel role={role} status={status ?? null} /> : null}

            {echoDiffers ? (
              <Notice tone="risk" lead="Different record">
                The answer is about a different record than the one pinned above. Check the links on the reply before acting on it.
              </Notice>
            ) : null}

            <GlassPanel clip className="ag-thread">
              <div ref={listRef} className="ag-thread__scroll quiet-scroll">
                {chat.loading ? (
                  <Loading label="Loading the conversation" rows={3} />
                ) : chat.error ? (
                  <ErrorState error={chat.error} onRetry={chat.reload} title="Couldn't load this conversation" />
                ) : chat.messages.length === 0 ? (
                  <EmptyState
                    align="left"
                    title={`Ask the ${ROLE_META[role].label} anything about your trucks, tasks or customers`}
                    body="It answers from your records and asks before doing anything consequential. Messages you send here and on Telegram are the same conversation."
                  />
                ) : (
                  chat.messages.map((m) => (
                    <Turn key={m.key} m={m} role={role} live={m.key === lastAgentKey} onReload={chat.reload} />
                  ))
                )}
              </div>
            </GlassPanel>

            {missionOpen || live?.run_id ? (
              <MissionPanel
                runId={live?.run_id || null}
                missionId={live?.mission_id || null}
                startCursor={live?.cursor || 0}
                onChanged={chat.reload}
              />
            ) : null}

            <Composer
              role={role}
              pinned={pinned}
              onClearPinned={clearPinned}
              sending={chat.sending}
              blocked={mayChat ? null : whyNot("agents.chat")}
              restored={chat.restored}
              onRestoredConsumed={chat.clearRestored}
              focusSignal={focusSignal}
              onSend={handleSend}
            />
          </section>
        </div>
      )}

      {tab === "chat" ? (
        <div className="fs12 t4">
          {AGENT_ROLES.length} roles share your records and your permissions — the same rules apply whichever one you ask.
        </div>
      ) : null}
    </div>
  );
}
