import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { startAuthentication, startRegistration } from "@simplewebauthn/browser";
import type { PublicKeyCredentialCreationOptionsJSON, PublicKeyCredentialRequestOptionsJSON } from "@simplewebauthn/browser";
import { ApiError, api, onUnauthorized } from "./api";
import type { Role, Scope } from "./perms";

/* Shapes from backend/app/auth/passkey.py (provisional while the backend is being written). */
export interface AuthUser {
  id: string;
  handle: string;
  display_name: string;
  role: Role;
  scope: Scope;
  email?: string | null;
  timezone?: string | null;
  perms: Record<string, boolean>;
  status?: "active" | "invited" | "disabled" | string;
}
export interface AuthStateResponse {
  registered: boolean;
  authed: boolean;
  user: AuthUser | null;
}
export interface InvitationPreview {
  display_name: string;
  role: Role;
  expires_at: string;
}
/** What a second device is told before it is asked for a passkey. */
export interface DeviceLinkPreview {
  display_name: string;
  label: string;
  expires_at: string;
}
export interface DeviceEnrollment {
  id: string;
  label: string;
  status: "pending" | "used" | "revoked" | "expired" | string;
  created_at: string | null;
  expires_at: string | null;
  used_at: string | null;
}
/** POST /auth/device-link — the token is handed over once and never stored. */
export interface DeviceLink {
  enrollment: DeviceEnrollment;
  token: string;
  url: string;
  path: string;
  qr: { rows: string[] } | null;
  replaced: string[];
  expires_in_minutes: number;
}

export interface AuthContextValue {
  /** First /auth/state probe finished. */
  ready: boolean;
  /** At least one passkey exists (false → first-run setup). */
  registered: boolean;
  authed: boolean;
  /** A 401 arrived while we believed we were signed in. */
  expired: boolean;
  /** GET /auth/state could not be reached (backend down / offline). */
  probeFailed: boolean;
  user: AuthUser | null;
  /** Last auth error, plain language. */
  error: string | null;
  busy: boolean;
  refresh: () => Promise<AuthStateResponse | null>;
  login: () => Promise<void>;
  registerWithSetupToken: (setupToken: string, label?: string) => Promise<void>;
  registerWithInvite: (inviteToken: string, label?: string) => Promise<void>;
  previewInvitation: (token: string) => Promise<InvitationPreview>;
  addPasskey: (label?: string) => Promise<void>;
  /** On a second device, with the one-time link from the dash. Signs that device in. */
  registerWithDeviceLink: (deviceToken: string, label?: string) => Promise<void>;
  previewDeviceLink: (deviceToken: string) => Promise<DeviceLinkPreview>;
  logout: () => Promise<void>;
  clearError: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function explainWebAuthn(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return e.message || "That link or token isn't valid any more.";
    if (e.status === 0) return e.message;
    return e.message || "Sign-in failed.";
  }
  if (e && typeof e === "object" && "name" in e) {
    const name = String((e as { name: unknown }).name);
    if (name === "NotAllowedError") return "Cancelled, or no passkey was offered. Try again.";
    if (name === "InvalidStateError") return "This device already has a passkey for AZKT.";
    if (name === "NotSupportedError") return "This browser can't use passkeys.";
    if (name === "SecurityError") return "Passkeys need a secure (https) address that matches AZKT's domain.";
    if (name === "AbortError") return "Cancelled.";
  }
  if (e instanceof Error) return e.message;
  return "Something went wrong.";
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [registered, setRegistered] = useState(false);
  const [authed, setAuthed] = useState(false);
  const [expired, setExpired] = useState(false);
  const [probeFailed, setProbeFailed] = useState(false);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const authedRef = useRef(false);
  authedRef.current = authed;

