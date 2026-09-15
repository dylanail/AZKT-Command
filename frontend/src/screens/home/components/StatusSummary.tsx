/* 1. Status — the source-backed summary sentence and connection freshness (spec §2.2).
   The sentence comes from the server, which never claims an all-clear while a required source is behind:
   "No urgent items found in synced data; Gmail needs attention". We render it verbatim. */
import { Link } from "react-router-dom";
import { Chip, ErrorState, GlassPanel, HealthLabel, Skeleton, When } from "../../../ui";
import type { ConnectionRow, StatusSection } from "../types";
import { sentence } from "./parts";

const STATE_TONE: Record<string, "ok" | "risk" | "blocked" | "wait" | "soft"> = {
  ok: "ok",
  warn: "risk",
  degraded: "risk",
  expired: "blocked",
  disconnected: "blocked",
};

function tone(state: string): "ok" | "risk" | "blocked" | "wait" | "soft" {
  return STATE_TONE[state] || "soft";
}

function lastSync(rows: ConnectionRow[]): string | null {
  const times = rows.map((c) => c.freshness.last_success_at || c.last_success_at).filter((t): t is string => !!t);
  if (!times.length) return null;
  return times.sort()[times.length - 1];
}

export function StatusSummary({
  status, loading, onRetry, canOpenConnections, fallbackTitle,
}: {
  status: StatusSection | null | undefined;
  loading: boolean;
  onRetry: () => void;
  canOpenConnections: boolean;
  fallbackTitle: string;
}) {
  if (loading && !status) {
    return (
      <div className="stack-sm" aria-busy="true">
        <Skeleton width="72%" height={28} radius={10} />
        <Skeleton width="42%" height={13} />
      </div>
    );
  }
  if (status && status.available === false) {
    return (
      <div className="stack-sm">
        <h1>{fallbackTitle}</h1>
        <GlassPanel clip>
          <ErrorState
            error={new Error(status.reason || "Connection status could not be read.")}
            title="Couldn't read the status summary"
            onRetry={onRetry}
          />
        </GlassPanel>
      </div>
    );
  }
  if (!status) return <h1>{fallbackTitle}</h1>;

  const required = status.connections.filter((c) => c.required);
  const shown = required.length ? required : status.connections;
  const unhealthy = shown.filter((c) => c.freshness.state !== "ok");
  const synced = lastSync(status.connections);

  return (
    <div className="stack-sm">
      <h1>{sentence(status.summary)}</h1>
      <div className="hm-status-meta fs13 t3 tnum">
        <span>
          {shown.length
            ? `Based on ${shown.length} connected source${shown.length === 1 ? "" : "s"}`
            : "No connected sources yet"}
        </span>
        <span aria-hidden="true">·</span>
        <span>
          {synced ? <>last successful sync <When iso={synced} format="short" /></> : "no successful sync recorded"}
        </span>
        {status.as_of ? (
          <>
            <span aria-hidden="true">·</span>
            <span>checked <When iso={status.as_of} format="time" /></span>
          </>
        ) : null}
      </div>

      {shown.length ? (
        <div className="hm-conns" role="group" aria-label="Connection freshness">
          {shown.map((c) => (
            <Chip key={c.provider} size="sm" tone={tone(c.freshness.state)} title={`${c.label}: ${c.freshness.label}`}>
              {c.label} · {c.freshness.label}
            </Chip>
          ))}
          {canOpenConnections ? (
            <Link className="fs13" to="/settings/connections">Connections</Link>
          ) : (
            <span className="fs12 t4" title="Only the owner manages connections.">Only the owner manages connections</span>
          )}
        </div>
      ) : null}

      {unhealthy.length && !status.all_clear_possible ? (
        <div className="hm-stale-note fs13">
          <HealthLabel health="risk" label="Synced data is incomplete" />
          <span className="t2">
            {" "}Nothing here can be called all-clear until {status.stale_connections.join(", ") || "the connection"} catches up.
          </span>
        </div>
      ) : null}
    </div>
  );
}