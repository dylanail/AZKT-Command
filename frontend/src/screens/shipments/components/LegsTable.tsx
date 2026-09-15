/* The shipment's legs: export, ocean, port and domestic, each with its own status, carrier and evidence.
   A booked leg only ever becomes booked through an approved booking, so booking is not editable here.
   Amounts are shown only to roles with cost access. */
import { Link } from "react-router-dom";
import { TZ } from "../../../lib/format";
import { Button, Chip, EmptyState, Money, Table, When } from "../../../ui";
import { DualTime } from "../../requests/components/DualTime";
import { LEG_KIND_LABEL, LEG_STATUS_LABEL, type Leg, type VehicleMember } from "../types";

export interface LegsTableProps {
  legs: Leg[];
  vehicles: VehicleMember[];
  costs: boolean;
  onUpdate?: (leg: Leg) => void;
  onCancel?: (leg: Leg) => void;
  writeReason?: string;
}

function legTone(status: string): "ok" | "wait" | "blocked" | "soft" {
  if (status === "complete") return "ok";
  if (status === "cancelled") return "blocked";
  if (status === "booked" || status === "in_progress") return "wait";
  return "soft";
}

export function LegsTable({ legs, vehicles, costs, onUpdate, onCancel, writeReason }: LegsTableProps) {
  if (!legs.length) {
    return <EmptyState align="left" title="No legs recorded" body="Export, ocean, port and domestic legs are added as they are arranged. Booking a leg is a separate approved decision." />;
  }
  const titleOf = (vid: string | null) => (vid ? vehicles.find((v) => v.id === vid)?.title || vid : "Whole container");
  return (
    <Table minWidth={860} caption="One row per leg. A leg becomes Booked only through an approved booking.">
      <thead>
        <tr>
          <th>Leg</th>
          <th>Status</th>
          <th>Carrier</th>
          <th>Route</th>
          <th>Appointment / pickup / delivered</th>
          {costs ? <th className="num">Amount</th> : null}
          <th aria-label="Actions" />
        </tr>
      </thead>
      <tbody>
        {legs.map((l) => (
          <tr key={l.id}>
            <td>
              <div style={{ fontWeight: 500 }}>{LEG_KIND_LABEL[l.kind] || l.kind}</div>
              <div className="fs12 t3">
                {l.vehicle_id ? <Link to={`/vehicles/${encodeURIComponent(l.vehicle_id)}`}>{titleOf(l.vehicle_id)}</Link> : "Whole container"}
                {l.booking_ref ? <span> · booking {l.booking_ref}</span> : null}
              </div>
            </td>
            <td><Chip size="sm" tone={legTone(l.status)}>{LEG_STATUS_LABEL[l.status] || l.status}</Chip></td>
            <td>
              {l.carrier_name || (l.carrier_contact_id ? <Link to={`/contacts/${encodeURIComponent(l.carrier_contact_id)}`}>Carrier contact</Link> : <span className="not-recorded">Not recorded</span>)}
              {l.driver_contact ? <div className="fs12 t3">driver {l.driver_contact}</div> : null}
            </td>
            <td className="fs13">{l.route_from || l.route_to ? `${l.route_from || "?"} → ${l.route_to || "?"}` : <span className="not-recorded">Not recorded</span>}</td>
            <td className="fs13">
              <div>Appointment: <DualTime value={l.appointment} empty="Not booked" /></div>
              <div>Pickup: {l.pickup_at ? <When iso={l.pickup_at} tz={TZ.phoenix} format="datetime" /> : <span className="not-recorded">Not recorded</span>}</div>
              <div>Delivered: {l.delivered_at ? <When iso={l.delivered_at} tz={TZ.phoenix} format="datetime" /> : <span className="not-recorded">Not recorded</span>}</div>
            </td>
            {costs ? <td className="num"><Money amount={l.amount} currency={l.currency || "USD"} /></td> : null}
            <td className="actions">
              {onUpdate ? (
                <Button size="xs" variant="soft" onClick={() => onUpdate(l)} disabled={!!writeReason || l.status === "cancelled"} disabledReason={writeReason || "This leg is cancelled."}>Update</Button>
              ) : null}
              {onCancel ? (
                <Button size="xs" variant="ghost" onClick={() => onCancel(l)} disabled={!!writeReason || l.status === "booked" || l.status === "cancelled"}
                  disabledReason={writeReason || (l.status === "booked" ? "Cancel the booking with the carrier first, then record it." : "Already cancelled.")}>Cancel</Button>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

export default LegsTable;
