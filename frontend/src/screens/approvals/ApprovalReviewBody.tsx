/* The exact approval: header state line, recommendation, key facts, the actual payload text,
   checks (failing first), "Sources and technical details", truthful execution state, and the decisions.
   Shared by the full page (/approvals/:id) and the desktop dialog opened from lists. */
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { entityHref, entityLabel, humanize, shortId } from "../../lib/links";
import { Button, Expander, Field, HealthLabel, Input, KeyValues, Money, Notice, NotRecorded, Textarea, When } from "../../ui";
import { JsonDetail, isEmptyValue, scalarText } from "../shared/JsonDetail";
import { actorName, approveVerb, kindLabel, statusView, type ApprovalDetail } from "./types";
import type { ApprovalDetailState } from "./useApprovalDetail";

export function ApprovalStateLine({ d }: { d: ApprovalDetail }) {
  const sv = statusView(d.status);
  return (
    <div className="apv-state">
      {sv.health ? <HealthLabel health={sv.health} label={sv.label} dot /> : <span className="apv-state__label t3">{sv.label}</span>}
      <span className="t3 tnum">Version {d.version}{d.expires_at ? <> · {d.status === "pending" ? "expires" : "expired"} <When iso={d.expires_at} format="long" /></> : null}</span>
    </div>
  );
}

function listText(v: unknown): string {
  if (Array.isArray(v)) return v.map((x) => (typeof x === "object" && x ? (x as { label?: string; name?: string; email?: string }).label || (x as { name?: string }).name || (x as { email?: string }).email || JSON.stringify(x) : String(x))).join(", ");
  if (v && typeof v === "object") return Object.entries(v as Record<string, unknown>).map(([k, x]) => `${humanize(k)}: ${scalarText(x)}`).join(" · ");
  return scalarText(v);
}

function factsOf(d: ApprovalDetail): Array<[string, ReactNode]> {
  const p = d.payload || {}, t = d.targets || {}, c = d.consequence || {};
  const out: Array<[string, ReactNode]> = [];
  const to = t.recipients ?? p.to ?? p.recipients ?? t.to;
  if (!isEmptyValue(to)) out.push(["To", listText(to)]);
  if (!isEmptyValue(p.cc)) out.push(["Cc", listText(p.cc)]);
  if (typeof p.subject === "string" && p.subject) out.push(["Subject", p.subject]);
  const att = p.attachments ?? p.attachment;
  if (!isEmptyValue(att)) out.push(["Attachment", listText(att)]);
  if (!isEmptyValue(t.channel)) out.push(["Channel", humanize(String(t.channel))]);
  if (!isEmptyValue(t.account)) out.push(["Account", String(t.account)]);
  if (c.amount !== undefined && c.amount !== null && c.amount !== "") out.push(["Amount", <Money amount={c.amount as number | string} currency={typeof c.currency === "string" ? c.currency : "USD"} />]);
  const scopeBits = [typeof c.scope === "string" ? humanize(c.scope) : null, c.moves_money === true ? "funds move" : c.moves_money === false ? "no funds move" : null, c.reversible === true ? "reversible" : c.reversible === false ? "not reversible" : null].filter(Boolean);
  if (scopeBits.length) out.push(["Scope", scopeBits.join(" · ")]);
  for (const [k, v] of Object.entries(t)) {
    if (["recipients", "to", "channel", "account"].includes(k) || isEmptyValue(v)) continue;
    out.push([humanize(k), listText(v)]);
  }
  if (d.entity_kind) {
    const href = entityHref(d.entity_kind, d.entity_id);
    out.push(["Record", href ? <Link to={href}>{entityLabel(d.entity_kind)} {shortId(d.entity_id)}</Link> : <span>{entityLabel(d.entity_kind)} {shortId(d.entity_id)}</span>]);
  }
  if (!isEmptyValue(d.conditions)) out.push(["Conditions", listText(d.conditions)]);
  out.push(["Requested by", actorName(d.requested_by)]);
  return out;
}

