/* The checks a package must pass before it can be submitted or published.
   Source: ListingPackage.readiness (backend evaluate_gates → site profile listing gates per class).
   The first unresolved check is shown on its own with what would fix it and where to do it. */
import { Link } from "react-router-dom";
import { Button, EmptyState } from "../../../ui";
import { checkFix, type ReadinessCheck } from "../types";

export function FirstBlocker({ checks, vehicleId, onRefresh, refreshing }: {
  checks: ReadinessCheck[];
  vehicleId: string;
  onRefresh?: () => void;
  refreshing?: boolean;
}) {
  const first = checks.find((c) => !c.ok);
  if (!first) return null;
  const fix = checkFix(first.requirement, vehicleId);
  const others = checks.filter((c) => !c.ok).length - 1;
  return (
    <div className="lst-blocker" role="status">
      <span className="lst-blocker__title">{first.label}</span>
      <span className="lst-blocker__body">
        {first.detail || "This check has not passed."}
        {fix ? <> {fix.how}</> : null}
        {others > 0 ? <> {others === 1 ? "One more check" : `${others} more checks`} below.</> : null}
      </span>
      <div className="lst-blocker__actions">
        {fix ? <Button size="sm" variant="primary" to={fix.to}>{fix.label}</Button> : null}
        {onRefresh ? (
          <Button size="sm" variant="soft" onClick={onRefresh} loading={refreshing}>
            Rebuild and re-check
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export function ChecksList({ checks, vehicleId }: { checks: ReadinessCheck[]; vehicleId: string }) {
  if (!checks.length) {
    return (
      <EmptyState
        align="left"
        title="No checks recorded"
        body="The site profile hasn't been asked for this listing class yet. Build the package to evaluate it."
      />
    );
  }
  return (
    <div className="lst-checks">
      {checks.map((c, i) => {
        const fix = c.ok ? null : checkFix(c.requirement, vehicleId);
        return (
          <div key={`${c.requirement}-${i}`} className={`lst-check ${c.ok ? "lst-check--ok" : "lst-check--fail"}`}>
            <span className="lst-check__mark" aria-hidden="true">{c.ok ? "✓" : "!"}</span>
            <span className="lst-check__body">
              <span className="lst-check__label">
                {c.label}
                <span className="sr-only">{c.ok ? " — passed" : " — not passed"}</span>
              </span>
              <span className="lst-check__detail">
                {c.detail || (c.ok ? "Passed." : "Not passed.")}
                {fix ? <> · <Link to={fix.to}>{fix.label}</Link></> : null}
              </span>
            </span>
          </div>
        );
      })}
    </div>
  );
}
