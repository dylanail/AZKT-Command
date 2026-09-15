/* Follows one run: GET /api/runs/{id} for the current state, then GET /api/runs/{id}/events?cursor= for
   progress. The server closes a long stream on purpose ("idle") — that is a reconnect, not a failure, and
   the mission keeps running either way. Reconnects resume from the last seq the stream reported. */
import { useCallback, useEffect, useRef, useState } from "react";
import { describeError } from "../../lib/api";
import { getRun, streamRunEvents } from "./api";
import type { MissionBrief, MissionUpdate, RunBrief } from "./types";
import { RESTING_RUN, TERMINAL_MISSION, TERMINAL_RUN } from "./types";

export type RunConnection = "idle" | "connecting" | "live" | "reconnecting" | "finished" | "lost";

export interface RunFollowState {
  mission: MissionBrief | null;
  run: RunBrief | null;
  updates: MissionUpdate[];
  cursor: number;
  connection: RunConnection;
  /** Plain-language note for "lost"; never shown as a red error while work may still be running. */
  note: string | null;
  retry: () => void;
}

const BACKOFF_MS = [1000, 2000, 4000, 8000];

export function useRunEvents(runId: string | null | undefined, startCursor = 0): RunFollowState {
  const [mission, setMission] = useState<MissionBrief | null>(null);
  const [run, setRun] = useState<RunBrief | null>(null);
  const [updates, setUpdates] = useState<MissionUpdate[]>([]);
  const [connection, setConnection] = useState<RunConnection>("idle");
  const [note, setNote] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const cursorRef = useRef(startCursor);

  const retry = useCallback(() => { setNote(null); setAttempt((a) => a + 1); }, []);

  useEffect(() => { cursorRef.current = startCursor; setUpdates([]); setMission(null); setRun(null); }, [runId, startCursor]);

  useEffect(() => {
    if (!runId) { setConnection("idle"); return; }
    const ctrl = new AbortController();
    let stopped = false;
    let failures = 0;

    const addUpdate = (u: MissionUpdate) => {
      if (!u || typeof u.seq !== "number") return;
      cursorRef.current = Math.max(cursorRef.current, u.seq);
      setUpdates((prev) => (prev.some((p) => p.seq === u.seq) ? prev : [...prev, u].sort((a, b) => a.seq - b.seq)));
    };

    const settled = (r: RunBrief | null, m: MissionBrief | null) =>
      (!!r && (TERMINAL_RUN.has(r.status) || RESTING_RUN.has(r.status))) || (!!m && TERMINAL_MISSION.has(m.status));

    const loop = async () => {
      setConnection("connecting");
      try {
        const snap = await getRun(runId, ctrl.signal);
        if (stopped) return;
        setRun(snap.run);
        setMission(snap.mission);
        if (settled(snap.run, snap.mission)) { setConnection("finished"); return; }
      } catch (e) {
        if (stopped) return;
        setNote(describeError(e));
        setConnection("lost");
        return;
      }

      for (;;) {
        if (stopped) return;
        try {
          setConnection("live");
          setNote(null);
          const done: Array<{ mission?: MissionBrief; run?: RunBrief | null; cursor?: number }> = [];
          await streamRunEvents(runId, cursorRef.current, {
            onOpen: (c) => { cursorRef.current = Math.max(cursorRef.current, c); },
            onUpdate: addUpdate,
            onIdle: (c) => { cursorRef.current = Math.max(cursorRef.current, c); },
            onDone: (d) => { done.push(d); },
            onError: (msg) => { setNote(msg); },
          }, ctrl.signal);
          if (stopped) return;
          failures = 0;
          const last = done[done.length - 1];
          if (last) {
            if (last.mission) setMission(last.mission);
            if (last.run) setRun(last.run);
            if (typeof last.cursor === "number") cursorRef.current = Math.max(cursorRef.current, last.cursor);
            setConnection("finished");
            return;
          }
          // No "done": the server closed an idle stream. Reconnect from the cursor straight away.
          setConnection("reconnecting");
        } catch (e) {
          if (stopped || (e instanceof DOMException && e.name === "AbortError")) return;
          failures += 1;
          setConnection("reconnecting");
          if (failures > BACKOFF_MS.length) {
            setNote(`${describeError(e)} The work itself keeps running — reconnect when you are ready.`);
            setConnection("lost");
            return;
          }
          const wait = BACKOFF_MS[Math.min(failures - 1, BACKOFF_MS.length - 1)];
          await new Promise<void>((res) => { window.setTimeout(res, wait); });
        }
      }
    };

    void loop();
    return () => { stopped = true; ctrl.abort(); };
  }, [runId, attempt]);

  return { mission, run, updates, cursor: cursorRef.current, connection, note, retry };
}