const SHOWN_AS_FACTS = new Set(["subject", "to", "cc", "recipients", "attachments", "attachment"]);
function PayloadView({ d, bodyField }: { d: ApprovalDetail; bodyField: string | null }) {
  const p = d.payload || {};
  // Everything not already rendered as the message body or in the facts grid.
  const rest = Object.fromEntries(Object.entries(p).filter(([k]) => k !== bodyField && !SHOWN_AS_FACTS.has(k)));
  return (
    <div className="stack-sm">
      {bodyField ? (
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>Exactly what will go out</div>
          <div className="apv-body">{String(p[bodyField])}</div>
        </div>
      ) : null}
      {Object.keys(rest).length ? (
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>{bodyField ? "Other fields" : "Exact details"}</div>
          <JsonDetail value={rest} />
        </div>
      ) : !bodyField ? <NotRecorded text="Payload has no fields to show." /> : null}
    </div>
  );
}

function Checks({ checks }: { checks: ApprovalDetail["checks"] }) {
  const [open, setOpen] = useState(false);
  const list = Array.isArray(checks) ? checks : [];
  const failing = list.filter((c) => c && c.ok === false);
  const passed = list.filter((c) => c && c.ok !== false);
  if (!list.length) return <div className="fs13 t3">No checks recorded for this approval.</div>;
  return (
    <div className="apv-checks">
      {failing.map((c, i) => (
        <div key={`f${i}`} className="apv-check apv-check--fail"><span className="apv-check__mark" aria-hidden="true">✕</span><span><b style={{ fontWeight: 500 }}>{humanize(c.key)}</b> · {c.label || "Check failed"}</span></div>
      ))}
      {passed.length ? (
        <button type="button" className="expander__btn" aria-expanded={open} onClick={() => setOpen((o) => !o)} style={{ color: "var(--ok)" }}>
          <span className="apv-check__mark" aria-hidden="true">✓</span>
          <span>{failing.length ? `${passed.length} other ${passed.length === 1 ? "check" : "checks"} passed` : `Checks passed · ${passed.length}`}</span>
          <span className="expander__caret" aria-hidden="true">▶</span>
        </button>
      ) : null}
      {open ? passed.map((c, i) => (
        <div key={`p${i}`} className="apv-check apv-check--ok" style={{ paddingLeft: 4 }}><span className="apv-check__mark" aria-hidden="true">✓</span><span className="t2"><b style={{ fontWeight: 500 }}>{humanize(c.key)}</b>{c.label ? ` · ${c.label}` : ""}</span></div>
      )) : null}
    </div>
  );
}

