/* Shipment record (not yet designed in the prototype): carrier, vessel/ETA, ports, documents, releases, linked vehicles.
   TODO(screen builder): GET /api/shipments/{id} ; PATCH /api/shipments/{id} ; GET /api/shipments/{id}/documents. */
import { useParams } from "react-router-dom";
import { Scaffold } from "../scaffold";

export default function ShipmentDetail() {
  const { id = "" } = useParams();
  return <Scaffold title={`Shipment ${id}`} crumbs={[{ label: "Vehicles", to: "/vehicles?view=shipping" }, { label: id }]} probe={`/api/shipments/${encodeURIComponent(id)}`} emptyTitle="Shipment not found" emptyBody={`Nothing recorded for ${id} yet.`} />;
}
