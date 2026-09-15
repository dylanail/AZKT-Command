/* Versioned buyer requirements in three tiers, with the stable key and the recorded check for each line.
   A requirement without a check reads "no checkable criterion" — it can never silently pass (spec §8.1). */
import type { ReactNode } from "react";
import { Chip, EmptyState } from "../../../ui";
import { OP_LABEL, TIERS, TIER_LABEL, type Requirement, type RequirementTiersMap } from "../types";

export function criterionText(r: Requirement): string {
  if (!r.field) return "No checkable criterion — outcome stays Unknown";
  const op = OP_LABEL[r.op || "eq"] || r.op || "is";
  if (r.op === "truthy" || r.op === "falsy") return `${r.field} ${op}`;
  const v = Array.isArray(r.value) ? r.value.join(", ") : r.value === null || r.value === undefined ? "" : String(r.value);
  return `${r.field} ${op} ${v}`.trim();
}

export interface RequirementTiersProps {
  tiers: RequirementTiersMap | null | undefined;
  version?: number;
  /** Right-hand slot in the header (Revise button). */
  action?: ReactNode;
  /** Only render these tiers (candidate page shows all three). */
  only?: Array<keyof RequirementTiersMap>;
  /** Compact rows for a card. */
  compact?: boolean;
}

export function RequirementTiers({ tiers, version, action, only, compact = false }: RequirementTiersProps) {
  const list = (only || TIERS) as Array<keyof RequirementTiersMap>;
  const total = list.reduce((n, t) => n + (tiers?.[t]?.length || 0), 0);
  return (
    <div className="req">
      {action || version !== undefined ? (
        <div className="req__head">
          <span className="fs12 t4">{version !== undefined ? `Requirements v${version}` : ""}</span>
          {action}
        </div>
      ) : null}
      {total === 0 ? (
        <EmptyState align="left" title="No requirements recorded" body="Active search needs at least one must-have with a checkable criterion." />
      ) : (
        list.map((tier) => {
          const rows = tiers?.[tier] || [];
          return (
            <div key={tier} className="req__tier">
              <div className="req__tier-title">
                <span>{TIER_LABEL[tier as "must" | "prefer" | "avoid"]}</span>
                <span className="t4">{rows.length}</span>
              </div>
              {rows.length === 0 ? (
                <p className="fs13 t4 req__none">None recorded</p>
              ) : (
                <ul className="req__lines">
                  {rows.map((r) => (
                    <li key={r.key} className="req__line">
                      <span className="req__text">{r.text || r.key}</span>
                      <span className="req__meta">
                        <Chip size="sm" tone="soft" title="Stable key used by every evaluation">{r.key}</Chip>
                        {compact ? null : <span className={["req__crit", r.field ? "" : "req__crit--none"].filter(Boolean).join(" ")}>{criterionText(r)}</span>}
                        {r.source_ref ? <span className="fs12 t4">evidence {r.source_ref}</span> : null}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}

export default RequirementTiers;
