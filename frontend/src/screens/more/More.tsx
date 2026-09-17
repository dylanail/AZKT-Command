/* Mobile "More": the destinations not in the bottom nav, appearance toggles, help and account.
   Mechanic: Help (how to complete a task, evidence, blockers) and Account, which shares the same
   passkey block as Settings › Recovery — everyone adds a second device the same way.
   TODO(screen builder): counts per row (GET /api/counts), help content. */
import { Link, useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { useTheme } from "../../lib/theme";
import { isEmployeeRole, ROLE_LABELS, type Role } from "../../lib/perms";
import { navFor } from "../../app/nav";
import { Button, GlassPanel, PageHeader, Switch } from "../../ui";
import { PasskeysBlock } from "../settings/components/PasskeysBlock";

export default function More() {
  const { section } = useParams();
  const { user, logout } = useAuth();
  const { theme, toggleTheme, glass, toggleGlass } = useTheme();
  const nav = useNavigate();
  const role = (user?.role || "mechanic") as Role;
  const employee = isEmployeeRole(role);
  const items = navFor(role).more;

  if (section === "account") {
    return (
      <div className="page">
        <PageHeader title="Account" subtitle={`${user?.display_name} · ${ROLE_LABELS[role] || role}`} crumbs={[{ label: "More", to: "/more" }, { label: "Account" }]} />
        <GlassPanel padded>
          <div className="stack-sm">
            <Switch checked={theme === "dark"} onChange={toggleTheme} label="Dark mode" />
            <Switch checked={glass === "off"} onChange={toggleGlass} label="Reduce transparency" />
          </div>
        </GlassPanel>
        <PasskeysBlock />
      </div>
    );
  }
  if (section === "help") {
    return (
      <div className="page">
        <PageHeader title="Help" crumbs={[{ label: "More", to: "/more" }, { label: "Help" }]} />
        <GlassPanel clip>
          {[
            ["Finishing a task", "Add the required evidence, then Complete task. Dylan verifies."],
            ["When you're stuck", "Tap I'm blocked and say why. Dylan is notified; the task stays open."],
            ["No signal in the bay", "Uploads keep a draft on this phone and retry. The task isn't complete until it uploads."],
            ["Who to ask", "Your manager assigns work; the owner verifies and approves."],
          ].map(([t, b]) => (
            <div key={t} className="list-row"><div className="list-row__main"><div className="list-row__title">{t}</div><div className="list-row__meta" style={{ whiteSpace: "normal" }}>{b}</div></div></div>
          ))}
        </GlassPanel>
      </div>
    );
  }

  return (
    <div className="page">
      <PageHeader title="More" subtitle={`${user?.display_name} · ${ROLE_LABELS[role] || role}`} />
      <GlassPanel clip>
        {employee ? (
          <>
            <Link to="/more/help" className="more-row"><span>Help</span><span className="more-row__meta">›</span></Link>
            <Link to="/more/account" className="more-row"><span>Account</span><span className="more-row__meta">Ways to sign in · sign out</span></Link>
          </>
        ) : items.map((n) => (
          <Link key={n.key} to={n.to} className="more-row"><span>{n.label}</span><span className="more-row__meta">›</span></Link>
        ))}
      </GlassPanel>
      <GlassPanel padded>
        <div className="stack-sm">
          <div className="eyebrow">Appearance</div>
          <Switch checked={theme === "dark"} onChange={toggleTheme} label="Dark mode" />
          <Switch checked={glass === "off"} onChange={toggleGlass} label="Reduce transparency" />
        </div>
      </GlassPanel>
      {!employee ? (
        <div className="row-wrap">
          <Button variant="glass" to="/settings/recovery">Account &amp; passkeys</Button>
          <Button variant="ghost" onClick={() => { void logout().then(() => nav("/login", { replace: true })); }}>Sign out</Button>
        </div>
      ) : null}
    </div>
  );
}
