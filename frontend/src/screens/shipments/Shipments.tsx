/* Shipments: containers and consignments in flight, with where each one is, what it carries, when it is
   expected and whether a storage deadline is running. Reads GET /api/shipments. */
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { relativeTime, TZ } from "../../lib/format";
import { Badge, Chip, EmptyState, ErrorState, GlassPanel, Input, ListRow, Loading, PageHeader, When } from "../../ui";
import { DeniedOrError } from "../requests/components/DeniedPanel";
import { DualTime } from "../requests/components/DualTime";
import { shipmentsPath } from "./api";
import { MILESTONE_LABEL, SHIPMENT_LABEL, shipmentHealth, sourceLabel, type Shipment, type ShipmentListResp } from "./types";
import "./shipments.css";
import "../requests/requests.css";

type FilterKey = "active" | "at_port" | "exception" | "complete" | "all";
const FILTERS: { key: FilterKey; label: string }[] = [
  { key: "active", label: "In flight" },
  { key: "at_port", label: "At port" },
  { key: "exception", label: "Exceptions" },
  { key: "complete", label: "Complete" },
  { key: "all", label: "All" },
];

function matches(s: Shipment, f: FilterKey): boolean {
  switch (f) {
    case "active": return s.status !== "complete";
    case "at_port": return s.status === "at_port" || s.status === "released";
    case "exception": return s.status === "exception";
    case "complete": return s.status === "complete" || s.status === "received";
    default: return true;
  }
}

function storageUrgency(s: Shipment): { tone: "blocked" | "risk"; label: string } | null {
  const iso = s.storage_deadline?.utc;
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  if (t <= Date.now()) return { tone: "blocked", label: "Storage deadline passed" };
  if (t - Date.now() < 3 * 86400000) return { tone: "risk", label: "Storage deadline within 3 days" };
  return null;
}

export default function Shipments() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const filter = (FILTERS.find((f) => f.key === params.get("filter"))?.key || "active") as FilterKey;
  const q = params.get("q") || "";
  const [search, setSearch] = useState(q);

  const setParam = (k: string, v: string | null) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v); else p.delete(k);
    setParams(p, { replace: true });
  };

  useEffect(() => { setSearch(q); }, [q]);
  useEffect(() => {
    const h = window.setTimeout(() => { if (search.trim() !== q) setParam("q", search.trim() || null); }, 300);
    return () => window.clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const list = useQuery<ShipmentListResp | null>(
    (signal) => api.get<ShipmentListResp | null>(shipmentsPath("?include_complete=true&limit=200"), { signal, tolerate: [404, 501] }),
    [],
  );

  const rows = useMemo(() => list.data?.items || [], [list.data]);
  const counts = useMemo(() => {
    const out = {} as Record<FilterKey, number>;
    for (const f of FILTERS) out[f.key] = rows.filter((s) => matches(s, f.key)).length;
    return out;
  }, [rows]);
  const needle = q.trim().toLowerCase();
  const shown = useMemo(() => rows.filter((s) => matches(s, filter)).filter((s) => {
    if (!needle) return true;
    return [s.ref, s.container_no, s.vessel, s.voyage, s.route_from, s.route_to].filter(Boolean).some((x) => String(x).toLowerCase().includes(needle));
  }), [rows, filter, needle]);

  const read = can(user, "shipping.read");

  return (
    <div className="page page-wide">
      <PageHeader
        title="Shipments"
        subtitle={list.data ? `${counts.active} in flight · ${counts.exception} with an exception` : "Containers, vessels, legs and milestones."}
      >
        <div className="shp-toolbar">
          <div className="shp-chips" role="group" aria-label="Filter shipments">
            {FILTERS.map((f) => (
              <Chip key={f.key} selected={filter === f.key} tone={filter === f.key ? "act" : "neutral"} count={list.data ? counts[f.key] : undefined} onClick={() => setParam("filter", f.key === "active" ? null : f.key)}>
                {f.label}
              </Chip>
            ))}
          </div>
          <Input className="irq-search" pill value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Ref, container, vessel or port…" aria-label="Search shipments" />
        </div>
      </PageHeader>

      <GlassPanel clip>
        {list.loading ? <Loading label="Loading shipments" rows={4} />
          : list.error ? <DeniedOrError error={list.error} onRetry={list.reload} what="shipments" />
          : list.data === null ? <EmptyState title="Shipments aren't connected yet" body={read ? "This list fills in once the shipping API is live." : "Your role can't see shipments."} />
          : shown.length === 0 ? <EmptyState title={needle ? "No shipments match" : `Nothing ${FILTERS.find((f) => f.key === filter)?.label.toLowerCase()}`} body="Shipments are created when a purchased vehicle is booked onto a container." />
          : shown.map((s) => <ShipmentRow key={s.id} s={s} />)}
      </GlassPanel>
      {list.data && list.data.total > rows.length ? <span className="fs12 t4">Showing the {rows.length} most recently updated of {list.data.total}.</span> : null}
    </div>
  );
}

function ShipmentRow({ s }: { s: Shipment }) {
  const health = shipmentHealth(s.status);
  const storage = storageUrgency(s);
  const latest = s.latest_milestone;
  return (
    <ListRow
      to={`/shipments/${encodeURIComponent(s.id)}`}
      title={s.ref || `Shipment ${s.id.slice(0, 8)}`}
      health={health || undefined}
      healthLabel={SHIPMENT_LABEL[s.status] || s.status}
      tags={
        <span className="irq-row__tags">
          {s.container_no ? <Chip size="sm" tone="soft">{s.container_no}</Chip> : null}
          {storage ? <Badge tone={storage.tone}>{storage.label}</Badge> : null}
        </span>
      }
      meta={
        <span className="row-wrap" style={{ gap: 6 }}>
          <span>{s.route_from || "origin not recorded"} → {s.route_to || "destination not recorded"}</span>
          <span>· {s.vehicle_count ?? s.vehicle_ids?.length ?? 0} vehicle{(s.vehicle_count ?? s.vehicle_ids?.length ?? 0) === 1 ? "" : "s"}</span>
          {s.vessel ? <span>· {s.vessel}{s.voyage ? ` ${s.voyage}` : ""}</span> : null}
          {latest ? <span>· last: {MILESTONE_LABEL[latest.kind] || latest.kind} ({latest.status})</span> : null}
        </span>
      }
      right={
        <span className="shp-row__right">
          <span className="fs13"><DualTime value={s.eta} empty="ETA not recorded" /></span>
          <span className="shp-row__when">
            {s.eta?.utc ? sourceLabel(s.eta_source, "estimate") : s.updated_at ? `updated ${relativeTime(s.updated_at)}` : ""}
          </span>
          {s.storage_deadline?.utc ? (
            <span className="shp-row__when">storage until <When iso={s.storage_deadline.utc} tz={TZ.phoenix} format="date" /></span>
          ) : null}
        </span>
      }
    />
  );
}
