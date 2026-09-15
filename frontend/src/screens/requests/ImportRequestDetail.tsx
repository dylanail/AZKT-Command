/* One import request: who it is for, what they must have, whether the agreement and deposit evidence exist,
   what still blocks Active Search, every candidate with its per-requirement outcomes, the candidates already
   excluded and why, the bid packets and their approvals, the purchased vehicle, activity and notes.
   Reads GET /api/import-requests/{id}; every change dispatches a command through useCommand(). */
import { useMemo, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { useCommand } from "../../lib/useCommand";
import { relativeTime, TZ } from "../../lib/format";
import {
  Badge, Button, Chip, EmptyState, ErrorState, Expander, GlassPanel, HealthLabel, KeyValues, Loading, Menu,
  Money, Notice, PageHeader, PageLoading, When, type MenuItem,
} from "../../ui";
import { openApproval } from "../approvals/useApprovalReview";
import { CandidateCard } from "./components/CandidateCard";
import { DualTime } from "./components/DualTime";
import { DeniedOrError } from "./components/DeniedPanel";
import { GateList } from "./components/GateList";
import { RequirementTiers } from "./components/RequirementTiers";
import {
  AgreementDialog, DepositRuleDialog, MarkSentDialog, NextCheckDialog, PauseDialog,
  RecordPurchaseDialog, RejectCandidateDialog, ReviseRequirementsDialog,
} from "./RequestDialogs";
import { activityPath, bidAction, matchAction, requestPath, requestTranslation } from "./api";
import {
  BID_LABEL, agreementLabel, candidateTitle, depositLabel, depositRuleSet, lifecycleLabel,
  type ActivityRow, type ApprovalRef, type Bid, type CandidateMatch, type RequestDetailResp, type Translation,
} from "./types";
import "./requests.css";

type DialogKey = "pause" | "revise" | "agreement-sent" | "agreement-signed" | "deposit-rule" | "purchase" | "next-check" | null;

export default function ImportRequestDetail() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const { run, busy } = useCommand();
  const [dialog, setDialog] = useState<DialogKey>(null);
  const [reject, setReject] = useState<{ matchId: string; label: string } | null>(null);
  const [markSent, setMarkSent] = useState<string | null>(null);

  const q = useQuery<RequestDetailResp | null>(
    (signal) => api.get<RequestDetailResp | null>(requestPath(id), { signal, tolerate: [404] }),
    [id],
  );
  const activity = useQuery<{ items: ActivityRow[] } | null>(
    (signal) => api.get<{ items: ActivityRow[] } | null>(activityPath("import_request", id), { signal, tolerate: [403, 404, 501] }),
    [id],
  );

  const write = can(user, "requests.write");
  const costs = can(user, "costs.read");
  const financeStatus = can(user, "finance.status") || costs;
  const canUpload = can(user, "intake");
  const writeReason = write ? undefined : whyNot("requests.write");

  const d = q.data;
  const r = d?.request;

  const translationsById = useMemo(() => {
    const m = new Map<string, Translation>();
    for (const t of d?.translations || []) m.set(t.id, t);
    return m;
  }, [d?.translations]);

  const ranking = useMemo(() => {
    const m = new Map<string, number>();
    (d?.candidate_ranking || []).forEach((x, i) => m.set(x.id, i + 1));
    return m;
  }, [d?.candidate_ranking]);

  const approvalsByBid = useMemo(() => {
    const m = new Map<string, ApprovalRef>();
    for (const a of d?.bid_approvals || []) if (!m.has(a.entity_id)) m.set(a.entity_id, a);
    return m;
  }, [d?.bid_approvals]);

  if (q.loading) return <PageLoading />;
  if (q.error) return <div className="page"><GlassPanel clip><DeniedOrError error={q.error} onRetry={q.reload} what="this import request" backTo="/requests" backLabel="Back to import requests" /></GlassPanel></div>;
  if (!d || !r) {
    return (
      <div className="page">
        <PageHeader title="Request not found" crumbs={[{ label: "Import requests", to: "/requests" }, { label: id }]} />
        <GlassPanel clip><EmptyState title="Nothing recorded for this request" body="It may have been merged or you may not have access to it." action={<Button size="sm" variant="soft" to="/requests">Back to import requests</Button>} /></GlassPanel>
      </div>
    );
  }

  const gate = r.gates || r.active_search_gate;
  const gateBlocked = gate?.decision !== "Allowed";
  const candidates = d.candidates || [];
  const active = candidates.filter((m) => !["passed", "rejected"].includes(m.status));
  const excluded = r.exclusions || [];
  const reload = () => { q.reload(); activity.reload(); setDialog(null); setReject(null); setMarkSent(null); };

  const doTranslate = async (candidateId: string) => {
    await run(`translate:${candidateId}`, requestTranslation(candidateId), {}, { success: "Translation requested" });
    q.reload();
  };
  const doDraft = async (matchId: string) => {
    await run(`draft:${matchId}`, matchAction(matchId, "prepare-buyer-message"), {}, { success: "Buyer message drafted" });
    q.reload();
  };

  const menu: MenuItem[] = [
    { label: "Revise requirements…", meta: `v${r.requirements_version}`, onSelect: () => setDialog("revise"), disabled: !write, disabledReason: writeReason },
    { label: "Set the deposit rule…", meta: depositRuleSet(r) ? "recorded" : "not set", onSelect: () => setDialog("deposit-rule"), disabled: !write, disabledReason: writeReason },
    { label: "Record the purchase…", onSelect: () => setDialog("purchase"), disabled: !write || r.status === "closed", disabledReason: !write ? writeReason : "This request is closed.", sepBefore: true },
    { label: "Next check…", meta: r.next_check_at ? "scheduled" : "none", onSelect: () => setDialog("next-check"), disabled: !write, disabledReason: writeReason },
  ];
  if (r.opportunity_id) menu.push({ label: "Open in Sales", to: `/sales?lead=${encodeURIComponent(r.opportunity_id)}`, sepBefore: true });
  if (r.purchased_vehicle_id) menu.push({ label: "Open the purchased vehicle", to: `/vehicles/${encodeURIComponent(r.purchased_vehicle_id)}` });

  return (
    <div className="page page-wide">
      <PageHeader
        title={r.title || "Import request"}
        crumbs={[{ label: "Import requests", to: "/requests" }, { label: d.contact?.name || "Request" }]}
        subtitle={
          <span className="row-wrap" style={{ gap: 6 }}>
            {d.contact ? <Link to={`/contacts/${encodeURIComponent(d.contact.id)}`}>{d.contact.name}</Link> : <span className="not-recorded">Buyer not linked</span>}
            {d.contact?.company ? <span>· {d.contact.company}</span> : null}
            {r.created_at ? <span>· opened {relativeTime(r.created_at)}</span> : null}
            {r.next_check_at ? <span>· next check <When iso={r.next_check_at} tz={TZ.phoenix} format="datetime" /></span> : null}
          </span>
        }
        actions={
          <>
            <Button
              variant={r.paused ? "primary" : "glass"}
              onClick={() => setDialog("pause")}
              disabled={!write || r.status === "closed"}
              disabledReason={!write ? writeReason : "This request is closed."}
            >
              {r.paused ? "Resume search" : "Pause search"}
            </Button>
            <Menu label="Request actions" align="right" items={menu} trigger={<Button variant="glass">Actions</Button>} />
          </>
        }
      >
        <div className="irq-head__tags">
          <Chip tone="soft">{lifecycleLabel(r.status)}</Chip>
          {r.paused ? <Badge tone="risk">Paused</Badge> : null}
          <Chip size="sm" tone={r.agreement_status === "signed" ? "ok" : "soft"}>Agreement: {agreementLabel(r)}</Chip>
          <Chip size="sm" tone={r.deposit_status === "confirmed" ? "ok" : r.deposit_status === "unset" ? "risk" : "soft"}>Deposit: {depositLabel(r)}</Chip>
          <Chip size="sm" tone="soft">Requirements v{r.requirements_version}</Chip>
          {r.status !== "closed" ? <HealthLabel health={gateBlocked ? "wait" : "ok"} label={gateBlocked ? "Active search blocked" : "Active search open"} /> : null}
        </div>
      </PageHeader>

      {r.paused ? (
        <Notice tone="risk" lead="Paused" action={<Button size="sm" variant="soft" onClick={() => setDialog("pause")} disabled={!write} disabledReason={writeReason}>Resume</Button>}>
          {r.paused_reason || "No reason recorded."} Matching and bids are stopped until it resumes.
        </Notice>
      ) : null}
      {!costs ? <Notice tone="neutral" lead="Money hidden">Budget, deposit and bid amounts are hidden for your role. Everything else on this page is complete.</Notice> : null}

      <div className="irq-grid">
        {/* requirements */}
        <GlassPanel className="irq-sec" radius="lg">
          <div className="irq-sec__title"><span>Requirements</span><span className="count">v{r.requirements_version}</span></div>
          <RequirementTiers
            tiers={r.requirement_tiers}
            action={<Button size="sm" variant="soft" onClick={() => setDialog("revise")} disabled={!write} disabledReason={writeReason}>Revise</Button>}
          />
          {r.requirements_history?.length ? (
            <Expander title={`Earlier versions (${r.requirements_history.length})`}>
              {r.requirements_history.slice().reverse().map((h, i) => (
                <div key={i} className="fs12">
                  v{String(h.version ?? "?")} · {h.at ? relativeTime(String(h.at)) : "time not recorded"}
                  {h.evidence || h.source_ref ? ` · evidence ${String(h.source_ref || (h.evidence as Record<string, unknown>)?.source_ref || "recorded")}` : " · no evidence recorded"}
                  {h.note ? ` · ${String(h.note)}` : ""}
                </div>
              ))}
            </Expander>
          ) : null}
        </GlassPanel>

        {/* agreement & deposit */}
        <GlassPanel className="irq-sec" radius="lg" id="agreement">
          <div className="irq-sec__title"><span>Agreement &amp; deposit</span></div>
          <dl className="irq-facts">
            <dt>Search agreement</dt>
            <dd>
              {agreementLabel(r)}
              {r.agreement_id ? <span className="t4"> · {r.agreement_id}</span> : null}
              {r.agreement_status === "signed" ? (
                <div className="fs12 t3">
                  {r.agreement_evidence?.source_ref ? <>evidence {String(r.agreement_evidence.source_ref)}</> : <span style={{ color: "var(--risk)" }}>signed, but no evidence recorded — the gate stays blocked</span>}
                  {r.agreement_evidence?.signed_at ? <> · signed <When iso={String(r.agreement_evidence.signed_at)} tz={TZ.phoenix} format="datetime" /></> : null}
                </div>
              ) : null}
            </dd>

            <dt id="deposit">Deposit rule</dt>
            <dd>
              {depositRuleSet(r) ? (
                <>
                  <Money amount={r.deposit_rule.amount ?? null} currency={r.deposit_rule.currency || "USD"} hidden={!financeStatus} />
                  {r.deposit_rule.source_ref ? <span className="t4"> · {String(r.deposit_rule.source_ref)}</span> : null}
                </>
              ) : (
                <span style={{ color: "var(--risk)" }}>Deposit rule not set — the deposit gate stays blocked</span>
              )}
            </dd>

            <dt>Deposit status</dt>
            <dd>
              {depositLabel(r)}
              {r.deposit_evidence?.remaining ? <> · <Money amount={String(r.deposit_evidence.remaining)} currency={r.deposit_rule?.currency || "USD"} hidden={!financeStatus} /> outstanding</> : null}
              {r.deposit_confirmed_at ? <div className="fs12 t3">confirmed <When iso={r.deposit_confirmed_at} tz={TZ.phoenix} format="datetime" />{r.deposit_evidence?.source_ref ? ` · evidence ${String(r.deposit_evidence.source_ref)}` : ""}</div> : null}
            </dd>
          </dl>

          <div className="irq-actions">
            <Button size="sm" variant="soft" onClick={() => setDialog("agreement-sent")} disabled={!write || r.agreement_status === "signed"} disabledReason={!write ? writeReason : "Already signed."}>
              Record as sent
            </Button>
            <Button size="sm" variant="primary" onClick={() => setDialog("agreement-signed")} disabled={!write} disabledReason={writeReason}>
              {r.agreement_status === "signed" ? "Replace the signed evidence" : "Mark signed"}
            </Button>
            <Button size="sm" variant="soft" onClick={() => setDialog("deposit-rule")} disabled={!write} disabledReason={writeReason}>
              {depositRuleSet(r) ? "Change the deposit rule" : "Set the deposit rule"}
            </Button>
            <Button size="sm" variant="ghost" to="/finance?tab=matching">Record payment</Button>
          </div>
          <span className="fs12 t4">Deposits are confirmed from payment evidence in Finance, never by hand here.</span>
        </GlassPanel>
      </div>

      {/* gate */}
      <GlassPanel className="irq-sec" radius="lg">
        <div className="irq-sec__title"><span>Active search gate</span><span className="count">{gate?.decision || "not recorded"}</span></div>
        <GateList
          gate={gate}
          footer={<span className="fs12 t4">These are the API's own checks. A stage override cannot relax them; the evidence has to exist.</span>}
        />
      </GlassPanel>

      {/* candidates */}
      <GlassPanel clip radius="lg">
        <div className="irq-sec" style={{ paddingBottom: 0 }}>
          <div className="irq-sec__title">
            <span>Candidates</span>
            <span className="count">{active.length} open · {excluded.length} excluded</span>
          </div>
          {r.status !== "active_search" ? (
            <span className="fs12 t4">This request is {lifecycleLabel(r.status).toLowerCase()}; new candidates are matched once it is in active search.</span>
          ) : null}
        </div>
        {active.length === 0 ? (
          <EmptyState title="No candidates yet" body={r.status === "active_search" ? "Matches appear as auctions list. Nothing is matched while a must-have has no checkable criterion." : "Candidates are matched once the request is in active search."} />
        ) : (
          active.map((m) => (
            <CandidateCard
              key={m.id}
              match={m}
              rank={ranking.get(m.id) || null}
              translation={m.translation_id ? translationsById.get(m.translation_id) || null : null}
              busyKey={busy}
              writeReason={writeReason}
              pendingApprovalId={translationPendingApproval(m, translationsById)}
              onOpenApproval={openApproval}
              onRequestTranslation={() => void doTranslate(m.candidate_id)}
              onPrepareMessage={() => void doDraft(m.id)}
              onMarkSent={() => setMarkSent(m.id)}
              onReject={() => setReject({ matchId: m.id, label: candidateTitle(m.candidate) })}
            />
          ))
        )}
      </GlassPanel>

      {/* exclusions */}
      {excluded.length ? (
        <GlassPanel className="irq-sec" radius="lg">
          <div className="irq-sec__title"><span>Excluded candidates</span><span className="count">{excluded.length}</span></div>
          <p className="fs13 t3" style={{ margin: 0 }}>Kept so the same unsuitable option is never offered again.</p>
          <ul className="req__lines">
            {excluded.map((x, i) => (
              <li key={`${x.candidate_id}-${i}`} className="req__line">
                <span className="req__text">
                  <Link to={`/candidates/${encodeURIComponent(x.candidate_id)}`}>{candidateLabel(candidates, x.candidate_id)}</Link>
                </span>
                <span className="req__meta">
                  <Chip size="sm" tone="soft">{x.kind}</Chip>
                  <span>{x.reason || "No reason recorded"}</span>
                  {x.at ? <span className="t4">· {relativeTime(x.at)}</span> : null}
                  {x.requirements_version ? <span className="t4">· against requirements v{x.requirements_version}</span> : null}
                </span>
              </li>
            ))}
          </ul>
        </GlassPanel>
      ) : null}

      {/* bids */}
      <GlassPanel className="irq-sec" radius="lg">
        <div className="irq-sec__title"><span>Bids</span><span className="count">{d.bids?.length || 0}</span></div>
        {(d.bids || []).length === 0 ? (
          <EmptyState align="left" title="No bid packet prepared" body="A bid is prepared on the candidate page and needs the owner's approval every time." />
        ) : (
          (d.bids || []).map((b) => <BidRow key={b.id} b={b} approval={approvalsByBid.get(b.id) || null} costs={costs} write={write} busy={busy} onRun={run} onDone={reload} />)
        )}
      </GlassPanel>

      <div className="irq-grid">
        {/* purchase + tasks */}
        <GlassPanel className="irq-sec" radius="lg" id="purchase">
          <div className="irq-sec__title"><span>Purchase &amp; work</span></div>
          <dl className="irq-facts">
            <dt>Purchased vehicle</dt>
            <dd>
              {d.purchased_vehicle ? (
                <Link to={`/vehicles/${encodeURIComponent(d.purchased_vehicle.id)}`}>{d.purchased_vehicle.title || d.purchased_vehicle.stock_no || d.purchased_vehicle.id}</Link>
              ) : <span className="not-recorded">Not recorded</span>}
              {r.purchase_evidence?.source_ref ? <div className="fs12 t3">evidence {String(r.purchase_evidence.source_ref)}</div> : null}
              {r.purchased_at ? <div className="fs12 t3">purchased <When iso={r.purchased_at} tz={TZ.phoenix} format="datetime" /></div> : null}
            </dd>
            <dt>Budget</dt>
            <dd><Money amount={r.budget_amount} currency={r.budget_currency || "USD"} hidden={!costs} /></dd>
            <dt>Open tasks</dt>
            <dd>
              {(d.tasks || []).length === 0 ? <span className="not-recorded">None</span> : (
                <ul className="req__lines">
                  {(d.tasks || []).map((t) => (
                    <li key={t.id} className="fs13">
                      <Link to={`/tasks/${encodeURIComponent(t.id)}`}>{t.title}</Link>
                      <span className="t4"> · {t.status.replace(/_/g, " ")}{t.due_at ? " · " : ""}</span>
                      {t.due_at ? <When iso={t.due_at} tz={TZ.phoenix} format="datetime" /> : null}
                    </li>
                  ))}
                </ul>
              )}
            </dd>
          </dl>
          <div className="irq-actions">
            <Button size="sm" variant="soft" onClick={() => setDialog("purchase")} disabled={!write || r.status === "closed"} disabledReason={!write ? writeReason : "This request is closed."}>Record purchase…</Button>
          </div>
        </GlassPanel>

        {/* activity */}
        <GlassPanel className="irq-sec" radius="lg">
          <div className="irq-sec__title"><span>Activity</span><span className="count">{(activity.data?.items || d.activity || []).length}</span></div>
          <ActivityList
            rows={activity.data?.items || d.activity || []}
            denied={activity.data === null && !activity.loading}
            loading={activity.loading}
          />
        </GlassPanel>
      </div>

      {/* notes */}
      <GlassPanel className="irq-sec" radius="lg">
        <div className="irq-sec__title"><span>Notes</span></div>
        {r.notes ? <p className="irq-notes">{r.notes}</p> : <span className="not-recorded">Nothing recorded</span>}
      </GlassPanel>

      {/* dialogs */}
      <PauseDialog open={dialog === "pause"} onClose={() => setDialog(null)} r={r} onDone={reload} />
      <ReviseRequirementsDialog open={dialog === "revise"} onClose={() => setDialog(null)} r={r} onDone={reload} />
      <AgreementDialog open={dialog === "agreement-sent"} mode="sent" canUpload={canUpload} onClose={() => setDialog(null)} r={r} onDone={reload} />
      <AgreementDialog open={dialog === "agreement-signed"} mode="signed" canUpload={canUpload} onClose={() => setDialog(null)} r={r} onDone={reload} />
      <DepositRuleDialog open={dialog === "deposit-rule"} onClose={() => setDialog(null)} r={r} onDone={reload} />
      <RecordPurchaseDialog open={dialog === "purchase"} canUpload={canUpload} onClose={() => setDialog(null)} r={r} onDone={reload} />
      <NextCheckDialog open={dialog === "next-check"} onClose={() => setDialog(null)} r={r} onDone={reload} />
      <RejectCandidateDialog open={!!reject} matchId={reject?.matchId || null} candidateTitle={reject?.label || "This candidate"} onClose={() => setReject(null)} onDone={reload} />
      <MarkSentDialog open={!!markSent} matchId={markSent} onClose={() => setMarkSent(null)} onDone={reload} />
    </div>
  );
}

/* ---------- helpers ---------- */
function translationPendingApproval(m: CandidateMatch, byId: Map<string, Translation>): string | null {
  const t = m.translation_id ? byId.get(m.translation_id) : null;
  if (t && t.status === "pending_approval" && t.approval_id) return t.approval_id;
  return null;
}

function candidateLabel(matches: CandidateMatch[], candidateId: string): string {
  const m = matches.find((x) => x.candidate_id === candidateId);
  return m ? candidateTitle(m.candidate) : `Candidate ${candidateId.slice(0, 8)}…`;
}

function BidRow({ b, approval, costs, write, busy, onRun, onDone }: {
  b: Bid;
  approval: ApprovalRef | null;
  costs: boolean;
  write: boolean;
  busy: (k: string) => boolean;
  onRun: ReturnType<typeof useCommand>["run"];
  onDone: () => void;
}) {
  const submit = async () => {
    const res = await onRun(`bid:${b.id}`, bidAction(b.id, "submit-for-approval"), {}, { success: "Sent for the owner's decision" });
    if (res) onDone();
  };
  const canSubmit = write && b.status === "draft";
  return (
    <div className="req__line">
      <div className="between" style={{ flexWrap: "wrap" }}>
        <span className="req__text">
          {b.auction_house || "Auction not recorded"}{b.lot_no ? ` · lot ${b.lot_no}` : ""}
          <span className="t4"> · </span>
          <Money amount={b.max_amount} currency={b.currency || "JPY"} hidden={!costs} /> max
        </span>
        <span className="row-wrap" style={{ gap: 8 }}>
          <Chip size="sm" tone={b.status === "won" ? "ok" : b.status === "invalidated" || b.status === "lost" ? "blocked" : b.status === "pending_approval" ? "wait" : "soft"}>
            {BID_LABEL[b.status] || b.status}
          </Chip>
          {approval ? <Button size="xs" variant="soft" onClick={() => openApproval(approval.id)}>Review approval</Button> : null}
          <Button size="xs" variant="primary" onClick={submit} loading={busy(`bid:${b.id}`)} disabled={!canSubmit}
            disabledReason={!write ? whyNot("requests.write") : b.status === "pending_approval" ? "Already with the owner." : `This packet is ${(BID_LABEL[b.status] || b.status).toLowerCase()}.`}>
            Submit for approval
          </Button>
          <Button size="xs" variant="ghost" to={`/candidates/${encodeURIComponent(b.candidate_id)}`}>Open candidate</Button>
        </span>
      </div>
      <span className="req__meta">
        <span>Deadline: <DualTime value={b.deadline_at} /></span>
        {b.invalidated_reason ? <span style={{ color: "var(--risk)" }}>· {b.invalidated_reason}</span> : null}
        {b.gate?.reasons?.length ? <span style={{ color: "var(--risk)" }}>· {b.gate.reasons.join(" · ")}</span> : null}
      </span>
    </div>
  );
}

function ActivityList({ rows, denied, loading }: { rows: ActivityRow[]; denied: boolean; loading: boolean }) {
  if (loading) return <Loading label="Loading activity" rows={3} />;
  if (denied && rows.length === 0) return <p className="fs13 t4" style={{ margin: 0 }}>Activity is hidden for your role.</p>;
  if (rows.length === 0) return <span className="not-recorded">Nothing recorded yet</span>;
  return (
    <div className="irq-act">
      {rows.slice(0, 40).map((a) => (
        <div key={a.id} className="irq-act__row">
          <span className="irq-act__time"><When iso={a.at} tz={TZ.phoenix} format="long" /></span>
          <span className={["irq-act__what", a.exception ? "irq-act__what--exc" : ""].filter(Boolean).join(" ")}>
            {a.what}
            {a.state ? <span className="t4"> · {a.state.replace(/_/g, " ")}</span> : null}
          </span>
        </div>
      ))}
      {rows.length > 40 ? (
        <Expander title={`${rows.length - 40} older entries`}>
          <KeyValues items={rows.slice(40).map((a): [ReactNode, ReactNode] => [<When key={a.id} iso={a.at} tz={TZ.phoenix} format="long" />, a.what])} />
        </Expander>
      ) : null}
    </div>
  );
}
