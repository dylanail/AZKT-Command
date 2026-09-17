/* Adding this device to an account that already exists — opened from the QR or link the dash issued.
   GET /auth/device-link/{token} → preview {display_name, label, expires_at}; then
   POST /auth/register/options {device_token, label} → startRegistration → POST /auth/register/verify,
   which registers this device's own passkey and signs it in. A passkey never leaves the device that
   made it, so this is how a phone joins a desktop's account. */
import { useEffect, useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { platformAuthenticatorIsAvailable } from "@simplewebauthn/browser";
import { useAuth, type DeviceLinkPreview } from "../../lib/auth";
import { ApiError, api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { Button, Field, GlassPanel, Input, Loading, When } from "../../ui";

export default function AddDevice() {
  const { token = "" } = useParams();
  const nav = useNavigate();
  const { registerWithDeviceLink, busy, error } = useAuth();
  const [label, setLabel] = useState("");
  const [platform, setPlatform] = useState<boolean | null>(null);
  const q = useQuery<DeviceLinkPreview>((signal) => api.get<DeviceLinkPreview>(`/auth/device-link/${encodeURIComponent(token)}`, { signal }), [token]);

  useEffect(() => { platformAuthenticatorIsAvailable().then(setPlatform).catch(() => setPlatform(false)); }, []);
  useEffect(() => { if (q.data && !label) setLabel(q.data.label || ""); }, [q.data]);  // eslint-disable-line react-hooks/exhaustive-deps

  const add = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await registerWithDeviceLink(token, label.trim() || undefined);
      nav("/", { replace: true });  // RoleHome sends employees on to their tasks
    } catch { /* shown from context */ }
  };

  const gone = q.error instanceof ApiError && (q.error.status === 404 || q.error.status === 403);

  return (
    <div className="auth">
      <GlassPanel radius="xl" tone="side" className="auth__card">
        <div className="auth__logo"><img src="/gen/azkt-logo.png" alt="Arizona Kei Trucks" width={180} height={46} /></div>
        {q.loading ? <Loading label="Checking the link" rows={2} /> : gone || (q.error && !q.data) ? (
          <>
            <div className="auth__title">This link isn't valid any more</div>
            <div className="auth__sub">{gone ? "Add-a-device links last minutes and work once. Open Settings › Recovery on the device you're already signed in on and make a new one." : "Couldn't check the link. Try again in a moment."}</div>
            {!gone ? <Button variant="soft" onClick={q.reload}>Try again</Button> : null}
            <Button variant="ghost" to="/login">Go to sign-in</Button>
          </>
        ) : q.data ? (
          <form className="stack" onSubmit={add}>
            <div className="auth__title">Add this device</div>
            <div className="auth__sub">A new passkey for {q.data.display_name}, on this device only · link valid until <When iso={q.data.expires_at} format="time" /></div>
            <Field label="Name this device" hint="So you can tell it apart later — e.g. iPhone">
              <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="passkey" autoFocus />
            </Field>
            <Button type="submit" variant="primary" size="xl" block loading={busy}>
              {platform === false ? "Create a passkey here" : "Create passkey with Face ID"}
            </Button>
            {error ? <div className="error-state" role="alert"><span className="error-state__msg">{error}</span></div> : null}
            <div className="auth__foot">Your other devices keep working. Each one has its own passkey; none of them can be copied.</div>
          </form>
        ) : null}
      </GlassPanel>
    </div>
  );
}
