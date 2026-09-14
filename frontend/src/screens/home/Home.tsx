/* Home: status sentence → Needs your decision → Needs attention → In progress → Completed today (collapsed).
   TODO(screen builder): GET /api/home (status line, sync time), GET /api/approvals?status=pending,
   GET /api/attention, GET /api/progress, GET /api/activity?completed=today. Manager variant adds
   GET /api/team/people (Your people today) and "Waiting on Dylan". Review → /approvals/:id. */
import { Link } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { Button, EmptyState, ErrorState, Expander, GlassPanel, Loading, Section, When } from "../../ui";
import { countOf } from "../scaffold";

interface HomeResp { status_line?: string; synced_at?: string | null; connections?: number }

export default function Home() {
  const { user } = useAuth();
  const home = useQuery<HomeResp | null>((signal) => api.get<HomeResp | null>("/api/home", { signal, tolerate: [404, 501] }), []);
  const approvals = useQuery<unknown>((signal) => api.get<unknown>("/api/approvals?status=pending", { signal, tolerate: [404, 501] }), []);
  const isOwner = user?.role === "owner";
  const isManager = user?.role === "manager";
  const first = (user?.display_name || "").split(" ")[0] || "there";

  return (
    <div className="page">
      <div>
        <h1>{home.data?.status_line || `Good to see you, ${first}.`}</h1>
        <div className="fs13 t3 tnum" style={{ marginTop: 6 }}>
          {home.loading ? "Checking connections…" : home.data?.synced_at ? <>Based on {home.data.connections ?? "your"} connections · last successful sync <When iso={home.data.synced_at} format="short" /></> : "Status will appear once connections report in."}
        </div>
      </div>

      {isOwner ? (
        <Section title="Needs your decision" count={countOf(approvals.data) ?? undefined} link={<Link to="/tasks" className="fs13">All approvals</Link>}>
          <GlassPanel clip>
            {approvals.loading ? <Loading rows={2} /> : approvals.error ? <ErrorState error={approvals.error} onRetry={approvals.reload} /> :
              <EmptyState title="Nothing waiting for a decision" body="Approvals land here with the exact payload, recipient and checks." />}
          </GlassPanel>
        </Section>
      ) : null}

      {isManager ? (
        <Section title="Your people today" link={<Link to="/settings/team" className="fs13">All people</Link>}>
          <GlassPanel clip><EmptyState title="No one reports to you yet" body="People assigned to you show here with today's work." /></GlassPanel>
        </Section>
      ) : null}

      <Section title="Needs attention">
        <GlassPanel clip><EmptyState title="Nothing needs attention" body="Blocked and at-risk vehicles, overdue tasks and stale promises show here." /></GlassPanel>
      </Section>

      <Section title="In progress">
        <GlassPanel clip><EmptyState title="Nothing in progress" body="Vehicles moving through sourcing, shipping, shop and sale appear here." /></GlassPanel>
      </Section>

      <section>
        <Expander title={<span style={{ fontSize: 15, fontWeight: 600, color: "var(--text)" }}>Completed today</span>}>
          <GlassPanel clip style={{ marginTop: 6 }}>
            <EmptyState title="Nothing finished yet today" action={<Button size="sm" variant="soft" to="/activity">View activity</Button>} />
          </GlassPanel>
        </Expander>
      </section>
    </div>
  );
}
