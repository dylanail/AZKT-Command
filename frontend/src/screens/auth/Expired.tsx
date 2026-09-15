/* Session ended (401 while signed in). Offers re-login in place; the current page is kept. */
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { Button, GlassPanel } from "../../ui";

export default function Expired() {
  const { login, busy, error, logout } = useAuth();
  const nav = useNavigate();
  return (
    <div className="auth">
      <GlassPanel radius="xl" tone="side" className="auth__card">
        <div className="auth__logo"><img src="/gen/azkt-logo.png" alt="Arizona Kei Trucks" width={180} height={46} /></div>
        <div className="auth__title">Your session ended</div>
        <div className="auth__sub">Unlock again to pick up where you were. Nothing was lost.</div>
        <Button variant="primary" size="xl" block loading={busy} onClick={() => login().catch(() => undefined)}>Unlock with Face ID</Button>
        {error ? <div className="error-state" role="alert"><span className="error-state__msg">{error}</span></div> : null}
        <Button variant="ghost" onClick={() => { void logout().then(() => nav("/login", { replace: true })); }}>Sign in as someone else</Button>
      </GlassPanel>
    </div>
  );
}
