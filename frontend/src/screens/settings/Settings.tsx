/* Settings, routed by /settings/:section — connections (default) · team · reminders · automation · external-agents ·
   procedures · knowledge · recovery · usage. Owner edits; other roles see what their permissions allow, with reasons.
   Appearance (dark / reduced transparency / reduced motion) is per device and lives at the top. */
import { useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { useTheme } from "../../lib/theme";
import { can } from "../../lib/perms";
import { Button, GlassPanel, PageHeader, Switch, Tabs } from "../../ui";
import { ConnectionsSection } from "./components/ConnectionsSection";
import { TeamSection } from "./components/TeamSection";
import { RemindersSection } from "./components/RemindersSection";
import { AutomationSection } from "./components/AutomationSection";
import { LaterSection } from "./components/LaterSection";
import { ProceduresSection } from "./components/ProceduresSection";
import { KnowledgeSection } from "./components/KnowledgeSection";
import { RecoverySection } from "./components/RecoverySection";
import { UsageSection } from "./components/UsageSection";

const SECTIONS = [
  { id: "connections", label: "Connections", blurb: "Every source AZKT reads or writes, with freshness. A stale source is said out loud." },
  { id: "team", label: "Team", blurb: "" },
  { id: "reminders", label: "Reminders", blurb: "Where each reminder reaches you, quiet hours and the digest." },
  { id: "automation", label: "Automation", blurb: "Pause switches, spending caps and the shop's stage gates." },
  { id: "external-agents", label: "External agents", blurb: "Scoped clients that talk to the Manager." },
  { id: "procedures", label: "Procedures", blurb: "Teach AZKT how you do things." },
  { id: "knowledge", label: "Knowledge", blurb: "What AZKT knows and where it came from." },
  { id: "recovery", label: "Recovery", blurb: "System health, passkeys and sign out." },
  { id: "usage", label: "Usage", blurb: "Model budget and spend." },
] as const;
type SectionId = (typeof SECTIONS)[number]["id"];

export default function Settings() {
  const { section } = useParams();
  const nav = useNavigate();
  const { user } = useAuth();
  const { theme, toggleTheme, glass, toggleGlass, motion, setMotion } = useTheme();
  const current: SectionId = SECTIONS.find((s) => s.id === section)?.id || "connections";
  const owner = user?.role === "owner";
  const settingsPerm = can(user, "settings");
  const tabs = SECTIONS.filter((s) => (s.id === "usage" ? settingsPerm : true)).map((s) => ({ id: s.id, label: s.id === "team" ? (owner ? "Team" : "People") : s.label }));
  const meta = SECTIONS.find((s) => s.id === current);

  return (
    <div className="page">
      <PageHeader
        title="Settings"
        subtitle={settingsPerm ? "Only the owner changes these. Everything works without a model." : "Your own preferences, passkeys and what you're allowed to see. Business settings are owner-only."}
        actions={<Button variant="glass" to="/settings/team">{owner ? "Team" : "People"}</Button>}
      >
        <Tabs<SectionId> label="Settings sections" value={current} onChange={(id) => nav(id === "team" ? "/settings/team" : `/settings/${id}`)} tabs={tabs} />
      </PageHeader>

      {current === "connections" ? (
        <GlassPanel padded>
          <div className="stack-sm">
            <div className="eyebrow">Appearance · this device</div>
            <Switch checked={theme === "dark"} onChange={toggleTheme} label="Dark mode" meta="Persists on this device" />
            <Switch checked={glass === "off"} onChange={toggleGlass} label="Reduce transparency" meta="Opaque panels, no blur" />
            <Switch checked={motion === "reduced"} onChange={(on) => setMotion(on ? "reduced" : "system")} label="Reduce motion" meta="Also follows your system setting" />
          </div>
        </GlassPanel>
      ) : null}

      <section className="stack" aria-labelledby="settings-section-title">
        {current !== "team" ? (
          <div>
            <h2 id="settings-section-title">{meta?.label}</h2>
            {meta?.blurb ? <div className="fs13 t3" style={{ marginTop: 2 }}>{meta.blurb}</div> : null}
          </div>
        ) : null}
        {current === "connections" ? <ConnectionsSection /> : null}
        {current === "team" ? <TeamSection level={2} /> : null}
        {current === "reminders" ? <RemindersSection /> : null}
        {current === "automation" ? <AutomationSection /> : null}
        {current === "external-agents" ? <LaterSection id={current} /> : null}
        {current === "procedures" ? <ProceduresSection /> : null}
        {current === "knowledge" ? <KnowledgeSection /> : null}
        {current === "recovery" ? <RecoverySection /> : null}
        {current === "usage" ? <UsageSection /> : null}
      </section>
    </div>
  );
}
