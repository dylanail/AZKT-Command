/* Settings › Procedures / Teach (spec §9.4, G14).
   GET  /api/procedures?status= · /api/procedures/{id} (versions with their spec and test results)
   POST /api/teach                                   — text, correction, demonstration or a knowledge item
   POST /api/procedures/{id}/versions/{vid}/tests    — deterministic replay of the stored test cases
   POST /api/procedures/{id}/versions/{vid}/promote  — owner, one step up the ladder, blocked while tests fail
   POST /api/procedures/{id}/versions/{vid}/rollback — owner, with a reason; history is kept
   Promotion never creates a permission, and a demonstration is interpreted with uncertainty. */
import { useRef, useState, type FormEvent } from "react";
import "../../../styles/learning.css";
import { api } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useQuery } from "../../../lib/useQuery";
import { useCommand } from "../../../lib/useCommand";
import { useIsMobile } from "../../../lib/viewport";
import { can } from "../../../lib/perms";
import { humanize } from "../../../lib/links";
import {
  Button, Chip, EmptyState, ErrorState, Expander, Field, GlassPanel, Input, KeyValues, Loading, Notice, ResponsiveDialog,
  SegmentedControl, Select, Textarea, When,
} from "../../../ui";

const LADDER = ["proposed", "offline_tested", "shadow", "supervised", "bounded_automatic"] as const;
const LADDER_LABELS: Record<string, string> = {
  proposed: "Proposed",
  offline_tested: "Offline tested",
  shadow: "Shadow",
  supervised: "Supervised",
  bounded_automatic: "Bounded automatic",
  withdrawn: "Withdrawn",
};
const LADDER_MEANING: Record<string, string> = {
  proposed: "Written down. Nothing runs on it.",
  offline_tested: "Its own test cases replay cleanly.",
  shadow: "Runs alongside you, producing nothing you send.",
  supervised: "Produces work you review before it goes out.",
  bounded_automatic: "Runs inside an existing standing permission, still revocable.",
};

const TEACH_KINDS = [
  { value: "procedure", label: "Steps I follow", hint: "Write the steps. AZKT turns them into a procedure you review." },
  { value: "correction", label: "A correction", hint: "What it got wrong and what it should have done." },
  { value: "demonstration", label: "A demonstration", hint: "A transcript or narration of you doing it. Interpreted with uncertainty." },
  { value: "email_example", label: "An email example", hint: "A reply worth imitating for tone and reasoning." },
  { value: "policy", label: "A business policy", hint: "General policy. Needs your explicit acceptance before it counts." },
  { value: "style", label: "A style preference", hint: "How things should sound. Used as an example, never as policy." },
  { value: "exception", label: "A buyer exception", hint: "One customer only. Stays scoped to them." },
] as const;

interface TestCase { name: string; ok: boolean; outcome: string; reasons: string[]; tripped: string[] }
interface TestResults { passed: number; failed: number; total: number; ok: boolean; cases: TestCase[]; at: string; reason?: string; spec_hash?: string }

interface ProcedureVersion {
  id: string; version: number; procedure_id: string; version_no: number; stage: string;
  spec: Record<string, unknown>; spec_hash: string | null; source_kind: string | null; source_ref: string | null;
  interpretation: string | null; test_results: TestResults | Record<string, never>;
  tests_passed_at: string | null; promoted_at: string | null; promoted_by: string | null;
  withdrawn_at: string | null; withdrawn_by: string | null; withdrawn_reason: string | null;
  superseded_at: string | null; permission_id: string | null;
  stage_history: Array<Record<string, unknown>>; proposed_by: string | null; created_at: string | null;
}
interface Procedure {
  id: string; version: number; key: string; title: string; goal: string; status: string;
  current_version_id: string | null; workflow_key: string | null; description: string;
  stage_history: Array<Record<string, unknown>>; last_promoted_at: string | null; withdrawn_at: string | null;
  created_at: string | null; updated_at: string | null; ladder: string[];
  versions?: ProcedureVersion[]; current_version?: ProcedureVersion | null;
}
interface ProceduresResp { items: Procedure[]; total: number }

