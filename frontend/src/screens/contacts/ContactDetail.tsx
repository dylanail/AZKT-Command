/* Contact detail: identity, relationship, linked vehicles/requests, promises, consent, last touch, actions.
   TODO(screen builder): GET /api/contacts/{id} ; PATCH /api/contacts/{id} ; GET /api/contacts/{id}/activity. */
import { useParams } from "react-router-dom";
import { Scaffold } from "../scaffold";

export default function ContactDetail() {
  const { id = "" } = useParams();
  return (
    <Scaffold title={`Contact ${id}`} crumbs={[{ label: "Contacts", to: "/contacts" }, { label: id }]} probe={`/api/contacts/${encodeURIComponent(id)}`} emptyTitle="Contact not found" emptyBody={`Nothing recorded for ${id} yet.`} />
  );
}
