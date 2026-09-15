/* One auction candidate: its identity and snapshot, the deadline in Japan and Arizona, the recorded
   specifications with where each value came from, and — one buyer at a time — how it scores against that
   request's requirements, the scoped buyer draft and the exact bid packet.
   Reads GET /api/candidates/{id}. A request tab never shows another buyer's request beside it. */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { useCommand } from "../../lib/useCommand";
import { useIsMobile } from "../../lib/viewport";
import { relativeTime, TZ } from "../../lib/format";
import {
  Button, Chip, EmptyState, ErrorState, Expander, Field, GlassPanel, HealthLabel, Input, KeyValues, Loading, Menu,
  Money, Notice, PageHeader, PageLoading, ResponsiveDialog, Select, Table, Tabs, Textarea, When, type MenuItem,
} from "../../ui";
import { JsonDetail } from "../shared/JsonDetail";
import { openApproval } from "../approvals/useApprovalReview";
import { DualTime, deadlineTone } from "../requests/components/DualTime";
import { MarkSentDialog, RejectCandidateDialog } from "../requests/RequestDialogs";
import { bidAction, matchAction, prepareBid, candidatePath, requestTranslation, CURRENCIES } from "../requests/api";
import {
  BID_LABEL, CHECK_LABEL, TRANSLATION_LABEL, auctionIdentity, bidReadyReason, candidateTitle, checkTone,
  type ApprovalRef, type Bid, type CandidateDetailResp, type CandidateMatch, type Translation,
} from "../requests/types";
import "../requests/requests.css";