function Ladder({ stage }: { stage: string }) {
  if (stage === "withdrawn") {
    return <span className="ladder"><span className="ladder__step ladder__step--withdrawn">Withdrawn</span></span>;
  }
  const idx = LADDER.indexOf(stage as (typeof LADDER)[number]);
  return (
    <span className="ladder">
      {LADDER.map((s, i) => (
        <span key={s} style={{ display: "contents" }}>
          {i ? <span className="ladder__arrow" aria-hidden="true">›</span> : null}
          <span
            className={["ladder__step", i < idx ? "ladder__step--done" : "", i === idx ? "ladder__step--current" : ""].filter(Boolean).join(" ")}
            title={LADDER_MEANING[s]}
          >
            {LADDER_LABELS[s]}
          </span>
        </span>
      ))}
    </span>
  );
}

function specList(spec: Record<string, unknown>, key: string): string[] {
  const v = spec[key];
  if (!Array.isArray(v)) return [];
  return v.map((x) => {
    if (typeof x === "string") return x;
    if (x && typeof x === "object") {
      const o = x as Record<string, unknown>;
      return String(o.text ?? o.step ?? JSON.stringify(o));
    }
    return String(x);
  });
}

function SpecBlock({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  return (
    <div className="spec-block">
      <span className="spec-block__title">{title}</span>
      {items.length ? <ul>{items.map((s, i) => <li key={i}>{s}</li>)}</ul> : <span className="spec-block__empty">{empty}</span>}
    </div>
  );
}

function testState(v: ProcedureVersion): { ok: boolean; why: string | null; results: TestResults | null } {
  const raw = v.test_results as Record<string, unknown> | null;
  if (!raw || Object.keys(raw).length === 0 || typeof raw.total !== "number") {
    return { ok: false, why: "Tests have not been run for this version.", results: null };
  }
  const tr = raw as unknown as TestResults;
  if (!tr.ok) return { ok: false, why: tr.reason || `${tr.failed} test case(s) failed.`, results: tr };
  if (v.spec_hash && tr.spec_hash && tr.spec_hash !== v.spec_hash) {
    return { ok: false, why: "The spec changed since the last passing run — test it again.", results: tr };
  }
  return { ok: true, why: null, results: tr };
}

function VersionCard({ p, v, canTeach, isOwner, onDone }: { p: Procedure; v: ProcedureVersion; canTeach: boolean; isOwner: boolean; onDone: () => void }) {
  const { run, busy } = useCommand();
  const [rollback, setRollback] = useState(false);
  const [reason, setReason] = useState("");
  const [permissionId, setPermissionId] = useState("");
  const isMobile = useIsMobile();

  const t = testState(v);
  const idx = LADDER.indexOf(v.stage as (typeof LADDER)[number]);
  const nextStage = idx >= 0 && idx < LADDER.length - 1 ? LADDER[idx + 1] : null;
  const uncertain = v.interpretation === "uncertain";

  const base = `/api/procedures/${encodeURIComponent(p.id)}/versions/${encodeURIComponent(v.id)}`;

  const promoteReason = !isOwner ? "Only the owner promotes a procedure."
    : v.withdrawn_at ? "This version is withdrawn — propose a new one."
    : !nextStage ? "Already at the top of the ladder."
    : !t.ok ? `Promotion blocked: ${t.why}`
    : nextStage === "bounded_automatic" && uncertain ? "A version derived from a demonstration cannot become automatic until it is taught explicitly."
    : nextStage === "bounded_automatic" && !permissionId.trim() ? "Bounded automatic needs an existing standing permission id — promotion never creates one."
    : undefined;

  const runTests = async () => {
    const r = await run(`tests:${v.id}`, `${base}/tests`, { expected_version: v.version },
      { success: "Tests replayed. The result is recorded on this version." });
    if (r?.status === "ok") onDone();
  };
  const promote = async () => {
    if (promoteReason) return;
    const r = await run(`promote:${v.id}`, `${base}/promote`, {
      expected_version: v.version, permission_id: nextStage === "bounded_automatic" ? permissionId.trim() : null,
    }, { success: `Promoted to ${LADDER_LABELS[nextStage || ""] || nextStage}. No permission was created.` });
    if (r?.status === "ok") onDone();
  };
  const doRollback = async (e: FormEvent) => {
    e.preventDefault();
    if (!reason.trim()) return;
    const r = await run(`rollback:${v.id}`, `${base}/rollback`, { expected_version: v.version, reason: reason.trim() },
      { success: "Version withdrawn. History is kept and queued work revalidates before it runs." });
    if (r?.status === "ok") { setRollback(false); setReason(""); onDone(); }
  };

  return (
    <div className="lrn-row">
      <div className="lrn-row__head">
        <div className="lrn-row__main">
          <div className="lrn-row__title">
            <span>Version {v.version_no}</span>
            {v.id === p.current_version_id ? <Chip size="sm" tone="act">Current</Chip> : null}
            {v.superseded_at ? <Chip size="sm" tone="soft">Superseded</Chip> : null}
            {v.withdrawn_at ? <Chip size="sm" tone="blocked">Withdrawn</Chip> : null}
            {v.source_kind ? <Chip size="sm" tone="soft">{humanize(v.source_kind)}</Chip> : null}
            {uncertain ? <Chip size="sm" tone="risk" title="Steps were inferred from a demonstration, not given as instructions.">Interpreted with uncertainty</Chip> : null}
          </div>
          <div className="lrn-row__meta">
            Proposed <When iso={v.created_at} format="date" />
            {v.promoted_at ? <> · promoted <When iso={v.promoted_at} format="date" /></> : null}
            {v.permission_id ? ` · permission ${v.permission_id.slice(0, 8)}` : ""}
            {v.withdrawn_reason ? ` · ${v.withdrawn_reason}` : ""}
          </div>
        </div>
        <Ladder stage={v.withdrawn_at ? "withdrawn" : v.stage} />
      </div>

      {uncertain ? (
        <Notice tone="risk" lead="Interpreted with uncertainty">
          {String((v.spec as { interpretation_note?: unknown }).interpretation_note
            || "Derived from a demonstration: the steps are inferred, not instructions. Nothing from it was installed as executable behaviour.")}
        </Notice>
      ) : null}

      <div className="row-wrap">
        <Chip size="sm" tone={t.ok ? "ok" : t.results ? "blocked" : "amber"}>
          {t.results ? `Tests ${t.results.passed}/${t.results.total}` : "Tests not run"}
        </Chip>
        {t.why ? <span className="fs12 t3">{t.why}</span> : null}
        {t.results?.at ? <span className="fs12 t4">last run <When iso={t.results.at} relative /></span> : null}
      </div>

      <Expander title="What this version says">
        <div className="stack">
          <SpecBlock title="Goal" items={[String((v.spec as { goal?: unknown }).goal || p.goal || "")].filter(Boolean)} empty="No goal recorded." />
          <SpecBlock title="Inputs it needs" items={specList(v.spec, "inputs")} empty="No inputs recorded." />
          <SpecBlock title="Hard constraints" items={specList(v.spec, "hard_constraints")} empty="No hard constraints — nothing stops it early." />
          <SpecBlock title="Normal path" items={specList(v.spec, "normal_path")} empty="No steps recorded." />
          <SpecBlock title="Permitted alternatives" items={specList(v.spec, "permitted_alternatives")} empty="None. Anything else escalates." />
          <SpecBlock title="Evidence of completion" items={specList(v.spec, "evidence_of_completion")} empty="No evidence of completion — it cannot prove it finished." />
          <SpecBlock title="Escalation" items={specList(v.spec, "escalation")} empty="No escalation conditions recorded." />
          {t.results?.cases?.length ? (
            <div className="spec-block">
              <span className="spec-block__title">Test cases · last run</span>
              <div className="apv-checks">
                {t.results.cases.map((c, i) => (
                  <div key={i} className={["apv-check", c.ok ? "apv-check--ok" : "apv-check--fail"].join(" ")}>
                    <span className="apv-check__mark" aria-hidden="true">{c.ok ? "✓" : "✕"}</span>
                    <span>
                      {c.name} <span className="t3">· {humanize(c.outcome)}</span>
                      {c.reasons.length ? <div className="fs12 t3">{c.reasons.join(" · ")}</div> : null}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
          <KeyValues items={[
            ["Source", v.source_kind ? humanize(v.source_kind) : "Not recorded"],
            ["Source reference", v.source_ref || "Not recorded"],
            ["Spec fingerprint", v.spec_hash ? v.spec_hash.slice(0, 16) : "Not recorded"],
            ["Executable", "No — a spec never becomes tool authority by itself"],
          ]} />
        </div>
      </Expander>

      <div className="lrn-row__actions">
        <Button size="sm" variant="soft" loading={busy(`tests:${v.id}`)}
          disabled={!canTeach || !!v.withdrawn_at}
          disabledReason={!canTeach ? "Teaching procedures isn't part of your role." : "This version is withdrawn."}
          onClick={runTests}>Run tests</Button>
        <Button size="sm" variant="primary" loading={busy(`promote:${v.id}`)} disabled={!!promoteReason} disabledReason={promoteReason} onClick={promote}>
          {nextStage ? `Promote to ${LADDER_LABELS[nextStage].toLowerCase()}` : "Promote one step"}
        </Button>
        <Button size="sm" variant="ghost" disabled={!isOwner || !!v.withdrawn_at}
          disabledReason={!isOwner ? "Only the owner rolls a version back." : "Already withdrawn."}
          onClick={() => setRollback(true)}>Roll back…</Button>
      </div>

      {nextStage === "bounded_automatic" && isOwner && !v.withdrawn_at && t.ok && !uncertain ? (
        <Field label="Standing permission id" hint="Bounded automatic runs inside a permission you already enabled. Promotion never creates one.">
          <Input value={permissionId} onChange={(e) => setPermissionId(e.target.value)} placeholder="permission id" />
        </Field>
      ) : null}

      <ResponsiveDialog mobile={isMobile} open={rollback} onClose={() => setRollback(false)} size="sm" title={`Withdraw version ${v.version_no}?`}
        footer={<><Button type="submit" form="rollback-form" variant="danger" loading={busy(`rollback:${v.id}`)} disabled={!reason.trim()} disabledReason="A reason is required.">Withdraw</Button><Button variant="ghost" onClick={() => setRollback(false)}>Cancel</Button></>}>
        <form id="rollback-form" className="stack" onSubmit={doRollback}>
          <div className="fs14 t2">
            History is kept. New runs stop on this version, the previous promoted version becomes current again, and anything
            queued against it is revalidated before it executes.
          </div>
          <Field label="Reason (recorded in Activity)" required>
            <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. told a buyer the wrong deposit rule" />
          </Field>
        </form>
      </ResponsiveDialog>
    </div>
  );
}

function ProcedureDrawer({ id, canTeach, isOwner, onClose, onChanged }: { id: string; canTeach: boolean; isOwner: boolean; onClose: () => void; onChanged: () => void }) {
  const isMobile = useIsMobile();
  const q = useQuery<Procedure>((signal) => api.get<Procedure>(`/api/procedures/${encodeURIComponent(id)}`, { signal }), [id]);
  const p = q.data;
  return (
    <ResponsiveDialog
      mobile={isMobile} open onClose={onClose} size="xl" align="top"
      title={p ? p.title : "Procedure"}
      eyebrow={p ? p.key : undefined}
      description={p?.goal || undefined}
      footer={<Button variant="ghost" onClick={onClose}>Close</Button>}
      label="Procedure detail"
    >
      {q.loading ? <Loading label="Loading procedure" rows={4} />
        : q.error ? <ErrorState error={q.error} onRetry={q.reload} />
        : !p ? null
        : (
          <div className="stack">
            <div className="row-wrap">
              <Ladder stage={p.status} />
              {p.workflow_key ? <Chip size="sm" tone="soft">workflow {p.workflow_key}</Chip> : null}
            </div>
            {p.description ? <div className="fs14 t2">{p.description}</div> : null}
            <GlassPanel clip>
              {(p.versions || []).length
                ? (p.versions || []).map((v) => (
                  <VersionCard key={v.id} p={p} v={v} canTeach={canTeach} isOwner={isOwner}
                    onDone={() => { q.reload(); onChanged(); }} />
                ))
                : <EmptyState title="No versions recorded" />}
            </GlassPanel>
          </div>
        )}
    </ResponsiveDialog>
  );
}

function TeachComposer({ canTeach, onDone }: { canTeach: boolean; onDone: () => void }) {
  const { run, busy } = useCommand();
  const [kind, setKind] = useState<string>("procedure");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [workflow, setWorkflow] = useState("");
  const [contactId, setContactId] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);

  const meta = TEACH_KINDS.find((k) => k.value === kind) || TEACH_KINDS[0];
  const isKnowledge = kind === "policy" || kind === "style" || kind === "exception";
  const blocked = !canTeach ? "Teaching isn't part of your role."
    : !text.trim() ? "Write what you want AZKT to learn."
    : kind === "exception" && !contactId.trim() ? "A buyer exception needs the contact it applies to."
    : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const body: Record<string, unknown> = { kind, text: text.trim(), title: title.trim() || undefined };
    if (isKnowledge) {
      body.content = text.trim();
      body.scope = { ...(workflow.trim() ? { workflow: workflow.trim() } : {}), ...(contactId.trim() ? { contact_id: contactId.trim() } : {}) };
    } else if (workflow.trim()) {
      body.workflow_key = workflow.trim();
    }
    const r = await run("teach", "/api/teach", body, { success: "Recorded as a proposal. Nothing is live until you promote or approve it." });
    if (r?.status === "ok") {
      setResult((r.data as Record<string, unknown>) || null);
      setText("");
      setTitle("");
      onDone();
    }
  };

  const proposedProcedure = result?.procedure as { id?: string; key?: string; title?: string; status?: string } | undefined;
  const proposedVersion = result?.version as { version_no?: number; interpretation?: string; stage?: string } | undefined;
  const proposedItem = result?.item as { id?: string; kind?: string; title?: string; status?: string; requires_owner?: boolean; usable_as?: string[] } | undefined;

  return (
    <GlassPanel padded>
      <form className="stack" onSubmit={submit}>
        <div className="stack-sm" style={{ gap: 2 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Teach AZKT</h3>
          <span className="fs13 t3">{meta.hint}</span>
        </div>

        <SegmentedControl<string>
          label="What kind of thing are you teaching?"
          value={kind}
          onChange={setKind}
          options={TEACH_KINDS.map((k) => ({ value: k.value, label: k.label }))}
        />

        <Field label="Name it" hint="Blank uses the first line of what you write.">
          <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Quote shipping to a new buyer" />
        </Field>
        <Field label={isKnowledge ? "What should be true from now on" : "The steps, in your words"}
          hint={kind === "procedure" ? "Numbered or bulleted lines become the normal path. Lines with never / must not / always become hard constraints; ask-me lines become escalation." : undefined}>
          <Textarea rows={7} value={text} onChange={(e) => setText(e.target.value)}
            placeholder={kind === "correction" ? "What it did, and what it should have done instead." : kind === "demonstration" ? "Paste the transcript or describe what you did, step by step." : "Goal: …\n1. …\n2. …\nNever quote a price before the frame number is confirmed.\nAsk me before promising a delivery date."} />
        </Field>
        <div className="form-grid">
          <Field label="Workflow key" hint="Optional. Ties this to one kind of work, e.g. reply.availability.">
            <Input value={workflow} onChange={(e) => setWorkflow(e.target.value)} placeholder="reply.availability" />
          </Field>
          {kind === "exception" ? (
            <Field label="Contact id" required hint="An exception stays scoped to this buyer and never becomes general policy.">
              <Input value={contactId} onChange={(e) => setContactId(e.target.value)} />
            </Field>
          ) : null}
        </div>

        {kind === "demonstration" ? (
          <Notice tone="risk" lead="Interpreted with uncertainty">
            Steps inferred from a demonstration are marked inferred and never installed as executable behaviour. You review each one.
          </Notice>
        ) : null}
        {kind === "policy" ? (
          <Notice tone="wait" lead="New general policy">Policies wait for your explicit acceptance. Approving one activates knowledge; it never changes a permission.</Notice>
        ) : null}

        <div className="row-wrap">
          <Button type="submit" variant="primary" size="md" loading={busy("teach")} disabled={!!blocked} disabledReason={blocked || undefined}>
            {isKnowledge ? "Propose it" : "Propose a procedure"}
          </Button>
          <span className="fs12 t3">Everything lands as a proposal for review. Teaching never grants a permission.</span>
        </div>

        {result ? (
          <Notice tone="ok" lead="Proposed" action={<Button size="sm" variant="ghost" onClick={() => setResult(null)}>Dismiss</Button>}>
            {proposedProcedure ? (
              <>Procedure <strong>{proposedProcedure.title || proposedProcedure.key}</strong> · version {proposedVersion?.version_no ?? "?"} ·
                stage {LADDER_LABELS[proposedVersion?.stage || proposedProcedure.status || "proposed"] || proposedProcedure.status}
                {proposedVersion?.interpretation === "uncertain" ? " · interpreted with uncertainty" : ""}. Run its tests before promoting it.</>
            ) : proposedItem ? (
              <>{humanize(proposedItem.kind || "knowledge")} <strong>{proposedItem.title}</strong> · {humanize(proposedItem.status || "proposed")}
                {proposedItem.usable_as?.length ? ` · usable as ${proposedItem.usable_as.join(", ")}` : ""}
                {proposedItem.requires_owner ? " · needs the owner's acceptance" : ""}. Approve it under Knowledge.</>
            ) : "Recorded."}
          </Notice>
        ) : null}
      </form>
    </GlassPanel>
  );
}

export function ProceduresSection() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const canTeach = can(user, "knowledge.write");
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const firstRef = useRef<HTMLSelectElement>(null);

  const q = useQuery<ProceduresResp>((signal) => api.get<ProceduresResp>(
    `/api/procedures?limit=200${status ? `&status=${encodeURIComponent(status)}` : ""}`, { signal, tolerate: [404, 501] },
  ), [status]);

  const items = q.data?.items || [];

  return (
    <div className="stack-lg">
      <TeachComposer canTeach={canTeach} onDone={q.reload} />

      <div className="stack">
        <div className="between">
          <div className="eyebrow">Procedures {q.data ? `· ${q.data.total}` : ""}</div>
          <Select ref={firstRef} aria-label="Filter by stage" value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: "auto", minWidth: 180 }}>
            <option value="">Every stage</option>
            {LADDER.map((s) => <option key={s} value={s}>{LADDER_LABELS[s]}</option>)}
            <option value="withdrawn">Withdrawn</option>
          </Select>
        </div>

        <GlassPanel clip>
          {q.loading ? <Loading label="Loading procedures" rows={3} />
            : q.error ? <ErrorState error={q.error} onRetry={q.reload} />
            : q.data === null ? <EmptyState title="Not available yet" body={<span><code>/api/procedures</code> is not answering. Nothing is hidden here.</span>} />
            : !items.length ? <EmptyState title="Nothing taught yet" body="Write down how you do something above. It becomes a versioned procedure you review, test and promote one step at a time." />
            : items.map((p) => {
              const cv = p.current_version;
              const tests = cv ? testState(cv) : null;
              return (
                <div key={p.id} className="lrn-row">
                  <div className="lrn-row__head">
                    <div className="lrn-row__main">
                      <div className="lrn-row__title">
                        <span className="truncate">{p.title}</span>
                        <Chip size="sm" tone="soft">{p.key}</Chip>
                        {p.workflow_key ? <Chip size="sm" tone="soft">{p.workflow_key}</Chip> : null}
                        {cv?.interpretation === "uncertain" ? <Chip size="sm" tone="risk">Interpreted with uncertainty</Chip> : null}
                      </div>
                      <div className="lrn-row__meta">{p.goal || "No goal recorded."}</div>
                      <div className="lrn-row__meta">
                        {cv ? <>Current version {cv.version_no}</> : "No current version"}
                        {tests ? <> · {tests.results ? `tests ${tests.results.passed}/${tests.results.total}` : "tests not run"}</> : null}
                        {p.last_promoted_at ? <> · promoted <When iso={p.last_promoted_at} relative /></> : null}
                      </div>
                    </div>
                    <Ladder stage={p.withdrawn_at ? "withdrawn" : p.status} />
                  </div>
                  <div className="lrn-row__actions">
                    <Button size="sm" variant="soft" onClick={() => setOpen(p.id)}>Open versions</Button>
                    {tests && !tests.ok ? <span className="fs12 t3">Promotion blocked: {tests.why}</span> : null}
                  </div>
                </div>
              );
            })}
        </GlassPanel>

        <div className="set-foot">
          A procedure is a written spec, not code: promoting it never installs a tool and never grants a permission. Tests must
          pass before a version moves up, and rolling one back keeps its history while queued work is revalidated.
        </div>
      </div>

      {open ? <ProcedureDrawer id={open} canTeach={canTeach} isOwner={!!isOwner} onClose={() => setOpen(null)} onChanged={q.reload} /> : null}
    </div>
  );
}
