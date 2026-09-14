/* Team (owner) / People (manager). GET /api/team → {items, total, scope: all|reports}.
   Owner: table with role, reports-to, scope, status, last seen; Add person; Edit; invitations with revoke.
   Manager: read-only People list with "Assign task" → /tasks?new=1&owner=<id>. Others: denied. */
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, api } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useQuery } from "../../../lib/useQuery";
import { useCommand } from "../../../lib/useCommand";
import { useIsMobile } from "../../../lib/viewport";
import { ROLE_LABELS, whyNot, type Role } from "../../../lib/perms";
import { Avatar, Button, EmptyState, ErrorState, GlassPanel, HealthLabel, ListGroup, ListRow, Loading, PageHeader, Section, Table, Tr, When } from "../../../ui";
import { PersonDialog } from "./PersonDialog";
import { contactOf, scopeText, statusView, type InvitationsResp, type Person, type TeamResp } from "./types";

function StatusCell({ p }: { p: Person }) {
  const sv = statusView(p.status);
  return (
    <span className="stack-sm" style={{ gap: 1 }}>
      {sv.health ? <HealthLabel health={sv.health} label={sv.label} /> : <span>{sv.label}</span>}
      <span className="fs12 t3 tnum">{p.status === "disabled" ? <>since <When iso={p.disabled_at} format="date" /></> : p.last_seen_at ? <>seen <When iso={p.last_seen_at} relative /></> : "not signed in yet"}</span>
    </span>
  );
}

