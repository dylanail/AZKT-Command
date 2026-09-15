/* The seven roles. A fixed rail on desktop, a horizontally scrollable strip of cards on a phone.
   Each card shows the health and the live counts from GET /api/agent/status — what is actually running,
   never a made-up "thinking…" line. */
import { GlassPanel, HealthLabel, When } from "../../../ui";
import { AGENT_ROLES, ROLE_META, healthWords, statusLine, type AgentRole, type AgentStatus, type RoleStatus } from "../types";

export interface RoleRailProps {
  value: AgentRole;
  onSelect: (role: AgentRole) => void;
  status: AgentStatus | null;
  mobile: boolean;
}

function byRole(status: AgentStatus | null, role: AgentRole): RoleStatus | undefined {
  return status?.roles?.find((r) => r.role === role);
}

export function RoleRail({ value, onSelect, status, mobile }: RoleRailProps) {
  return (
    <nav className={mobile ? "ag-rail ag-rail--strip quiet-scroll" : "ag-rail"} aria-label="Choose who you are talking to">
      {AGENT_ROLES.map((role) => {
        const meta = ROLE_META[role];
        const s = byRole(status, role);
        const h = healthWords(s?.health || "");
        const on = role === value;
        return (
          <button
            key={role}
            type="button"
            className={["ag-rolecard", on ? "ag-rolecard--on" : ""].filter(Boolean).join(" ")}
            aria-current={on ? "true" : undefined}
            onClick={() => onSelect(role)}
          >
            <span className="ag-rolecard__top">
              <span className="ag-rolecard__name">{meta.label}</span>
              {s ? <HealthLabel health={h.tone} label={h.label} dot /> : <span className="fs12 t4 nowrap">No status</span>}
            </span>
            <span className="ag-rolecard__blurb">{meta.blurb}</span>
            <span className="ag-rolecard__status tnum">{statusLine(s)}</span>
          </button>
        );
      })}
    </nav>
  );
}

export function RoleStatusPanel({ role, status }: { role: AgentRole; status: AgentStatus | null }) {
  const s = byRole(status, role);
  if (!s) return null;
  const doing = s.doing || [];
  const waiting = s.waiting || [];
  if (!doing.length && !waiting.length) return null;
  return (
    <GlassPanel padded className="stack-sm" aria-label={`What ${ROLE_META[role].label} is handling`}>
      <div className="eyebrow">Running now</div>
      {doing.length ? (
        <ul className="ag-worklist">
          {doing.map((m) => (
            <li key={m.mission_id}>
              <span className="grow">{m.outcome}</span>
              <span className="fs12 t3 nowrap">{m.status}</span>
              {m.started_at ? <When iso={m.started_at} relative className="fs12 t4 nowrap" /> : null}
            </li>
          ))}
        </ul>
      ) : <div className="fs13 t3">Nothing running.</div>}
      {waiting.length ? (
        <>
          <div className="eyebrow">Waiting</div>
          <ul className="ag-worklist">
            {waiting.map((m) => (
              <li key={m.mission_id}>
                <span className="grow">{m.waiting_on || m.status.replace(/_/g, " ")}</span>
                {m.next_check_at ? <span className="fs12 t4 nowrap">next check <When iso={m.next_check_at} relative /></span> : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </GlassPanel>
  );
}
