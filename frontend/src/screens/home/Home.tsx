/* Home (route "/") — the owner's and manager's first screen.

   One read backs the whole page: GET /api/home?period=&start=&end=&tz=&horizon_days= (backend/app/routers/home.py),
   which returns the eight sections in spec order — status · business overview · needs decision · needs attention ·
   today · vehicle timeline · in progress · completed. The server computes every number from canonical records with
   no model call on the path, and hands back {available:false, reason} for a section this person may not see (and
   degraded:true for one that broke), so a single failing section shows its own state while the rest render.

   The reporting period and the timeline horizon live in the URL (?period=&start=&end=&horizon=), so Back returns
   to the same view. The period changes the business overview only — approvals, attention and today are never
   filtered by it. Drill-downs open GET /api/home/metrics/drilldown and link into Finance on the same cohort.
   Refresh is manual plus a 60-second poll while the tab is visible; nothing here writes. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import "../../styles/home.css";
import { useAuth } from "../../lib/auth";
import { useQuery } from "../../lib/useQuery";
import { useIsMobile } from "../../lib/viewport";
import { can } from "../../lib/perms";
import { TZ } from "../../lib/format";
import { Button, ErrorState, GlassPanel, RefreshIcon, When } from "../../ui";
import { fetchHome, periodReady, usePeopleNames, type PeriodQuery } from "./api";
import { BusinessOverviewSection, type PeriodState } from "./components/BusinessOverviewSection";
import { DecisionList } from "./components/NeedsDecision";
import { AttentionList } from "./components/NeedsAttention";
import { CompletedExpander, CompletedList, ProgressList } from "./components/Progress";
import { MetricDrilldown } from "./components/MetricDrilldown";
import { HomeSection, SectionBody } from "./components/parts";
import { StatusSummary } from "./components/StatusSummary";
import { TodayList } from "./components/Today";
import { TimelineList, VehicleTimelineControls, readHorizon, type Horizon } from "./components/VehicleTimelineSection";
import type { DrilldownMetric, HomeResp, PeriodKind } from "./types";
import { isDrilldownMetric, isPeriodKind } from "./types";

const REFRESH_MS = 60_000;
const TZ_NAME = TZ.phoenix;

/** Owner mobile: Needs your decision and Today stay one tap away without pushing a hash onto history. */
function JumpButton({ to, label, count }: { to: string; label: string; count: number }) {
  const jump = () => {
    const el = document.getElementById(to);
    if (!el) return;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    el.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
    el.focus?.({ preventScroll: true });
  };
  return (
    <button type="button" className="hm-jump__item" onClick={jump}>
      {label} <span className="hm-jump__count tnum">{count}</span>
    </button>
  );
}

