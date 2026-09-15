/* Material checks (backend/app/services/reply_checks.py). The first unresolved blocking check is shown
   in full with what would fix it; everything that passed is one line; non-blocking warnings sit under
   a disclosure. Labels and remediations are the server's words, never reworded here. */
import { useState } from "react";
import { Chip, Notice } from "../../../ui";
import type { Check, PlanItem } from "../types";

export function ChecksPanel({ checks }: { checks: Check[] }) {
  const [openAll, setOpenAll] = useState(false);
  if (!checks?.length) {
    return <p className="fs13 t3" style={{ margin: 0 }}>No checks have run on this draft yet.</p>;
  }
  const failing = checks.filter((c) => c.blocking && !c.ok);
  const warnings = checks.filter((c) => !c.blocking && !c.ok);
  const passed = checks.filter((c) => c.ok);
  const first = failing[0];

  return (
    <div className="stack-sm ib-checks">
      {first ? (
        <Notice tone="blocked" role="alert" lead={first.label}>
          {first.remediation || "This has to be resolved before the reply can be reviewed."}
          {failing.length > 1 ? <div className="fs12 t3" style={{ marginTop: 4 }}>{failing.length - 1} more {failing.length - 1 === 1 ? "check is" : "checks are"} also unresolved.</div> : null}
        </Notice>
      ) : (
        <Notice tone="ok" role="status" lead="Every check passed">
          Recipient, facts, money, availability, consent and promises were all verified against the records.
        </Notice>
      )}

      <div className="ib-checks__summary fs13">
        <span className="t3">{passed.length} passed</span>
        {failing.length ? <><span className="t4"> · </span><span style={{ color: "var(--blocked)" }}>{failing.length} blocking</span></> : null}
        {warnings.length ? <><span className="t4"> · </span><span style={{ color: "var(--risk)" }}>{warnings.length} to look at</span></> : null}
        <button type="button" className="linklike fs13" onClick={() => setOpenAll((v) => !v)} aria-expanded={openAll}>
          {openAll ? "Hide the list" : "Show every check"}
        </button>
      </div>

      {failing.length > 1 && !openAll ? (
        <ul className="ib-checks__list">
          {failing.slice(1).map((c) => (
            <li key={c.key} className="ib-checks__item ib-checks__item--fail">
              <span className="ib-checks__mark" aria-hidden="true">!</span>
              <span><b style={{ fontWeight: 500 }}>{c.label}</b>{c.remediation ? <> — {c.remediation}</> : null}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {warnings.length && !openAll ? (
        <ul className="ib-checks__list">
          {warnings.map((c) => (
            <li key={c.key} className="ib-checks__item ib-checks__item--warn">
              <span className="ib-checks__mark" aria-hidden="true">•</span>
              <span><b style={{ fontWeight: 500 }}>{c.label}</b>{c.remediation ? <> — {c.remediation}</> : null}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {openAll ? (
        <ul className="ib-checks__list">
          {checks.map((c) => (
            <li key={c.key} className={["ib-checks__item", c.ok ? "ib-checks__item--ok" : c.blocking ? "ib-checks__item--fail" : "ib-checks__item--warn"].join(" ")}>
              <span className="ib-checks__mark" aria-hidden="true">{c.ok ? "✓" : c.blocking ? "!" : "•"}</span>
              <span>
                <b style={{ fontWeight: 500 }}>{c.label}</b>
                {!c.ok && c.remediation ? <> — {c.remediation}</> : null}
                {!c.ok && !c.blocking ? <span className="t4"> (does not block sending)</span> : null}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** What the customer actually asked, and whether the draft answers it (draft.answer_plan). */
export function AnswerPlan({ plan }: { plan: PlanItem[] }) {
  if (!plan?.length) {
    return <p className="fs13 t3" style={{ margin: 0 }}>AZKT found no direct questions in the last message.</p>;
  }
  const open = plan.filter((p) => !p.answered).length;
  return (
    <div className="stack-sm">
      <div className="fs13 t3">
        {plan.length === 1 ? "1 thing was asked" : `${plan.length} things were asked`}
        {open ? ` · ${open} still unanswered in this draft` : " · all answered in this draft"}
      </div>
      <ul className="ib-plan">
        {plan.map((p) => (
          <li key={p.index} className={p.answered ? "ib-plan__item ib-plan__item--done" : "ib-plan__item"}>
            <span className="ib-plan__mark" aria-hidden="true">{p.answered ? "✓" : "?"}</span>
            <span className="ib-plan__q">
              {p.question}
              <span className="ib-plan__meta">
                {p.deadline ? <Chip size="sm" tone="risk">by {p.deadline}</Chip> : null}
                {p.needs_attachment ? <Chip size="sm" tone="soft">wants an attachment</Chip> : null}
                {!p.answered && p.facts_needed?.length ? <span className="fs12 t4">needs {p.facts_needed.join(", ").replace(/_/g, " ")}</span> : null}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
