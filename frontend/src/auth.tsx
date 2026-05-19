import { createContext, useContext, useEffect, useState } from "react";
import {
  startRegistration,
  startAuthentication,
} from "@simplewebauthn/browser";

type AuthState = {
  ready: boolean;
  authed: boolean;
  registered: boolean;
  login: () => Promise<void>;
  register: (setupToken: string) => Promise<void>;
  addPasskey: () => Promise<void>;
  logout: () => Promise<void>;
};

const Ctx = createContext<AuthState>(null as never);
export const useAuth = () => useContext(Ctx);

async function jp(path: string, body?: unknown) {
  const r = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
  return r.json();
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [authed, setAuthed] = useState(false);
  const [registered, setRegistered] = useState(false);

  async function probe() {
    try {
      const s = await fetch("/auth/state", { credentials: "include" }).then((r) => r.json());
      setRegistered(s.registered);
      // A protected route returning non-401 means we have a live session.
      const a = await fetch("/api/agents", { credentials: "include" });
      setAuthed(a.status !== 401);
    } catch {
      setAuthed(false);
    } finally {
      setReady(true);
    }
  }
  useEffect(() => {
    probe();
  }, []);

  const register = async (setupToken: string) => {
    const opts = await jp("/auth/register/options", {
      handle: "owner",
      setup_token: setupToken,
    });
    const att = await startRegistration(opts);
    await jp("/auth/register/verify", att);
    await probe();
  };

  // Already authenticated: the server gates this on the session cookie,
  // not the setup token, so no token is sent.
  const addPasskey = async () => {
    const opts = await jp("/auth/register/options", { handle: "owner" });
    const att = await startRegistration(opts);
    await jp("/auth/register/verify", att);
  };

  const login = async () => {
    const opts = await jp("/auth/login/options");
    const asr = await startAuthentication(opts);
    await jp("/auth/login/verify", asr);
    await probe();
  };

  const logout = async () => {
    await jp("/auth/logout");
    setAuthed(false);
  };

  return (
    <Ctx.Provider value={{ ready, authed, registered, login, register, addPasskey, logout }}>
      {children}
    </Ctx.Provider>
  );
}
