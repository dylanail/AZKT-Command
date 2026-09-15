/* The Active Search gate, exactly as the API returns it (services/sourcing.gates). Each row says what it is,
   whether it is met and — when it is not — the recorded reason. Nothing here is computed in the browser. */
import type { ReactNode } from "react";
import { Chip, HealthLabel } from "../../../ui";
import { GATE_LABEL, type GateDecision } from "../types";

export interface GateListProps {
  gate: GateDecision | null | undefined;
  /** Rendered under the list (e.g. "Deposit rule not set" notice). */
  footer?: ReactNode;
  /** Actions offered per gate key, e.g. { agreement: <Button/> }. */
  actions?: Record<string, ReactNode>;
}

export function GateList({ gate, footer, actions }: GateListProps) {
  if (!gate || !gate.gates?.length) {
    return <p className="fs13 t3">The gate list is not recorded for this request yet.</p>;
  }
  const blocked = gate.decision !== "Allowed";
  return (
    <div className="gate">
      <div className="gate__head">
        <HealthLabel health={blocked ? "wait" : "ok"} size="md" label={blocked ? "Active search is blocked" : "Active search can open"} />
        <span className="fs12 t4">{blocked ? `${gate.gates.filter((g) => !g.ok).length} of ${gate.gates.length} checks not met` : `All ${gate.gates.length} checks met`}</span>
      </div>
      <ul className="gate__list">
        {gate.gates.map((g) => (
          <li key={g.key} className={["gate__row", g.ok ? "gate__row--ok" : ""].filter(Boolean).join(" ")}>
            <span className="gate__mark" aria-hidden="true">{g.ok ? "✓" : "•"}</span>
            <span className="gate__main">
              <span className="gate__label">{GATE_LABEL[g.key] || g.key.replace(/_/g, " ")}</span>
              <span className="gate__reason">{g.ok ? "Met" : g.reason || "Not met — no reason recorded"}</span>
            </span>
            <span className="gate__right">
              <Chip size="sm" tone={g.ok ? "ok" : "soft"}>{g.status || (g.ok ? "ok" : "not met")}</Chip>
              {actions?.[g.key]}
            </span>
          </li>
        ))}
      </ul>
      {footer}
    </div>
  );
}

export default GateList;
