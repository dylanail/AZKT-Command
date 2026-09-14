/* Sign-in: "Unlock with Face ID" (passkey). First run (no passkey registered yet) shows the SETUP-token step.
   Endpoints: GET /auth/state, POST /auth/login/options → startAuthentication → POST /auth/login/verify,
   POST /auth/register/options {setup_token, handle:"owner", label} → startRegistration → POST /auth/register/verify. */
import { useEffect, useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { platformAuthenticatorIsAvailable } from "@simplewebauthn/browser";
import { useAuth } from "../../lib/auth";
import { Button, Field, GlassPanel, Input } from "../../ui";

export default function Login() {
  const { registered, login, registerWithSetupToken, busy, error, clearError, ready, probeFailed, refresh } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const [setupToken, setSetupToken] = useState("");
  const [label, setLabel] = useState("");
  const [platform, setPlatform] = useState<boolean | null>(null);
  const from = (loc.state as { from?: string } | null)?.from;

  useEffect(() => { platformAuthenticatorIsAvailable().then(setPlatform).catch(() => setPlatform(false)); }, []);

  const unlock = async () => {
    try { await login(); nav(from || "/", { replace: true }); } catch { /* error shown from context */ }
  };
  const setup = async (e: FormEvent) => {
    e.preventDefault();
    if (!setupToken.trim()) return;
    try { await registerWithSetupToken(setupToken.trim(), label.trim() || undefined); nav("/", { replace: true }); } catch { /* shown */ }
  };
  const unlockLabel = platform === false ? "Unlock with a passkey" : "Unlock with Face ID";

  return (
    <div className="auth">
      <GlassPanel radius="xl" tone="side" className="auth__card">
        <div className="auth__logo"><img src="/gen/azkt-logo.png" alt="Arizona Kei Trucks" width={180} height={46} /></div>
        {!ready ? (
          <div className="auth__sub">Checking this device…</div>
        ) : probeFailed ? (
          <>
            <div className="auth__title">Can't reach AZKT</div>
            <div className="auth__sub">The server didn't answer. Check the connection, then try again.</div>
            <Button variant="glass" size="xl" block onClick={() => { void refresh(); }}>Try again</Button>
          </>
        ) : registered ? (
          <>
            <div className="auth__title">Welcome back</div>
            <div className="auth__sub">No passwords. Your device confirms it's you.</div>
            <Button variant="primary" size="xl" block loading={busy} onClick={unlock}>{unlockLabel}</Button>
            {error ? <div className="error-state" role="alert"><span className="error-state__msg">{error}</span><div><Button size="sm" variant="soft" onClick={() => { clearError(); void unlock(); }}>Try again</Button></div></div> : null}
            <div className="auth__foot">Invited to AZKT? Open the link you were sent — it registers this device.</div>
          </>
        ) : (
          <form className="stack" onSubmit={setup}>
            <div className="auth__title">Set up the owner passkey</div>
            <div className="auth__sub">First run. Paste the SETUP token from the server, then your device creates a passkey.</div>
            <Field label="Setup token" required>
              <Input value={setupToken} onChange={(e) => setSetupToken(e.target.value)} autoComplete="one-time-code" placeholder="SETUP_TOKEN from .env" autoFocus />
            </Field>
            <Field label="Name this device" hint="e.g. Dylan's iPhone">
              <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="passkey" />
            </Field>
            <Button type="submit" variant="primary" size="xl" block loading={busy} disabled={!setupToken.trim()} disabledReason="Paste the setup token first.">Create passkey</Button>
            {error ? <div className="error-state" role="alert"><span className="error-state__msg">{error}</span></div> : null}
          </form>
        )}
      </GlassPanel>
    </div>
  );
}
