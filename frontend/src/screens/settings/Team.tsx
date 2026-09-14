/* Team (owner) / People (manager): per-person role, scope, manager, per-key permission overrides
   (owner-locked keys stay locked), status (active | invited | disabled), invite links.
   TODO(screen builder): GET /api/team ; POST /api/team/invite {display_name, contact, role, scope, manager_id, perms} ;
   PATCH /api/team/{id} ; POST /api/team/{id}/disable|enable ; POST /api/team/{id}/resend-invite ; DELETE /api/team/{id}. */
import { useAuth, useCan } from "../../lib/auth";
import { whyNot } from "../../lib/perms";
import { Button } from "../../ui";
import { Scaffold } from "../scaffold";

export default function Team() {
  const { user } = useAuth();
  const can = useCan();
  const owner = user?.role === "owner";
  return (
    <Scaffold
      title={owner ? "Team" : "People"}
      subtitle={owner ? "Role sets defaults; every permission is individually overridable." : "People who report to you."}
      crumbs={[{ label: "Settings", to: "/settings" }, { label: owner ? "Team" : "People" }]}
      probe="/api/team"
      emptyTitle={owner ? "Just you so far" : "No one reports to you yet"}
      emptyBody={owner ? "Invite someone; nothing changes until they accept the link." : undefined}
      actions={<Button variant="primary" disabled={!can("team")} disabledReason={whyNot("team")}>Invite person</Button>}
    />
  );
}