export default function CandidateDetail() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const { user } = useAuth();
  const { run, busy } = useCommand();
  const [prepare, setPrepare] = useState(false);
  const [result, setResult] = useState<Bid | null>(null);
  const [reject, setReject] = useState<CandidateMatch | null>(null);
  const [markSent, setMarkSent] = useState<string | null>(null);

  const q = useQuery<CandidateDetailResp | null>(
    (signal) => api.get<CandidateDetailResp | null>(candidatePath(id), { signal, tolerate: [404] }),
    [id],
  );

  const write = can(user, "requests.write");
  const costs = can(user, "costs.read");
  const writeReason = write ? undefined : whyNot("requests.write");

  const d = q.data;
  const c = d?.candidate;
  const matches = useMemo(() => d?.checks || [], [d?.checks]);
  const wanted = params.get("request");
  const selected = useMemo(() => matches.find((m) => m.import_request_id === wanted) || matches[0] || null, [matches, wanted]);

  const approvalsByBid = useMemo(() => {
    const m = new Map<string, ApprovalRef>();
    for (const a of d?.bid_approvals || []) if (!m.has(a.entity_id)) m.set(a.entity_id, a);
    return m;
  }, [d?.bid_approvals]);

  const reload = () => { q.reload(); setPrepare(false); setResult(null); setReject(null); setMarkSent(null); };

  if (q.loading) return <PageLoading />;
  if (q.error) return <div className="page"><GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel></div>;
  if (!d || !c) {
    return (
      <div className="page">
        <PageHeader title="Candidate not found" crumbs={[{ label: "Import requests", to: "/requests" }, { label: id }]} />
        <GlassPanel clip><EmptyState title="Nothing recorded for this candidate" body="It may not have been ingested, or it may sit outside what you can see." action={<Button size="sm" variant="soft" to="/requests">Back to import requests</Button>} /></GlassPanel>
      </div>
    );
  }

  const tone = deadlineTone(c.deadline_at, c.deadline_passed);
  const current = d.current_translation;
  const bidsForRequest = (d.bids || []).filter((b) => !selected || b.import_request_id === selected.import_request_id);

  const menu: MenuItem[] = [
    { label: "Open the auction listing", onSelect: () => { if (c.source_url) window.open(c.source_url, "_blank", "noopener"); }, disabled: !c.source_url, disabledReason: "No listing link recorded." },
    { label: "Request a translation", meta: current ? TRANSLATION_LABEL[current.status] || current.status : "none yet", onSelect: () => void doTranslate(), disabled: !write || current?.status === "complete", disabledReason: !write ? writeReason : "The translation is already complete.", sepBefore: true },
  ];
  if (c.vehicle_id) menu.push({ label: "Open the purchased vehicle", to: `/vehicles/${encodeURIComponent(c.vehicle_id)}`, sepBefore: true });

  async function doTranslate() {
    await run(`translate:${id}`, requestTranslation(id), {}, { success: "Translation requested" });
    q.reload();
  }
  async function doDraft(matchId: string) {
    await run(`draft:${matchId}`, matchAction(matchId, "prepare-buyer-message"), {}, { success: "Buyer message drafted" });
    q.reload();
  }
  async function doSubmit(bid: Bid) {
    const res = await run(`bid:${bid.id}`, bidAction(bid.id, "submit-for-approval"), {}, { success: "Sent for the owner's decision" });
    if (res) q.reload();
  }

  return (
    <div className="page page-wide">
      <PageHeader
        title={candidateTitle(c)}
        crumbs={[{ label: "Import requests", to: "/requests" }, { label: candidateTitle(c) }]}
        subtitle={
          <span className="row-wrap" style={{ gap: 6 }}>
            <span>{auctionIdentity(c)}</span>
            {c.frame_raw ? <span>· frame {c.frame_raw}</span> : null}
            {c.last_seen_at ? <span>· last seen {relativeTime(c.last_seen_at)}</span> : null}
          </span>
        }
        actions={<Menu label="Candidate actions" align="right" items={menu} trigger={<Button variant="glass">Actions</Button>} />}
      >
        <div className="irq-head__tags">
          <Chip tone="soft">{c.status.replace(/_/g, " ")}</Chip>
          {tone ? <HealthLabel health={tone === "ok" ? "ok" : tone} label={tone === "blocked" ? "Deadline passed" : tone === "risk" ? "Deadline under 24 hours" : "Deadline open"} /> : null}
          <Chip size="sm" tone="soft">{matches.length} request{matches.length === 1 ? "" : "s"} matched</Chip>
          {current ? <Chip size="sm" tone={current.status === "complete" ? "ok" : "wait"}>Translation: {TRANSLATION_LABEL[current.status] || current.status}</Chip> : <Chip size="sm" tone="soft">Translation: not requested</Chip>}
        </div>
      </PageHeader>

      {c.deadline_passed ? <Notice tone="blocked" lead="The bid deadline has passed">No bid can be approved or submitted against this lot any more.</Notice> : null}
      {!costs ? <Notice tone="neutral" lead="Money hidden">Bid maximums and exchange estimates are hidden for your role.</Notice> : null}

      {/* identity + photos */}
      <GlassPanel className="irq-sec" radius="lg">
        <div className="irq-sec__title"><span>Auction identity</span><span className="count">snapshot v{c.snapshot_version ?? "—"}</span></div>
        <div className="cd2-images">
          {(c.images || []).length ? (
            (c.images || []).slice(0, 12).map((src, i) => (
              <img key={i} src={src} alt={`${candidateTitle(c)} photo ${i + 1}`} loading="lazy" width={168} height={118} />
            ))
          ) : (
            <div className="cd2-nophoto">No photo</div>
          )}
        </div>
        <dl className="irq-facts">
          <dt>Identity</dt><dd className="tnum">{c.identity}</dd>
          <dt>Listing</dt>
          <dd>{c.source_url ? <a href={c.source_url} target="_blank" rel="noreferrer noopener" className="wrap">{c.source_url}</a> : <span className="not-recorded">Not recorded</span>}</dd>
          <dt>Auction time</dt><dd><DualTime value={c.auction_at} showSource /></dd>
          <dt>Bid deadline</dt><dd><DualTime value={c.deadline_at} showSource /></dd>
          <dt>Discovered</dt><dd>{c.discovered_at ? <When iso={c.discovered_at} tz={TZ.phoenix} format="long" /> : <span className="not-recorded">Not recorded</span>}{c.ingest_count ? <span className="t4"> · seen {c.ingest_count} time{c.ingest_count === 1 ? "" : "s"}</span> : null}</dd>
        </dl>
        <Expander title="Snapshot and technical details">
          <KeyValues items={[["Snapshot hash", <span key="h" className="cd2-hash">{c.snapshot_hash || "not recorded"}</span>]]} />
          <JsonDetail value={c.snapshot} emptyText="No snapshot recorded" />
        </Expander>
      </GlassPanel>

      {/* specs */}
      <GlassPanel clip radius="lg">
        <div className="irq-sec" style={{ paddingBottom: 0 }}>
          <div className="irq-sec__title"><span>Specifications</span><span className="count">{Object.keys(c.specs || {}).length}</span></div>
          <span className="fs12 t4">Only what the source recorded. A field that is missing stays missing — it is never guessed from the description.</span>
        </div>
        {Object.keys(c.specs || {}).length === 0 ? (
          <EmptyState title="No structured specifications" body="Requirements that need one of these stay Unknown until a translation or the source provides it." />
        ) : (
          <Table minWidth={520}>
            <thead><tr><th>Field</th><th>Value</th><th>Where it came from</th></tr></thead>
            <tbody>
              {Object.entries(c.specs || {}).map(([k, v]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td>{v === null || v === undefined || v === "" ? <span className="not-recorded">Not recorded</span> : typeof v === "boolean" ? (v ? "Yes" : "No") : String(v)}</td>
                  <td className="t3">{c.spec_sources?.[k] || "snapshot"}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </GlassPanel>

      {/* per-request checks */}
      <GlassPanel className="irq-sec" radius="lg">
        <div className="irq-sec__title"><span>Against one buyer's requirements</span><span className="count">{matches.length}</span></div>
        {matches.length === 0 ? (
          <EmptyState align="left" title="Not matched to any request you can see" body="Matching is private per request: this candidate may still be matched to requests outside your access." />
        ) : (
          <>
            {matches.length > 1 ? (
              <>
                <Tabs
                  label="Import request"
                  idPrefix="cand-req"
                  value={selected?.import_request_id || ""}
                  onChange={(v) => { const p = new URLSearchParams(params); p.set("request", v); setParams(p, { replace: true }); }}
                  tabs={matches.map((m) => ({ id: m.import_request_id, label: m.request?.title || `Request ${m.import_request_id.slice(0, 6)}` }))}
                />
                <span className="fs12 t4">One request at a time. Buyers are never shown side by side, and nothing from another request appears here.</span>
              </>
            ) : null}
            {selected ? (
              <RequestPanel
                m={selected}
                bids={bidsForRequest}
                approvals={approvalsByBid}
                costs={costs}
                write={write}
                writeReason={writeReason}
                busy={busy}
                deadlinePassed={!!c.deadline_passed}
                onPrepare={() => setPrepare(true)}
                onSubmit={doSubmit}
                onResult={(b) => setResult(b)}
                onDraft={() => void doDraft(selected.id)}
                onMarkSent={() => setMarkSent(selected.id)}
                onReject={() => setReject(selected)}
              />
            ) : null}
          </>
        )}
      </GlassPanel>

      {/* translations */}
      <GlassPanel className="irq-sec" radius="lg">
        <div className="irq-sec__title"><span>Translation versions</span><span className="count">{(d.translations || []).length}</span></div>
        {(d.translations || []).length === 0 ? (
          <EmptyState
            align="left"
            title="No translation requested"
            body="The auction sheet is translated by the exporter. Requesting it is an external action and needs approval."
            action={<Button size="sm" variant="soft" onClick={() => void doTranslate()} loading={busy(`translate:${id}`)} disabled={!write} disabledReason={writeReason}>Request translation</Button>}
          />
        ) : (
          (d.translations || []).slice().reverse().map((t) => <TranslationCard key={t.id} t={t} onOpenApproval={openApproval} />)
        )}
      </GlassPanel>

      {/* dialogs */}
      <PrepareBidDialog
        open={prepare}
        onClose={() => setPrepare(false)}
        candidateId={id}
        match={selected}
        existing={bidsForRequest.find((b) => b.status === "draft" || b.status === "pending_approval") || null}
        onDone={reload}
      />
      <RecordResultDialog open={!!result} bid={result} onClose={() => setResult(null)} onDone={reload} />
      <RejectCandidateDialog open={!!reject} matchId={reject?.id || null} candidateTitle={candidateTitle(c)} onClose={() => setReject(null)} onDone={reload} />
      <MarkSentDialog open={!!markSent} matchId={markSent} onClose={() => setMarkSent(null)} onDone={reload} />
    </div>
  );
}

/* ---------- one request's view of this candidate ---------- */
function RequestPanel({
  m, bids, approvals, costs, write, writeReason, busy, deadlinePassed,
  onPrepare, onSubmit, onResult, onDraft, onMarkSent, onReject,
}: {
  m: CandidateMatch;
  bids: Bid[];
  approvals: Map<string, ApprovalRef>;
  costs: boolean;
  write: boolean;
  writeReason?: string;
  busy: (k: string) => boolean;
  deadlinePassed: boolean;
  onPrepare: () => void;
  onSubmit: (b: Bid) => void;
  onResult: (b: Bid) => void;
  onDraft: () => void;
  onMarkSent: () => void;
  onReject: () => void;
}) {
  const notReady = bidReadyReason(m);
  const draft = m.buyer_draft || null;
  return (
    <div className="stack">
      <div className="row-wrap">
        <Link to={`/requests/${encodeURIComponent(m.import_request_id)}`}>{m.request?.title || "Open the request"}</Link>
        <Chip size="sm" tone="soft">requirements v{m.requirements_version}</Chip>
        <Chip size="sm" tone={m.decision === "Allowed" ? "ok" : m.decision === "Blocked" ? "blocked" : "risk"}>{m.decision}</Chip>
        {m.stale ? <Chip size="sm" tone="risk">{m.stale_reason || "Needs re-evaluation"}</Chip> : null}
        {m.evaluated_at ? <span className="fs12 t4">evaluated {relativeTime(m.evaluated_at)}</span> : null}
      </div>

      {/* checks */}
      <ul className="req__lines">
        {(m.checks || []).length === 0 ? <li className="fs13 t4">No outcomes recorded for this match.</li> : (m.checks || []).map((ch) => (
          <li key={ch.key} className="req__line">
            <span className="req__text">
              <Chip size="sm" tone={checkTone(ch.result)}>{CHECK_LABEL[ch.result]}</Chip>{" "}
              <span>{ch.text || ch.key}</span>{" "}
              <span className="t4">({ch.tier})</span>
            </span>
            <span className="req__meta">
              {ch.evidence?.field ? <span>field <code>{ch.evidence.field}</code></span> : <span style={{ color: "var(--risk)" }}>no checkable criterion</span>}
              {ch.evidence?.observed !== undefined && ch.evidence?.observed !== null ? <span>· observed {String(ch.evidence.observed)}</span> : null}
              {ch.evidence?.expected !== undefined && ch.evidence?.expected !== null && ch.evidence?.expected !== "" ? <span>· expected {Array.isArray(ch.evidence.expected) ? (ch.evidence.expected as unknown[]).join(", ") : String(ch.evidence.expected)}</span> : null}
              {ch.evidence?.reason ? <span>· {ch.evidence.reason}</span> : null}
              {ch.evidence?.source ? <span className="t4">· source {ch.evidence.source}</span> : null}
            </span>
          </li>
        ))}
      </ul>
      {notReady ? <Notice tone="risk" lead={m.mandatory_fail ? "Fails a must-have" : "Not bid ready"}>{notReady}. A preference score never overrides a must-have.</Notice> : null}

      {/* buyer draft */}
      <div className="stack-sm">
        <div className="irq-sec__title"><span>Buyer message for this request</span><span className="count">{draft ? `v${draft.version}` : "none"}</span></div>
        {draft ? (
          <>
            {draft.invalidated ? <Notice tone="risk" lead="Draft invalidated">{draft.invalidated_reason || "The facts behind it changed."} Prepare a new one before sending.</Notice> : null}
            <pre className="cd2-draft">{draft.body}</pre>
            <span className="fs12 t4">
              {draft.sent_at ? <>Sent <When iso={draft.sent_at} tz={TZ.phoenix} format="long" />{draft.message_ref ? ` · ${draft.message_ref}` : ""}</> : "Not sent"}
              {draft.sources?.length ? ` · built from ${draft.sources.join(", ")}` : ""}
            </span>
          </>
        ) : (
          <span className="fs13 t4">Nothing drafted yet. The draft only ever contains this request's facts.</span>
        )}
        <div className="irq-actions">
          <Button size="sm" variant="soft" onClick={onDraft} loading={busy(`draft:${m.id}`)}
            disabled={!write || m.mandatory_fail || m.stale || ["rejected", "passed", "lost"].includes(m.status)}
            disabledReason={writeReason || (m.mandatory_fail ? "It fails a must-have — nothing to offer this buyer." : m.stale ? "Re-evaluate the match first." : "This candidate is closed for this request.")}>
            {draft ? "Prepare a new draft" : "Prepare buyer message"}
          </Button>
          <Button size="sm" variant="ghost" onClick={onMarkSent} disabled={!write || !draft || !!draft.sent_at || !!draft.invalidated}
            disabledReason={writeReason || (!draft ? "Prepare the draft first." : draft.invalidated ? "The draft was invalidated." : "Already recorded as sent.")}>
            Record as sent
          </Button>
          <Button size="sm" variant="ghost" onClick={onReject} disabled={!write || ["passed", "rejected"].includes(m.status)}
            disabledReason={writeReason || "Already excluded for this request."}>
            Reject for this buyer…
          </Button>
        </div>
      </div>

      {/* bid packets */}
      <div className="stack-sm">
        <div className="irq-sec__title"><span>Bid packet</span><span className="count">{bids.length}</span></div>
        {bids.length === 0 ? (
          <span className="fs13 t4">No packet prepared for this request.</span>
        ) : (
          bids.map((b) => (
            <BidPacketPanel
              key={b.id}
              b={b}
              approval={approvals.get(b.id) || null}
              costs={costs}
              write={write}
              writeReason={writeReason}
              busy={busy}
              onSubmit={() => onSubmit(b)}
              onResult={() => onResult(b)}
            />
          ))
        )}
        <div className="irq-actions">
          <Button size="sm" variant="soft" onClick={onPrepare}
            disabled={!write || deadlinePassed || m.mandatory_fail}
            disabledReason={writeReason || (deadlinePassed ? "The deadline has passed." : "It fails a must-have for this request.")}>
            {bids.some((b) => b.status === "draft" || b.status === "pending_approval") ? "Rebuild the packet…" : "Prepare bid packet…"}
          </Button>
          <span className="fs12 t4">Buyer interest is not bid authorization. Every bid needs the owner's approval, and any change to the packet invalidates it.</span>
        </div>
      </div>
    </div>
  );
}

/* ---------- exact bid packet ---------- */
function BidPacketPanel({ b, approval, costs, write, writeReason, busy, onSubmit, onResult }: {
  b: Bid; approval: ApprovalRef | null; costs: boolean; write: boolean; writeReason?: string;
  busy: (k: string) => boolean; onSubmit: () => void; onResult: () => void;
}) {
  const p = b.packet || {};
  const fx = (p.fx_estimate || {}) as Record<string, string | undefined>;
  const fxOk = fx.status === "estimated";
  return (
    <div className={["cd2-packet", b.status === "pending_approval" ? "cd2-packet--pending" : ""].filter(Boolean).join(" ")}>
      <div className="between" style={{ flexWrap: "wrap" }}>
        <span className="row-wrap" style={{ gap: 8 }}>
          <Chip size="sm" tone={b.status === "won" ? "ok" : ["invalidated", "lost", "expired", "cancelled"].includes(b.status) ? "blocked" : b.status === "pending_approval" ? "wait" : "soft"}>
            {BID_LABEL[b.status] || b.status}
          </Chip>
          <span className="fs13">Max <Money amount={b.max_amount} currency={b.currency || "JPY"} hidden={!costs} /></span>
        </span>
        <span className="row-wrap" style={{ gap: 8 }}>
          {approval ? <Button size="xs" variant="soft" onClick={() => openApproval(approval.id)}>Review approval</Button> : null}
          <Button size="xs" variant="primary" onClick={onSubmit} loading={busy(`bid:${b.id}`)}
            disabled={!write || b.status !== "draft" || !!b.gate?.reasons?.length}
            disabledReason={writeReason || (b.gate?.reasons?.length ? b.gate.reasons.join(" · ") : b.status === "pending_approval" ? "Already with the owner." : `This packet is ${(BID_LABEL[b.status] || b.status).toLowerCase()}.`)}>
            Submit for approval
          </Button>
          <Button size="xs" variant="soft" onClick={onResult}
            disabled={!write || !["submitted", "approved"].includes(b.status)}
            disabledReason={writeReason || "A result is recorded once the bid has been submitted."}>
            Record result…
          </Button>
        </span>
      </div>

      <dl className="irq-facts">
        <dt>Auction</dt><dd>{b.auction_house || "Not recorded"}{b.lot_no ? ` · lot ${b.lot_no}` : ""}</dd>
        <dt>Deadline</dt><dd><DualTime value={b.deadline_at} showSource /></dd>
        <dt>Fee basis</dt><dd>{b.fee_basis || <span className="not-recorded">Not recorded</span>}</dd>
        <dt>Exchange estimate</dt>
        <dd>
          {costs ? (
            fxOk ? <>1 {b.currency || "JPY"} = {fx.rate} · {fx.source} · {fx.date}{fx.usd ? <> → <Money amount={fx.usd} currency="USD" /></> : null}</>
              : <span style={{ color: "var(--risk)" }}>{fx.note || "No exchange estimate recorded (rate, source and date are all required)"}</span>
          ) : <span className="money money--hidden">••••</span>}
        </dd>
        <dt>Disclosures</dt>
        <dd>{(p.disclosures || []).length ? <ul className="req__lines">{(p.disclosures || []).map((x, i) => <li key={i} className="fs13">{x}</li>)}</ul> : <span className="not-recorded">None recorded</span>}</dd>
        <dt>Translation</dt>
        <dd>{p.translation ? `${String((p.translation as Record<string, unknown>).status ?? "not recorded")}${(p.translation as Record<string, unknown>).revision_no ? ` · r${String((p.translation as Record<string, unknown>).revision_no)}` : ""}` : "Not recorded"}</dd>
      </dl>

      {b.invalidated_reason ? <Notice tone="risk" lead="Invalidated">{b.invalidated_reason}</Notice> : null}
      {b.gate?.reasons?.length ? <Notice tone="blocked" lead="Blocked">{b.gate.reasons.join(" · ")}</Notice> : null}
      {b.result && Object.keys(b.result).length ? <Notice tone={String(b.result.outcome || b.status) === "won" ? "ok" : "neutral"} lead="Result"><JsonDetail value={b.result} /></Notice> : null}

      <Expander title="Packet hash and full contents">
        <KeyValues items={[["Packet hash", <span key="h" className="cd2-hash">{b.packet_hash || "not recorded"}</span>], ["Requirements version", b.requirements_version ?? "not recorded"], ["Translation revision", b.translation_revision_no ?? "not recorded"]]} />
        <JsonDetail value={b.packet} emptyText="No packet recorded" />
      </Expander>
    </div>
  );
}

/* ---------- translation version card ---------- */
function TranslationCard({ t, onOpenApproval }: { t: Translation; onOpenApproval: (id: string) => void }) {
  const sections = (t.required_sections || []);
  const completeness = (t.completeness || {}) as Record<string, unknown>;
  return (
    <div className="req__line">
      <div className="between" style={{ flexWrap: "wrap" }}>
        <span className="row-wrap" style={{ gap: 8 }}>
          <Chip size="sm" tone={t.status === "complete" ? "ok" : t.status === "invalidated" ? "blocked" : "wait"}>{TRANSLATION_LABEL[t.status] || t.status}</Chip>
          {t.revision_no ? <span className="fs13">revision r{t.revision_no}</span> : null}
          {t.doc_revision ? <span className="fs12 t4">doc {t.doc_revision}</span> : null}
        </span>
        <span className="row-wrap" style={{ gap: 8 }}>
          {t.doc_ref ? <span className="fs12 t4 truncate" style={{ maxWidth: 240 }}>{t.doc_provider || "doc"}: {t.doc_ref}</span> : null}
          {t.status === "pending_approval" && t.approval_id ? <Button size="xs" variant="soft" onClick={() => onOpenApproval(t.approval_id as string)}>Review request</Button> : null}
          {t.manual_task_id ? <Button size="xs" variant="ghost" to={`/tasks/${encodeURIComponent(t.manual_task_id)}`}>Open the sending task</Button> : null}
        </span>
      </div>
      <span className="req__meta">
        {t.requested_at ? <span>requested <When iso={t.requested_at} tz={TZ.phoenix} format="datetime" /></span> : null}
        {t.request_action_state ? <span>· send {t.request_action_state}</span> : null}
        {t.completed_at ? <span>· completed <When iso={t.completed_at} tz={TZ.phoenix} format="datetime" /></span> : null}
        {t.last_checked_at ? <span>· last checked {relativeTime(t.last_checked_at)}</span> : null}
      </span>

      {sections.length ? (
        <span className="req__meta">
          {sections.map((s) => {
            const ok = completeness[s] === true || (completeness[s] as Record<string, unknown> | undefined)?.present === true;
            return <Chip key={s} size="sm" tone={ok ? "ok" : "risk"}>{s}: {ok ? "present" : "missing"}</Chip>;
          })}
        </span>
      ) : null}

      {(t.excerpts || []).length ? (
        <Expander title={`Original Japanese and translation (${t.excerpts.length} excerpt${t.excerpts.length === 1 ? "" : "s"})`}>
          {t.excerpts.map((e, i) => (
            <div key={i} className="cd2-excerpt">
              <span className="cd2-excerpt__ja">{e.ja || "—"}</span>
              <span className="cd2-excerpt__en">{e.en || "—"}</span>
              {e.section ? <span className="fs12 t4" style={{ gridColumn: "1 / -1" }}>{e.section}</span> : null}
            </div>
          ))}
        </Expander>
      ) : <span className="fs12 t4">No excerpts recorded yet.</span>}

      {Object.keys(t.findings || {}).length ? (
        <Expander title="Findings extracted from this revision"><JsonDetail value={t.findings} /></Expander>
      ) : null}
      {t.request_content ? <Expander title="Exact content of the translation request"><pre className="act-pre">{t.request_content}</pre></Expander> : null}
    </div>
  );
}

/* ---------- prepare a bid packet ---------- */
function PrepareBidDialog({ open, onClose, candidateId, match, existing, onDone }: {
  open: boolean; onClose: () => void; candidateId: string; match: CandidateMatch | null; existing: Bid | null; onDone: () => void;
}) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("JPY");
  const [feeBasis, setFeeBasis] = useState("");
  const [rate, setRate] = useState("");
  const [source, setSource] = useState("");
  const [date, setDate] = useState("");
  const [disclosures, setDisclosures] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!open) return;
    setAmount(existing?.max_amount || "");
    setCurrency(existing?.currency || "JPY");
    setFeeBasis(existing?.fee_basis || "");
    setRate(""); setSource(""); setDate("");
    setDisclosures(((existing?.packet?.disclosures) || []).join("\n"));
    setNote("");
  }, [open, existing]);

  if (!open || !match) return null;
  const valid = Number(amount) > 0;
  const fxPartial = [rate, source, date].filter((x) => x.trim()).length;
  const fxBad = fxPartial > 0 && fxPartial < 3;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!valid || fxBad) return;
    const res = await run("prepare-bid", prepareBid(candidateId), {
      import_request_id: match.import_request_id,
      max_amount: amount.trim(),
      currency,
      fee_basis: feeBasis.trim(),
      fx_estimate: fxPartial === 3 ? { rate: rate.trim(), source: source.trim(), date: date.trim() } : null,
      disclosures: disclosures.split("\n").map((x) => x.trim()).filter(Boolean),
      note: note.trim() || null,
      bid_id: existing?.id || null,
      expected_version: existing?.version ?? null,
    }, { success: "Bid packet prepared" });
    if (res?.status === "ok") onDone();
  };

  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="lg" align="top"
      title="Prepare the bid packet"
      description="This is exactly what the owner reviews. Re-preparing a packet that is already with the owner invalidates that approval."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("prepare-bid")} disabled={!valid || fxBad}
            disabledReason={!valid ? "Enter a positive maximum." : "An exchange estimate needs a rate, a source and a date — or leave all three empty."}>
            Prepare packet
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <div className="form-grid">
          <Field label="Maximum bid" required hint="The ceiling the owner approves. Nothing above it can be submitted.">
            <Input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" placeholder="1250000" />
          </Field>
          <Field label="Currency" required>
            <Select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Fee basis" hint="How fees on top of the hammer price are counted.">
          <Input value={feeBasis} onChange={(e) => setFeeBasis(e.target.value)} placeholder="Hammer + auction fee + transport to port" />
        </Field>
        <Expander title="Exchange estimate (optional — all three parts or none)" defaultOpen={false}>
          <div className="req-edit__check">
            <Field label="Rate"><Input value={rate} onChange={(e) => setRate(e.target.value)} inputMode="decimal" placeholder="0.0068" /></Field>
            <Field label="Source"><Input value={source} onChange={(e) => setSource(e.target.value)} placeholder="ECB" /></Field>
            <Field label="Date"><Input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></Field>
          </div>
          {fxBad ? <span className="field__error">Give the rate, the source and the date, or leave all three empty — an estimate without its source is not recorded.</span> : null}
        </Expander>
        <Field label="Disclosures" hint="One per line. These travel with the approval.">
          <Textarea value={disclosures} onChange={(e) => setDisclosures(e.target.value)} rows={3} placeholder={"Grade 3.5 sheet, underbody rust unknown\nTranslation revision r2"} />
        </Field>
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- record the auction result ---------- */
function RecordResultDialog({ open, bid, onClose, onDone }: { open: boolean; bid: Bid | null; onClose: () => void; onDone: () => void }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [outcome, setOutcome] = useState<"won" | "lost">("won");
  const [sourceRef, setSourceRef] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("JPY");
  const [at, setAt] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!open) return;
    setOutcome("won"); setSourceRef(""); setAmount(""); setCurrency(bid?.currency || "JPY"); setAt(""); setNote("");
  }, [open, bid]);

  if (!open || !bid) return null;
  const blocked = !sourceRef.trim();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const res = await run("bid-result", bidAction(bid.id, "record-result"), {
      result: outcome,
      evidence: {
        source_ref: sourceRef.trim(),
        amount: amount.trim() || null,
        currency: amount.trim() ? currency : null,
        at: at ? new Date(at).toISOString() : null,
        note: note.trim() || null,
      },
      expected_version: bid.version,
    }, { success: outcome === "won" ? "Win recorded" : "Loss recorded" });
    if (res?.status === "ok") onDone();
  };

  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="md"
      title="Record the auction result"
      description="A win links exactly one purchased vehicle and starts fulfilment. A loss keeps this request's exclusions."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("bid-result")} disabled={blocked} disabledReason="The result needs its evidence.">Record result</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Outcome" required>
          <Select value={outcome} onChange={(e) => setOutcome(e.target.value === "lost" ? "lost" : "won")}>
            <option value="won">Won</option>
            <option value="lost">Lost</option>
          </Select>
        </Field>
        <Field label="Evidence" required error={blocked ? "Required." : undefined} hint="Auction result notice, exporter message or invoice reference.">
          <Input value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} placeholder="msg:… or result:lot-4021" />
        </Field>
        <div className="form-grid">
          <Field label={outcome === "won" ? "Winning price" : "Winning price (if known)"} hint="Optional. Hidden from roles without cost access.">
            <Input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" placeholder="1180000" />
          </Field>
          <Field label="Currency">
            <Select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Result time"><Input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} /></Field>
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </form>
    </ResponsiveDialog>
  );
}