export default function Home() {
  const { user } = useAuth();
  const isMobile = useIsMobile();
  const [params, setParams] = useSearchParams();

  /* ---------- URL state: what is actually applied ---------- */
  const urlPeriod: PeriodKind = isPeriodKind(params.get("period")) ? (params.get("period") as PeriodKind) : "month";
  const urlStart = params.get("start") || "";
  const urlEnd = params.get("end") || "";
  const horizon = readHorizon(params.get("horizon"));
  const openMetric = isDrilldownMetric(params.get("metric")) ? (params.get("metric") as DrilldownMetric) : null;

  /* A custom period the URL cannot satisfy (no dates yet) would be a 422, so the request keeps the last
     complete period and the form below shows what still needs filling in. */
  const applied: PeriodQuery = useMemo(() => {
    const p: PeriodQuery = { period: urlPeriod, start: urlStart, end: urlEnd };
    return periodReady(p) ? p : { period: "month" };
  }, [urlPeriod, urlStart, urlEnd]);

  /* ---------- the form the person types into ---------- */
  const [form, setForm] = useState<PeriodState>({ period: urlPeriod, start: urlStart, end: urlEnd });
  useEffect(() => { setForm({ period: urlPeriod, start: urlStart, end: urlEnd }); }, [urlPeriod, urlStart, urlEnd]);

  const writeParams = useCallback((mutate: (next: URLSearchParams) => void) => {
    const next = new URLSearchParams(params);
    mutate(next);
    setParams(next, { replace: true });
  }, [params, setParams]);

  const onPeriod = useCallback((next: PeriodState, commit?: boolean) => {
    setForm(next);
    const write = next.period !== "custom" || (commit && !!next.start && !!next.end);
    if (!write) return;
    writeParams((p) => {
      p.set("period", next.period);
      if (next.period === "custom") { p.set("start", next.start); p.set("end", next.end); }
      else { p.delete("start"); p.delete("end"); }
    });
  }, [writeParams]);

  const onHorizon = useCallback((h: Horizon) => {
    writeParams((p) => { if (h === 7) p.delete("horizon"); else p.set("horizon", String(h)); });
  }, [writeParams]);

  const onOpenMetric = useCallback((m: DrilldownMetric) => {
    writeParams((p) => p.set("metric", m));
  }, [writeParams]);

  const onCloseMetric = useCallback(() => {
    writeParams((p) => p.delete("metric"));
  }, [writeParams]);

  /* ---------- the one read ---------- */
  const q = useQuery<HomeResp>(
    (signal) => fetchHome(applied, TZ_NAME, horizon, signal),
    [applied.period, applied.start, applied.end, horizon],
  );
  const d = q.data;
  const first = (user?.display_name || "").split(" ")[0] || "there";

  /* No faster than every 60 s, and never while the tab is in the background. Coming back to the tab
     refreshes too, but the same 60-second floor applies so a tab switch cannot hammer the endpoint. */
  const lastLoad = useRef(Date.now());
  const refresh = useCallback((force: boolean) => {
    if (!force && Date.now() - lastLoad.current < REFRESH_MS) return;
    lastLoad.current = Date.now();
    q.reload();
  }, [q.reload]);

  useEffect(() => {
    const tick = () => { if (document.visibilityState === "visible") refresh(false); };
    const h = window.setInterval(tick, REFRESH_MS);
    document.addEventListener("visibilitychange", tick);
    return () => { window.clearInterval(h); document.removeEventListener("visibilitychange", tick); };
  }, [refresh]);

  const ownerName = usePeopleNames(true, user?.id);
  const vehicleNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const t of d?.vehicle_timeline.items || []) {
      map.set(t.vehicle_id, t.title || t.stock_no || `Vehicle ${t.vehicle_id.slice(0, 8)}`);
    }
    return map;
  }, [d]);
  const vehicleName = useCallback((id: string | null | undefined) => {
    if (!id) return null;
    return vehicleNames.get(id) || `Vehicle ${id.slice(0, 8)}`;
  }, [vehicleNames]);

  const counts = d?.status?.counts;
  const refreshing = q.loading && !!d;

  /* The page failed outright (not one section) — say so once and keep the retry in reach. */
  if (q.error && !d) {
    return (
      <div className="page">
        <h1>Good to see you, {first}.</h1>
        <GlassPanel clip>
          <ErrorState error={q.error} onRetry={q.reload} title="Couldn't load Home" />
        </GlassPanel>
      </div>
    );
  }

  return (
    <div className="page hm-page">
      <div className="hm-head">
        <div className="grow">
          <StatusSummary
            status={d?.status}
            loading={q.loading}
            onRetry={q.reload}
            canOpenConnections={can(user, "connections")}
            fallbackTitle={`Good to see you, ${first}.`}
          />
        </div>
        <div className="hm-head__actions">
          <Button
            size={isMobile ? "xl" : "md"}
            variant="soft"
            onClick={() => refresh(true)}
            loading={refreshing}
            iconLeft={<RefreshIcon />}
          >
            Refresh
          </Button>
        </div>
      </div>

      {q.error && d ? (
        <GlassPanel clip>
          <ErrorState error={q.error} onRetry={q.reload} title="The last refresh failed — you're seeing the previous read" />
        </GlassPanel>
      ) : null}

      {isMobile && counts ? (
        <nav className="hm-jump" aria-label="Jump to a section">
          {d?.needs_decision.available === false ? null : (
            <JumpButton to="home-decisions" label="Decisions" count={counts.needs_decision} />
          )}
          <JumpButton to="home-attention" label="Attention" count={counts.needs_attention} />
          {d?.today.available === false ? null : (
            <JumpButton to="home-today" label="Today" count={counts.today} />
          )}
        </nav>
      ) : null}

      <HomeSection id="home-overview" title="Business overview">
        <BusinessOverviewSection
          data={d?.business_overview}
          periodState={form}
          onPeriod={onPeriod}
          onOpenMetric={onOpenMetric}
          mobile={isMobile}
          refreshing={refreshing}
        />
      </HomeSection>

      <HomeSection
        id="home-decisions"
        title="Needs your decision"
        count={d?.needs_decision.available === false ? null : d?.needs_decision.total}
        link={can(user, "approve") ? <Link to="/approvals" className="fs13">All approvals</Link> : undefined}
      >
        <SectionBody
          section={d?.needs_decision}
          loading={q.loading}
          count={d?.needs_decision.items.length || 0}
          emptyTitle="Nothing waiting for a decision"
          emptyBody="Approvals arrive here with the action, what it costs, the record it touches and the deadline."
          onRetry={q.reload}
          loadingLabel="Loading approvals"
        >
          <DecisionList items={d?.needs_decision.items || []} mobile={isMobile} />
        </SectionBody>
      </HomeSection>

      <HomeSection
        id="home-attention"
        title="Needs attention"
        count={d?.needs_attention.available === false ? null : d?.needs_attention.total}
        countLabel={d && d.needs_attention.items_total > d.needs_attention.total ? `groups · ${d.needs_attention.items_total} items` : undefined}
      >
        <SectionBody
          section={d?.needs_attention}
          loading={q.loading}
          count={d?.needs_attention.groups.length || 0}
          emptyTitle="Nothing needs attention"
          emptyBody="Blocked work, missing documents, overdue promises, money to match, failed publications and stale connections collect here."
          onRetry={q.reload}
          loadingLabel="Loading problems"
        >
          <AttentionList groups={d?.needs_attention.groups || []} mobile={isMobile} ownerName={ownerName} />
        </SectionBody>
      </HomeSection>

      <HomeSection
        id="home-today"
        title="Today"
        count={d?.today.available === false ? null : d?.today.total}
        link={<Link to="/tasks" className="fs13">All tasks</Link>}
      >
        <SectionBody
          section={d?.today}
          loading={q.loading}
          count={d?.today.items.length || 0}
          emptyTitle="Nothing scheduled today"
          emptyBody="Calls, meetings and timed follow-ups from both sales pipelines and operations appear here, in Phoenix time."
          onRetry={q.reload}
          loadingLabel="Loading today"
        >
          <TodayList items={d?.today.items || []} vehicleName={vehicleName} ownerName={ownerName} />
        </SectionBody>
      </HomeSection>

      <HomeSection
        id="home-timeline"
        title="Vehicle timeline"
        count={d?.vehicle_timeline.available === false ? null : d?.vehicle_timeline.total}
        link={<Link to="/vehicles" className="fs13">All vehicles</Link>}
      >
        <div className="stack-sm">
          <VehicleTimelineControls horizon={horizon} onHorizon={onHorizon} section={d?.vehicle_timeline} />
          <SectionBody
            section={d?.vehicle_timeline}
            loading={q.loading}
            count={d?.vehicle_timeline.items.length || 0}
            emptyTitle="No vehicles to show"
            emptyBody="Each vehicle appears here with its current stage, how long it has been there and the next recorded event. Dates that were never recorded stay “Not recorded”."
            onRetry={q.reload}
            loadingLabel="Loading vehicle timelines"
            loadingRows={3}
          >
            <TimelineList items={d?.vehicle_timeline.items || []} />
          </SectionBody>
        </div>
      </HomeSection>

      <HomeSection
        id="home-progress"
        title="In progress"
        count={d?.in_progress.available === false ? null : d?.in_progress.total}
      >
        <SectionBody
          section={d?.in_progress}
          loading={q.loading}
          count={d?.in_progress.items.length || 0}
          emptyTitle="Nothing in progress"
          emptyBody="Work AZKT or a person has picked up shows here with who is waiting and the next checkpoint."
          onRetry={q.reload}
          loadingLabel="Loading work in progress"
        >
          <ProgressList items={d?.in_progress.items || []} ownerName={ownerName} />
        </SectionBody>
      </HomeSection>

      <section id="home-completed" className="hm-section">
        <CompletedExpander count={d?.completed.available === false ? 0 : d?.completed.total || 0}>
          <div className="stack-sm" style={{ marginTop: 6 }}>
            <SectionBody
              section={d?.completed}
              loading={q.loading}
              count={d?.completed.items.length || 0}
              emptyTitle="Nothing finished recently"
              emptyBody="Completed work from the last week appears here."
              emptyAction={<Button size="sm" variant="soft" to="/activity">View activity</Button>}
              onRetry={q.reload}
              loadingLabel="Loading recent outcomes"
            >
              <CompletedList items={d?.completed.items || []} />
            </SectionBody>
            <div className="row-wrap" style={{ justifyContent: "space-between" }}>
              <span className="fs12 t4 tnum">
                {d?.as_of ? <>Everything on this page as of <When iso={d.as_of} format="datetime" /> · {d.timezone}</> : null}
              </span>
              <Link to={d?.completed.link || "/activity"} className="fs13">Open Activity</Link>
            </div>
          </div>
        </CompletedExpander>
      </section>

      {/* A deep-linked ?metric= only opens once we know this role may see the overview at all — otherwise the
          dialog would exist only to show a permission error the page has already explained. */}
      {openMetric && d && d.business_overview.available !== false ? (
        <MetricDrilldown
          metric={openMetric}
          periodQuery={applied}
          tz={TZ_NAME}
          mobile={isMobile}
          onClose={onCloseMetric}
        />
      ) : null}
    </div>
  );
}