  const refresh = useCallback(async (): Promise<AuthStateResponse | null> => {
    try {
      const s = await api.get<AuthStateResponse>("/auth/state");
      setProbeFailed(false);
      setRegistered(!!s.registered);
      setAuthed(!!s.authed);
      setUser(s.authed ? s.user : null);
      if (s.authed) setExpired(false);
      return s;
    } catch (e) {
      // Network or backend down: keep whatever we had, flag it, and stop the splash.
      if (!(e instanceof ApiError) || e.status !== 401) setProbeFailed(true);
      return null;
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  // A 401 anywhere in the app while signed in means the session ended.
  useEffect(() => onUnauthorized((path) => {
    if (path.startsWith("/auth/")) return;
    if (authedRef.current) {
      setExpired(true);
      setAuthed(false);
    }
  }), []);

  const run = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      const msg = explainWebAuthn(e);
      setError(msg);
      throw new Error(msg);
    } finally {
      setBusy(false);
    }
  }, []);

  const login = useCallback(() => run(async () => {
    const options = await api.post<PublicKeyCredentialRequestOptionsJSON>("/auth/login/options");
    const assertion = await startAuthentication({ optionsJSON: options });
    await api.post("/auth/login/verify", assertion);
    await refresh();
  }), [run, refresh]);

  const registerWith = useCallback((body: Record<string, unknown>) => run(async () => {
    const options = await api.post<PublicKeyCredentialCreationOptionsJSON>("/auth/register/options", body);
    const attestation = await startRegistration({ optionsJSON: options });
    await api.post("/auth/register/verify", attestation);
    await refresh();
  }), [run, refresh]);

  const registerWithSetupToken = useCallback((setupToken: string, label?: string) =>
    registerWith({ setup_token: setupToken, handle: "owner", label: label || "passkey" }), [registerWith]);

  const registerWithInvite = useCallback((inviteToken: string, label?: string) =>
    registerWith({ invite_token: inviteToken, label: label || "passkey" }), [registerWith]);

  const previewInvitation = useCallback((token: string) =>
    api.get<InvitationPreview>(`/auth/invitation/${encodeURIComponent(token)}`), []);

  // A passkey cannot be copied from the desktop, so this device registers its own against the
  // account the link names. The server only accepts the link once, and only before it expires.
  const registerWithDeviceLink = useCallback((deviceToken: string, label?: string) =>
    registerWith({ device_token: deviceToken, label }), [registerWith]);

  const previewDeviceLink = useCallback((deviceToken: string) =>
    api.get<DeviceLinkPreview>(`/auth/device-link/${encodeURIComponent(deviceToken)}`), []);

  // Signed in already: the server gates on the session cookie, no token is sent.
  const addPasskey = useCallback((label?: string) => run(async () => {
    const options = await api.post<PublicKeyCredentialCreationOptionsJSON>("/auth/register/options", { label: label || "passkey" });
    const attestation = await startRegistration({ optionsJSON: options });
    await api.post("/auth/register/verify", attestation);
  }), [run]);

  const logout = useCallback(async () => {
    try { await api.post("/auth/logout"); } catch { /* cookie may already be gone */ }
    setAuthed(false);
    setUser(null);
    setExpired(false);
  }, []);

  const clearError = useCallback(() => setError(null), []);

  const value = useMemo<AuthContextValue>(() => ({
    ready, registered, authed, expired, probeFailed, user, error, busy,
    refresh, login, registerWithSetupToken, registerWithInvite, previewInvitation, addPasskey,
    registerWithDeviceLink, previewDeviceLink, logout, clearError,
  }), [ready, registered, authed, expired, probeFailed, user, error, busy, refresh, login, registerWithSetupToken, registerWithInvite, previewInvitation, addPasskey, registerWithDeviceLink, previewDeviceLink, logout, clearError]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

/** Convenience: permission check bound to the signed-in user. */
export function useCan(): (perm: string) => boolean {
  const { user } = useAuth();
  return useCallback((perm: string) => {
    if (!user) return false;
    if (user.role === "owner") return true;
    return !!user.perms?.[perm];
  }, [user]);
}
