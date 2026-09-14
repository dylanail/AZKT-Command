/* Settings sections: connections, team, reminders, automation, external agents, procedures, knowledge, recovery, usage.
   Owner edits; manager reads (People lives under team). Appearance (dark / reduced transparency) is also here.
   TODO(screen builder): GET/PATCH /api/settings ; GET /api/connections ; POST /api/connections/{id}/reconnect ;
   GET /api/settings/external-agents ; GET /api/procedures ; GET /api/knowledge ; GET /auth/credentials (recovery) ;
   GET /api/usage. */
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useAuth, useCan } from "../../lib/auth";
import { useTheme } from "../../lib/theme";
import { useIsMobile } from "../../lib/viewport";
import { Button, EmptyState, GlassPanel, PageHeader, Switch, Tabs, useToast } from "../../ui";
import { useProbe } from "../scaffold";

const SECTIONS = [
  { id: "connections", label: "Connections" },
  { id: "team", label: "Team" },
  { id: "reminders", label: "Reminders" },
  { id: "automation", label: "Automation" },
  { id: "external-agents", label: "External agents" },
  { id: "procedures", label: "Procedures" },
  { id: "knowledge", label: "Knowledge" },
  { id: "recovery", label: "Recovery" },
  { id: "usage", label: "Usage" },
] as const;
type SectionId = (typeof SECTIONS)[number]["id"];

export default function Settings() {
  const { section } = useParams();
  const nav = useNavigate();
  const { user, addPasskey, busy } = useAuth();
  const can = useCan();
  const { theme, toggleTheme, glass, toggleGlass, motion, setMotion } = useTheme();
  const isMobile = useIsMobile();
  const { toast } = useToast();
  const current: SectionId = (SECTIONS.find((s) => s.id === section)?.id) || "connections";
  const [label, setLabel] = useState("");
  const probe = useProbe(current === "recovery" ? "/auth/credentials" : `/api/settings/${current}`);
  const tabs = SECTIONS.filter((s) => s.id !== "team").map((s) => ({ id: s.id, label: s.label }));

  return (
    <div className="page">
      <PageHeader title="Settings" subtitle={can("settings") ? "Only the owner changes these." : "Read-only for your role."} actions={<Button variant="glass" to="/settings/team">{user?.role === "owner" ? "Team" : "People"}</Button>}>
        <Tabs<SectionId> label="Settings sections" value={current} onChange={(id) => nav(`/settings/${id}`)} tabs={tabs} />
      </PageHeader>

      <GlassPanel padded>
        <div className="stack-sm">
          <div className="eyebrow">Appearance</div>
          <Switch checked={theme === "dark"} onChange={toggleTheme} label="Dark mode" meta="Persists on this device" />
          <Switch checked={glass === "off"} onChange={toggleGlass} label="Reduce transparency" meta="Opaque panels, no blur" />
          <Switch checked={motion === "reduced"} onChange={(on) => setMotion(on ? "reduced" : "system")} label="Reduce motion" meta="Also follows your system setting" />
        </div>
      </GlassPanel>

      <GlassPanel clip>
        {current === "recovery" ? (
          <div className="stack" style={{ padding: 16 }}>
            <div className="eyebrow">Passkeys on this account</div>
            {probe.data === null ? <EmptyState align="left" title="Passkeys will list here" body="GET /auth/credentials is not answering yet." /> : <EmptyState align="left" title={`${Array.isArray(probe.data) ? probe.data.length : 0} passkeys`} body="Rename or revoke from the list once the builder renders it." />}
            <div className="row-wrap">
              <input className="input" style={{ maxWidth: 240 }} placeholder="Device name (e.g. iPhone)" value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Passkey label" />
              <Button variant="primary" loading={busy} onClick={() => addPasskey(label || undefined).then(() => { toast({ message: "Passkey added.", tone: "ok" }); setLabel(""); probe.reload(); }).catch((e: Error) => toast({ message: e.message, tone: "risk" }))}>Add a passkey to this device</Button>
            </div>
            {!isMobile ? <span className="fs12 t4">Add one on each device you unlock from. Recovery codes arrive with the backend.</span> : null}
          </div>
        ) : (
          <EmptyState align="left" title={`${SECTIONS.find((s) => s.id === current)?.label} not recorded yet`} body={<span>This section fills in when <code>/api/settings/{current}</code> answers. <Link to="/settings/team">{user?.role === "owner" ? "Team" : "People"}</Link> is ready now.</span>} />
        )}
      </GlassPanel>
    </div>
  );
}