function StateNotice({ d }: { d: ApprovalDetail }) {
  const xa = d.external_action;
  const provider = xa?.provider ? humanize(xa.provider) : "the provider";
  switch (d.status) {
    case "pending": return null;
    case "approved":
    case "queued":
      return <Notice tone="wait" lead="Approved · awaiting execution">{provider} has not confirmed yet{xa?.attempts ? ` · attempt ${xa.attempts}` : ""}. This page refreshes on its own.</Notice>;
    case "executing":
      return <Notice tone="wait" lead="Executing">Waiting for {provider}'s receipt.</Notice>;
    case "confirmed":
      return <Notice tone="ok" lead="Confirmed" action={<Link to={`/activity?q=${encodeURIComponent(d.title || "")}`} className="fs13">Activity</Link>}>{xa?.provider_ref ? `${provider} receipt ${xa.provider_ref}` : "Receipt recorded"}{xa?.executed_at ? <> · <When iso={xa.executed_at} format="long" /></> : d.decided_at ? <> · <When iso={d.decided_at} format="long" /></> : null}</Notice>;
    case "failed":
      return <Notice tone="blocked" lead="Failed" role="alert">{xa?.error || (d.result && typeof d.result.error === "string" ? d.result.error : "The provider rejected this action. Nothing was retried.")}</Notice>;
    case "result_unknown":
      return <Notice tone="risk" lead="Result unknown" role="alert">{provider} may have accepted this. AZKT does not retry blindly; it reconciles by {xa?.provider_ref ? `reference ${xa.provider_ref}` : "provider reference"} first.</Notice>;
    case "declined":
      return <Notice lead="Declined">{d.decision_note ? `"${d.decision_note}"` : "Nothing was sent."}{d.decided_at ? <> · <When iso={d.decided_at} format="long" /></> : null}</Notice>;
    case "expired":
      return <Notice tone="risk" lead="Expired">This version can no longer be approved{d.expires_at ? <> · was valid until <When iso={d.expires_at} format="long" /></> : null}.</Notice>;
    case "invalidated": {
      const reason = (d.invalidated_reason || "").replace(/^details changed\s*[—-]\s*review again\s*/i, "").replace(/^[(\s]+|[)\s]+$/g, "");
      return <Notice tone="risk" lead="Details changed — review again" action={d.superseded_by_id ? <Link to={`/approvals/${d.superseded_by_id}`} className="fs13">Open the current version</Link> : undefined}>{reason ? reason.charAt(0).toUpperCase() + reason.slice(1) : "A bound fact, target or policy changed. This version cannot run."}</Notice>;
    }
    case "canceled":
      return <Notice lead="Canceled">Remaining work stopped; completed effects are not undone.</Notice>;
    default:
      return <Notice lead={humanize(d.status)} />;
  }
}

export function ApprovalReviewBody({ s }: { s: ApprovalDetailState }) {
  const { user } = useAuth();
  const d = s.data;
  if (!d) return null;
  const xa = d.external_action;
  return (
    <div className="stack">
      {s.conflict ? <Notice tone="risk" lead="Details changed — review again" role="alert">The approval changed since you loaded it. This is the current version.</Notice> : null}
      {d.recommendation || d.description ? (
        <div className="apv-rec"><b style={{ fontWeight: 500, color: "var(--text)" }}>Recommendation.</b> {d.recommendation || d.description}</div>
      ) : null}
      <dl className="apv-facts">
        {factsOf(d).map(([k, v], i) => (<div key={i} style={{ display: "contents" }}><dt>{k}</dt><dd>{v}</dd></div>))}
      </dl>

      {s.editing ? (
        <div className="apv-edit">
          <div className="fs13 t3">Editing creates version {d.version + 1}. Version {d.version} is invalidated and you review the new one before anything runs.</div>
          {s.hasSubject ? <Field label="Subject"><Input value={s.editSubject} onChange={(e) => s.setEditSubject(e.target.value)} /></Field> : null}
          {s.bodyField ? <Field label={humanize(s.bodyField)}><Textarea value={s.editBody} onChange={(e) => s.setEditBody(e.target.value)} rows={7} /></Field> : null}
          <div className="row-wrap">
            <Button variant="primary" size="md" loading={s.busy("edit")} onClick={s.saveEdit} disabled={!s.editBody.trim() && !s.editSubject.trim()} disabledReason="Write something first.">Save as version {d.version + 1}</Button>
            <Button variant="ghost" size="md" onClick={s.cancelEdit}>Cancel</Button>
          </div>
        </div>
      ) : (
        <PayloadView d={d} bodyField={s.bodyField} />
      )}

      <Checks checks={d.checks} />
      <StateNotice d={d} />

      <Expander>
        <KeyValues items={[
          ["Sources", isEmptyValue(d.sources) ? <NotRecorded text="No sources attached" /> : <JsonDetail value={d.sources} />],
          ["Payload", <span className="tnum">{d.id} v{d.version} · hash {shortId(d.payload_hash, 12)}</span>],
          ["Policy", d.policy_version ? `policy ${d.policy_version}` : <NotRecorded />],
          ["Command", <span className="tnum">{d.command_name}{d.action_class ? ` · ${humanize(d.action_class)}` : ""}</span>],
          ["Run", d.run_id || d.mission_id ? <span className="tnum">{[d.run_id ? `run ${d.run_id}` : null, d.mission_id ? `mission ${d.mission_id}` : null].filter(Boolean).join(" · ")}</span> : <NotRecorded />],
          ["Authority", d.authorized_by ? <span>Decided by {d.authorized_by === user?.id ? "you" : `person ${shortId(d.authorized_by)}`}{d.decided_at ? <> · <When iso={d.decided_at} format="long" /></> : null}</span> : "Owner approval required. No standing permission covers this action."],
          ["External action", xa ? <span className="tnum">{humanize(xa.state)} · {xa.provider || "provider not recorded"}{xa.provider_ref ? ` · ref ${xa.provider_ref}` : ""} · {xa.attempts} {xa.attempts === 1 ? "attempt" : "attempts"}</span> : d.executor ? `Executor: ${d.executor}` : "Internal change — no external side effect"],
          ["Receipt", isEmptyValue(d.receipt) && isEmptyValue(xa?.receipt) ? <NotRecorded text="No receipt yet" /> : <JsonDetail value={isEmptyValue(d.receipt) ? xa?.receipt : d.receipt} />],
          ["Record versions", isEmptyValue(d.record_versions) ? <NotRecorded /> : <JsonDetail value={d.record_versions} />],
          ["Previous version", d.supersedes_id ? <Link to={`/approvals/${d.supersedes_id}`}>v{d.previous_version?.version ?? d.version - 1} · {shortId(d.supersedes_id)}</Link> : <NotRecorded text="First version" />],
          ["Created", d.created_at ? <When iso={d.created_at} format="long" /> : <NotRecorded />],
        ]} />
      </Expander>
    </div>
  );
}

