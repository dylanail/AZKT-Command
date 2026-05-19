import { useState } from "react";
import { useAuth } from "../auth";

export default function Login() {
  const { registered, login, register } = useAuth();
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const go = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr("");
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="center">
      <div style={{ fontSize: 40, fontWeight: 800 }}>AZKT</div>
      <div className="muted">Arizona Kei Trucks · Command</div>

      {registered ? (
        <button className="btn-green" disabled={busy} onClick={() => go(login)}>
          Unlock with Face ID
        </button>
      ) : (
        <div className="card" style={{ width: "90%", maxWidth: 400 }}>
          <h3>First-time setup</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            Enter the one-time setup token from your <code>.env</code> to claim
            the account with a passkey.
          </p>
          <input
            placeholder="SETUP_TOKEN"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <button
            className="btn-green"
            style={{ marginTop: 12, width: "100%" }}
            disabled={busy || !token}
            onClick={() => go(() => register(token))}
          >
            Register this device
          </button>
        </div>
      )}
      {err && <div className="pill-warn">{err}</div>}
    </div>
  );
}
