/* Shown when a route isn't allowed for the signed-in role (client gate) or the server answers 403. */
import { useAuth } from "../../lib/auth";
import { isEmployeeRole, ROLE_LABELS, type Role } from "../../lib/perms";
import { Button, EmptyState, GlassPanel, PageHeader } from "../../ui";

export default function Denied() {
  const { user } = useAuth();
  const role = (user?.role || "") as Role;
  const home = isEmployeeRole(role) ? "/tasks" : "/";
  return (
    <div className="page page-narrow">
      <PageHeader title="Not for your role" subtitle={user ? `Signed in as ${user.display_name} · ${ROLE_LABELS[role] || role}` : undefined} />
      <GlassPanel clip>
        <EmptyState title="This page isn't available to you" body="If you need it, ask the owner to change what your role can do." action={<Button to={home} variant="glass">Back to {isEmployeeRole(role) ? "My tasks" : "Home"}</Button>} />
      </GlassPanel>
    </div>
  );
}