export function TeamSection({ level = 1 }: { level?: 1 | 2 }) {
  const { user } = useAuth();
  const isMobile = useIsMobile();
  const [params, setParams] = useSearchParams();
  const { run, busy } = useCommand();
  const team = useQuery<TeamResp>((signal) => api.get<TeamResp>("/api/team", { signal }), []);
  const owner = team.data?.scope === "all";
  const invites = useQuery<InvitationsResp | null>(async (signal) => (owner ? api.get<InvitationsResp>("/api/team/invitations", { signal }) : null), [owner]);
  const [dialog, setDialog] = useState<{ mode: "invite" | "edit"; person?: Person } | null>(null);

  useEffect(() => {
    if (params.get("new") === "1" && owner) {
      setDialog({ mode: "invite" });
      const next = new URLSearchParams(params); next.delete("new"); setParams(next, { replace: true });
    }
  }, [params, owner, setParams]);

  const people = team.data?.items || [];
  const byId = useMemo(() => Object.fromEntries(people.map((p) => [p.id, p])), [people]);
  const denied = team.error instanceof ApiError && team.error.isDenied;
  const active = people.filter((p) => p.status === "active").length;
  const pending = (invites.data?.items || []).filter((i) => i.status === "pending");
  const reload = () => { team.reload(); invites.reload(); };

  const revoke = async (id: string, version: number) => {
    const r = await run(`revoke:${id}`, `/api/team/invitations/${encodeURIComponent(id)}/revoke`, { expected_version: version }, { success: "Invitation revoked. The link no longer works." });
    if (r?.status === "ok") invites.reload();
  };

  const title = owner ? "Team" : "Your people";
  const subtitle = team.loading ? "Loading…" : owner
    ? `${active} active${pending.length ? ` · ${pending.length} invited` : ""} · role sets defaults; every permission is individually overridable.`
    : `${people.length} ${people.length === 1 ? "person reports" : "people report"} to you. The owner decides who; you assign their work.`;

  return (
    <div className="stack-lg">
      <PageHeader
        level={level}
        title={title}
        subtitle={subtitle}
        crumbs={level === 1 ? [{ label: "Settings", to: "/settings" }, { label: title }] : undefined}
        actions={owner || (!team.data && !denied) ? (
          <Button variant="primary" disabled={!owner} disabledReason={whyNot("team")} onClick={() => setDialog({ mode: "invite" })}>Add person</Button>
        ) : team.data?.scope === "reports" ? (
          <Button variant="primary" to="/tasks?new=1">Assign task</Button>
        ) : undefined}
      />

      <GlassPanel clip>
        {team.loading ? <Loading label="Loading people" rows={3} /> : denied ? (
          <EmptyState title="Not for your role" body="Team and People are for the owner and managers." />
        ) : team.error ? <ErrorState error={team.error} onRetry={team.reload} /> : !people.length ? (
          <EmptyState title={owner ? "Just you so far" : "No one reports to you yet"} body={owner ? "Invite someone; nothing changes until they accept the link." : "When the owner assigns people to you, they show here with their work."} action={owner ? <Button size="sm" variant="primary" onClick={() => setDialog({ mode: "invite" })}>Add person</Button> : undefined} />
        ) : owner && !isMobile ? (
          <Table minWidth={820} aria-label="Team">
            <thead><tr><th>Person</th><th>Does</th><th>Reports to</th><th>Can see</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {people.map((p) => (
                <Tr key={p.id} clickable onClick={() => setDialog({ mode: "edit", person: p })}>
                  <td>
                    <span className="person"><Avatar name={p.display_name} /><span className="stack-sm" style={{ gap: 0, minWidth: 0 }}><span className="person__name">{p.display_name}{p.id === user?.id ? <span className="t3"> · you</span> : null}</span><span className="person__sub">{contactOf(p)}</span></span></span>
                  </td>
                  <td>{ROLE_LABELS[p.role as Role] || p.role}</td>
                  <td className="t2">{p.role === "owner" ? "—" : p.manager_id ? (byId[p.manager_id]?.display_name || "Someone no longer here") : <span className="t4">Nobody yet</span>}</td>
                  <td className="t2">{scopeText(p.scope, p.role)}</td>
                  <td><StatusCell p={p} /></td>
                  <td className="actions"><Button size="sm" variant="soft" onClick={(e) => { e.stopPropagation(); setDialog({ mode: "edit", person: p }); }}>Edit</Button></td>
                </Tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <div className="list">
            {people.map((p) => {
              const sv = statusView(p.status);
              return (
                <ListRow
                  key={p.id}
                  leading={<Avatar name={p.display_name} />}
                  title={<>{p.display_name}{p.id === user?.id ? <span className="t3"> · you</span> : null}</>}
                  health={sv.health || undefined}
                  healthLabel={sv.label}
                  meta={<>{ROLE_LABELS[p.role as Role] || p.role} · {contactOf(p)}{owner ? <> · {scopeText(p.scope, p.role)}</> : null}</>}
                  right={owner ? <Button size="sm" variant="soft" onClick={() => setDialog({ mode: "edit", person: p })}>Edit</Button> : <Button size="sm" variant="soft" to={`/tasks?new=1&owner=${encodeURIComponent(p.id)}`}>Assign task</Button>}
                />
              );
            })}
          </div>
        )}
      </GlassPanel>

      {owner ? (
        <Section title="Invitations" count={pending.length} link={pending.length ? <span className="fs13 t3">Links expire on their own</span> : undefined}>
          <ListGroup aria-label="Invitations">
            {invites.loading ? <Loading rows={1} /> : invites.error ? <ErrorState error={invites.error} onRetry={invites.reload} /> : !(invites.data?.items || []).length ? (
              <EmptyState align="left" title="No invitations yet" body="Each invitation is a one-time link; AZKT stores only its hash." />
            ) : (invites.data?.items || []).slice(0, 12).map((i) => {
              const sv = statusView(i.status);
              return (
                <ListRow
                  key={i.id}
                  title={i.display_name}
                  health={sv.health || undefined}
                  healthLabel={sv.label}
                  meta={<>{ROLE_LABELS[i.role as Role] || i.role} · {contactOf(i)}{i.status === "pending" && i.expires_at ? <> · expires <When iso={i.expires_at} relative /></> : i.accepted_at ? <> · accepted <When iso={i.accepted_at} format="date" /></> : i.revoked_at ? <> · revoked <When iso={i.revoked_at} format="date" /></> : null}</>}
                  right={i.status === "pending" ? <Button size="sm" variant="soft" loading={busy(`revoke:${i.id}`)} onClick={() => revoke(i.id, i.version)}>Revoke</Button> : undefined}
                />
              );
            })}
          </ListGroup>
        </Section>
      ) : null}

      <div className="set-foot">
        {owner
          ? "What someone does decides what they see. You choose who each person reports to; a manager assigns work to those people but cannot add people or verify finished work. Blockers always reach you."
          : "The owner decides who reports to you. When one of them is blocked, the owner is told directly; you see it here too. Finished work waits for the owner's verification."}
        {!owner && team.data?.scope === "reports" ? <> Open <Link to="/tasks">Tasks</Link> to see their work.</> : null}
      </div>

      {owner ? <PersonDialog open={!!dialog} mode={dialog?.mode || "invite"} person={dialog?.person} people={people} onClose={() => setDialog(null)} onChanged={reload} /> : null}
    </div>
  );
}