export function ApprovalReviewActions({ s, onClose }: { s: ApprovalDetailState; onClose?: () => void }) {
  const { user } = useAuth();
  const d = s.data;
  const [declining, setDeclining] = useState(false);
  if (!d) return null;
  const allowed = can(user, "approve");
  const pending = d.status === "pending";
  if (!pending || s.editing) {
    return (
      <div className="apv-actions">
        {onClose ? <Button variant="glass" onClick={onClose}>Close</Button> : <Button variant="glass" to="/approvals">All approvals</Button>}
        {d.status === "invalidated" && d.superseded_by_id ? <Button variant="primary" to={`/approvals/${d.superseded_by_id}`}>Review version {d.version + 1}</Button> : null}
      </div>
    );
  }
  if (declining) {
    return (
      <div className="stack-sm" style={{ width: "100%" }}>
        <Field label="Why decline? (optional, recorded)"><Input value={s.note} onChange={(e) => s.setNote(e.target.value)} placeholder="e.g. wrong recipient" /></Field>
        <div className="apv-actions">
          <Button variant="danger" loading={s.busy("decline")} onClick={() => { void s.decline().then(() => setDeclining(false)); }}>Decline</Button>
          <Button variant="ghost" onClick={() => setDeclining(false)}>Keep reviewing</Button>
        </div>
      </div>
    );
  }
  return (
    <div className="apv-actions">
      <Button variant="primary" loading={s.busy("approve")} disabled={!allowed} disabledReason={whyNot("approve")} onClick={() => { void s.approve(); }}>{approveVerb(d.kind)}</Button>
      <Button variant="glass" disabled={!allowed || !s.canEdit} disabledReason={!allowed ? whyNot("approve") : "This approval has no editable text; decline and ask for a new draft."} onClick={s.startEdit}>Edit</Button>
      <Button variant="ghost" disabled={!allowed} disabledReason={whyNot("approve")} onClick={() => setDeclining(true)}>Decline</Button>
      <span className="apv-actions__note">{kindLabel(d.kind)} · owner approval, exact version {d.version}</span>
    </div>
  );
}
