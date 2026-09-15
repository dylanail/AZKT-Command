/* Invite acceptance: GET /auth/invitation/{token} → preview {display_name, role, expires_at};
   then POST /auth/register/options {invite_token, label} → startRegistration → POST /auth/register/verify. */
import { useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useAuth, type InvitationPreview } from "../../lib/auth";
import { ApiError, api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { ROLE_LABELS, isEmployeeRole, type Role } from "../../lib/perms";
import { Button, Field, GlassPanel, Input, Loading, When } from "../../ui";

export default function Invite() {
  const { token = "" } = useParams();
  const nav = useNavigate();
  const { registerWithInvite, busy, error, authed } = useAuth();
  const [label, setLabel] = useState("");
  const q = useQuery<InvitationPreview>((signal) => api.get<InvitationPreview>(`/auth/invitation/${encodeURIComponent(token)}`, { signal }), [token]);

  const accept = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await registerWithInvite(token, label.trim() || undefined);
      nav(isEmployeeRole(q.data?.role) ? "/tasks" : "/", { replace: true });
    } catch { /* shown */ }
  };

  const gone = q.error instanceof ApiError && (q.error.status === 404 || q.error.status === 403);

  return (
    <div className="auth">
      <GlassPanel radius="xl" tone="side" className="auth__card">
        <div className="auth__logo"><img src="/gen/azkt-logo.png" alt="Arizona Kei Trucks" width={180} height={46} /></div>
        {q.loading ? <Loading label="Checking invitation" rows={2} /> : gone || (q.error && !q.data) ? (
          <>
            <div className="auth__title">This invitation isn't valid</div>
            <div className="auth__sub">{gone ? "It may have expired or already been used. Ask the owner for a new link." : "Couldn't check the invitation. Try again in a moment."}</div>
            {!gone ? <Button variant="soft" onClick={q.reload}>Try again</Button> : null}
            <Button variant="ghost" to="/login">Go to sign-in</Button>
          </>
        ) : q.data ? (
          <form className="stack" onSubmit={accept}>
            <div className="auth__title">Join AZKT as {q.data.display_name}</div>
            <div className="auth__sub">{ROLE_LABELS[q.data.role as Role] || q.data.role} · link valid until <When iso={q.data.expires_at} format="long" /></div>
            {authed ? <div className="notice notice--risk"><span><span className="notice__lead">Signed in already</span> · sign out first to accept this invitation on this device.</span></div> : null}
            <Field label="Name this device" hint="e.g. Marco's phone">
              <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="passkey" autoFocus />
            </Field>
            <Button type="submit" variant="primary" size="xl" block loading={busy} disabled={authed} disabledReason="Sign out first.">Create passkey and join</Button>
            {error ? <div className="error-state" role="alert"><span className="error-state__msg">{error}</span></div> : null}
            <div className="auth__foot">Your device confirms it's you with Face ID, Touch ID or a PIN. No password.</div>
          </form>
        ) : null}
      </GlassPanel>
    </div>
  );
}
