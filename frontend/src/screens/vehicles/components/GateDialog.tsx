/* Gate dialog (design "READY-FOR-SALE gate dialog"). Shown when a stage move is blocked, and as a
   read-only preview from the Sale tab. Requirements carry ✓/○ with their detail; the tasks the move
   created are listed with links; factual requirements say plainly that they cannot be overridden. */
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Button, Field, Notice, ResponsiveDialog, Textarea } from "../../../ui";
import { useIsMobile } from "../../../lib/viewport";
import { stateLabel, type GateItem, type GateTaskRef } from "../types";

export interface GateDialogProps {
  open: boolean;
  onClose: () => void;
  /** Stage the vehicle is in now. */
  from: string;
  /** Stage the move was aimed at. */
  to: string;
  gates: GateItem[];
  /** Tasks the blocked move created (linked missing work). */
  tasks?: GateTaskRef[];
  /** Run the move again with reasons for the overridable gates. Omitted → no override offered. */
  onOverride?: (overrides: Record<string, string>) => Promise<void> | void;
  busy?: boolean;
  /** Read-only preview (Sale tab) — no override, no task list. */
  preview?: boolean;
  /** Extra action, e.g. "Open Work". */
  extraAction?: ReactNode;
}

export function GateList({ gates }: { gates: GateItem[] }) {
  if (!gates.length) return <span className="not-recorded">No requirements recorded for this stage.</span>;
  return (
    <div>
      {gates.map((g) => (
        <div key={`${g.to_state || ""}:${g.requirement}`} className="vh-gate">
          <span className="vh-gate__mark" style={{ color: g.ok ? "var(--ok)" : "var(--t4)" }} aria-hidden="true">{g.ok ? "✓" : "○"}</span>
          <span className="vh-gate__body">
            <span>{g.label}</span>
            {g.detail ? <span className="vh-gate__detail">{g.detail}</span> : null}
            {g.items?.length ? <span className="vh-gate__detail">{g.items.slice(0, 4).join(" · ")}{g.items.length > 4 ? ` · +${g.items.length - 4} more` : ""}</span> : null}
            {g.override_refused ? <span className="vh-gate__detail" style={{ color: "var(--blocked)" }}>{g.override_refused}</span> : null}
          </span>
          <span className="vh-gate__state">{g.ok ? "Done" : g.factual ? "Not done · factual" : "Not done"}</span>
        </div>
      ))}
    </div>
  );
}

export function GateDialog({ open, onClose, from, to, gates, tasks = [], onOverride, busy = false, preview = false, extraAction }: GateDialogProps) {
  const mobile = useIsMobile();
  const unmet = gates.filter((g) => !g.ok);
  const overridable = unmet.filter((g) => g.overridable && !g.factual);
  const factual = unmet.filter((g) => g.factual || !g.overridable);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [showOverride, setShowOverride] = useState(false);

  useEffect(() => { if (open) { setReasons({}); setShowOverride(false); } }, [open]);

  const canOverride = !!onOverride && overridable.length > 0 && factual.length === 0;
  const allReasonsGiven = overridable.every((g) => (reasons[g.requirement] || "").trim().length > 0);

  const title = unmet.length === 0
    ? `${stateLabel(to)} — requirements met`
    : `${stateLabel(to)} — not yet`;
  const description = unmet.length === 0
    ? `Every requirement for ${stateLabel(to)} is satisfied.`
    : `${unmet.length} requirement${unmet.length === 1 ? " is" : "s are"} unmet. The vehicle stays ${stateLabel(from)}.`;

  return (
    <ResponsiveDialog
      mobile={mobile}
      open={open}
      onClose={onClose}
      title={title}
      description={description}
      size="md"
      footer={
        <>
          {canOverride && showOverride ? (
            <Button
              variant="primary"
              loading={busy}
              disabled={!allReasonsGiven}
              disabledReason="Write a reason for each requirement you are overriding."
              onClick={() => { void onOverride?.(reasons); }}
            >
              Move with reason
            </Button>
          ) : null}
          <Button variant={canOverride && showOverride ? "ghost" : "glass"} onClick={onClose}>{preview ? "Close" : "Cancel"}</Button>
          {extraAction}
          {canOverride && !showOverride ? (
            <button type="button" className="linklike fs13" style={{ marginLeft: "auto" }} onClick={() => setShowOverride(true)}>Override with reason…</button>
          ) : null}
        </>
      }
    >
      <div className="stack" style={{ gap: 14 }}>
        <GateList gates={gates} />

        {tasks.length ? (
          <div className="stack-sm">
            <div className="fs12 t3" style={{ letterSpacing: ".04em", textTransform: "uppercase" }}>Work created</div>
            {tasks.map((t, i) => (
              <div key={`${t.gate}:${t.task_id || i}`} className="vh-link">
                {t.task_id ? (
                  <Link to={`/tasks/${encodeURIComponent(t.task_id)}`} className="wrap">{t.title || "Open task"}</Link>
                ) : (
                  <span>{t.title || "Task"}</span>
                )}
                <span className="vh-link__meta">{t.created ? "Created now" : "Already open"}</span>
              </div>
            ))}
            <span className="fs12 t4">The stage is unchanged. Finish this work, then move the vehicle again.</span>
          </div>
        ) : null}

        {factual.length ? (
          <Notice tone="risk" lead="Cannot be overridden">
            {factual.length === 1 ? "This requirement is" : "These requirements are"} checked against the record
            ({factual.map((g) => g.label).join(", ")}). An override cannot turn a factual check green — record the
            evidence and the check turns green by itself.
          </Notice>
        ) : null}

        {canOverride && showOverride ? (
          <div className="stack-sm">
            {overridable.map((g) => (
              <Field key={g.requirement} label={`Why override "${g.label}"?`} required hint="Recorded with your name on the vehicle's history.">
                <Textarea
                  rows={2}
                  value={reasons[g.requirement] || ""}
                  onChange={(e) => setReasons((r) => ({ ...r, [g.requirement]: e.target.value }))}
                  placeholder="What makes this safe to skip"
                />
              </Field>
            ))}
          </div>
        ) : null}
      </div>
    </ResponsiveDialog>
  );
}

export default GateDialog;
