/* People for owner pickers. GET /api/team (owner: everyone; manager: reports; others: 403 → hidden).
   Cached for a minute across screens so task rows can name owners without a request each. */
import { useEffect, useState, useCallback } from "react";
import { ApiError, api } from "../../lib/api";
import { useAuth } from "../../lib/auth";

export interface Person {
  id: string;
  display_name: string;
  handle?: string;
  role: string;
  status?: string;
  timezone?: string | null;
}
interface TeamResp { items?: Person[]; }

interface Cache { at: number; people: Person[]; denied: boolean; }
let cache: Cache | null = null;
let inflight: Promise<Cache> | null = null;
const TTL = 60_000;

async function load(): Promise<Cache> {
  if (cache && Date.now() - cache.at < TTL) return cache;
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const r = await api.get<TeamResp | null>("/api/team", { tolerate: [403, 404, 501] });
      const people = (r?.items || []).filter((p) => p && typeof p.id === "string").map((p) => ({ ...p, display_name: p.display_name || p.handle || p.id }));
      cache = { at: Date.now(), people, denied: r === null };
    } catch (e) {
      cache = { at: Date.now(), people: [], denied: e instanceof ApiError && e.isDenied };
    } finally {
      inflight = null;
    }
    return cache as Cache;
  })();
  return inflight;
}

export function usePeople(enabled = true) {
  const { user } = useAuth();
  const [state, setState] = useState<Cache>(() => cache || { at: 0, people: [], denied: false });
  const [loading, setLoading] = useState(enabled && !cache);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    setLoading(!cache);
    void load().then((c) => { if (alive) { setState(c); setLoading(false); } });
    return () => { alive = false; };
  }, [enabled]);
  const nameOf = useCallback((id: string | null | undefined): string => {
    if (!id) return "Unassigned";
    if (user && id === user.id) return "You";
    const p = state.people.find((x) => x.id === id);
    return p ? p.display_name : "Assigned";
  }, [state.people, user]);
  const active = state.people.filter((p) => !p.status || p.status === "active");
  return { people: active, denied: state.denied, loading, nameOf };
}
