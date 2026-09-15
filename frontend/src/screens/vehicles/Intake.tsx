/* Photo and voice intake through Manager (spec §7.4; acceptance I01–I07).
   Routes: /vehicles/intake (book in a vehicle) and /vehicles/:id/intake (add photos / update an existing card).

   The composer keeps one durable intake session: target (New vehicle | Existing vehicle | Find the vehicle)
   sits beside the composer at all times; photos upload as they are added and say per file whether they are
   saved to AZKT or only on this device; a voice note is transcribed live where the browser allows it and can
   always be typed instead; Analyze proposes bullets and verb-first work; Apply writes the card and returns a
   compact result with Edit, Add more, Assign and Undo. A partly-applied intake never reads as complete. */
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api, command, describeError, newIdempotencyKey } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can } from "../../lib/perms";
import { useIsMobile } from "../../lib/viewport";
import {
  Button, Chip, EmptyState, Field, GlassPanel, Input, Loading, Notice, PageHeader, ResponsiveDialog,
  SegmentedControl, Select, Textarea, useToast,
} from "../../ui";
import { usePeople } from "../tasks/usePeople";
import { fetchVehicleOptions, vehicleLabel, type VehicleOption } from "../tasks/useNames";
import { VehicleThumb } from "./components/HealthRow";
import { UploadsUnavailable, dataUrlToBlob, humanSize, readAsDataUrl, uploadOne } from "./uploads";
import {
  INTAKE_STATUS_LABELS, type IntakeApplyResult, type IntakeCandidate, type IntakeObservation, type IntakeStatusResp,
} from "./types";
import "./vehicles.css";
import "../tasks/tasks.css";

/* ---------- local draft ("Saved on device, not in AZKT") ---------- */
interface DraftFile { key: string; name: string; type: string; size: number; dataUrl?: string; assetId?: string }
interface Draft {
  requestKey: string;
  intakeId?: string;
  targetMode: TargetMode;
  vehicleId?: string | null;
  text: string;
  transcript: string;
  files: DraftFile[];
  savedAt: string;
}
const draftKey = (scope: string) => `azkt-intake-draft:${scope}`;
function readDraft(scope: string): Draft | null {
  try { const raw = localStorage.getItem(draftKey(scope)); return raw ? (JSON.parse(raw) as Draft) : null; } catch { return null; }
}
function writeDraft(scope: string, d: Draft | null): boolean {
  try {
    if (!d) { localStorage.removeItem(draftKey(scope)); return true; }
    localStorage.setItem(draftKey(scope), JSON.stringify(d));
    return true;
  } catch {
    if (d) {
      // Quota or private mode: keep the words, drop the image data.
      try { localStorage.setItem(draftKey(scope), JSON.stringify({ ...d, files: d.files.map((f) => ({ ...f, dataUrl: undefined })) })); } catch { /* nothing more we can do */ }
    }
    return false;
  }
}

/* ---------- speech (optional) ---------- */
interface SpeechResultLike { 0: { transcript: string }; isFinal: boolean }
interface SpeechEventLike { resultIndex: number; results: { length: number; [i: number]: SpeechResultLike } }
interface RecognitionLike {
  continuous: boolean; interimResults: boolean; lang: string;
  start(): void; stop(): void;
  onresult: ((e: SpeechEventLike) => void) | null;
  onerror: ((e: unknown) => void) | null;
  onend: (() => void) | null;
}
function recognitionCtor(): (new () => RecognitionLike) | null {
  const w = window as unknown as { SpeechRecognition?: new () => RecognitionLike; webkitSpeechRecognition?: new () => RecognitionLike };
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}
function canRecord(): boolean {
  return typeof window !== "undefined" && typeof window.MediaRecorder !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

/* ---------- composer state ---------- */
type TargetMode = "new" | "existing" | "find";
type FileStatus = "waiting" | "uploading" | "saved" | "failed";
interface ShotFile {
  key: string;
  name: string;
  type: string;
  size: number;
  dataUrl?: string;
  file?: File;
  assetId?: string;
  uploadId?: string;
  status: FileStatus;
  progress: number;
  error?: string;
}

interface ProposedTask { observationId: string; title: string; detail: string; assignee: string; priority: string }

export default function Intake() {
  const { id: routeVehicleId } = useParams();
  const { user } = useAuth();
  const nav = useNavigate();
  const mobile = useIsMobile();
  const { toast } = useToast();
  const people = usePeople(can(user, "tasks.assign"));
  const scope = routeVehicleId || "new";

  const [draft] = useState<Draft | null>(() => readDraft(scope));
  const requestKey = useRef<string>(draft?.requestKey || newIdempotencyKey());

  const [targetMode, setTargetMode] = useState<TargetMode>(routeVehicleId ? "existing" : (draft?.targetMode || "find"));
  const [vehicleId, setVehicleId] = useState<string | null>(routeVehicleId || draft?.vehicleId || null);
  const [intakeId, setIntakeId] = useState<string | null>(draft?.intakeId || null);
  const [status, setStatus] = useState<IntakeStatusResp | null>(null);
  const [files, setFiles] = useState<ShotFile[]>(() => (draft?.files || []).map((f) => ({
    ...f, status: f.assetId ? "saved" : "waiting", progress: f.assetId ? 100 : 0,
  })));
  const [text, setText] = useState(draft?.text || "");
  const [transcript, setTranscript] = useState(draft?.transcript || "");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploadsOff, setUploadsOff] = useState(false);
  const [applied, setApplied] = useState<IntakeApplyResult | null>(null);
  const [appliedStatus, setAppliedStatus] = useState<string | null>(null);
  const [taskEdits, setTaskEdits] = useState<Record<string, { assignee: string; priority: string }>>({});
  const [assignOpen, setAssignOpen] = useState(false);
  const [vehicles, setVehicles] = useState<VehicleOption[] | null | undefined>(undefined);
  const [vehicleFilter, setVehicleFilter] = useState("");
  const [draftWarning, setDraftWarning] = useState(false);
  const captureRef = useRef<HTMLInputElement>(null);
  const galleryRef = useRef<HTMLInputElement>(null);

  const canIntake = can(user, "intake");
  const canCreateCard = can(user, "vehicles.write");

  /* ---------- draft persistence ---------- */
  const persist = useCallback((next: Partial<Draft> = {}) => {
    const d: Draft = {
      requestKey: requestKey.current,
      intakeId: intakeId || undefined,
      targetMode,
      vehicleId,
      text,
      transcript,
      files: files.map(({ key, name, type, size, dataUrl, assetId }) => ({ key, name, type, size, dataUrl, assetId })),
      savedAt: new Date().toISOString(),
      ...next,
    };
    const ok = writeDraft(scope, d);
    if (!ok) setDraftWarning(true);
  }, [files, intakeId, scope, targetMode, text, transcript, vehicleId]);

  // Only write a device draft once there is something to lose.
  useEffect(() => {
    if (files.length || text.trim() || transcript.trim() || intakeId) persist();
  }, [persist, files.length, text, transcript, intakeId]);

  /* ---------- vehicle picker ---------- */
  useEffect(() => {
    if (targetMode !== "existing" || vehicles !== undefined) return;
    void fetchVehicleOptions().then(setVehicles).catch(() => setVehicles(null));
  }, [targetMode, vehicles]);

  /* ---------- session ---------- */
  const refreshStatus = useCallback(async (iid: string) => {
    try {
      const s = await api.get<IntakeStatusResp | null>(`/api/vehicle-intakes/${encodeURIComponent(iid)}`, { tolerate: [404] });
      if (s) setStatus(s);
      return s;
    } catch (e) {
      setError(describeError(e));
      return null;
    }
  }, []);

  useEffect(() => {
    if (intakeId) void refreshStatus(intakeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const ensureIntake = useCallback(async (): Promise<string | null> => {
    if (intakeId) return intakeId;
    const body: Record<string, unknown> = {
      target_mode: targetMode,
      channel: "web",
      request_key: requestKey.current,
      device_draft: { files: files.length, has_text: !!text.trim(), has_transcript: !!transcript.trim() },
    };
    if (targetMode === "existing") {
      if (!vehicleId) { setError("Pick the vehicle this update is for."); return null; }
      body.vehicle_id = vehicleId;
    }
    try {
      const r = await command<{ intake?: { id: string } }>("/api/vehicle-intakes", body);
      if (r.status !== "ok") { setError(r.decision.reasons.join(" · ") || "The intake could not be started."); return null; }
      const iid = r.data?.intake?.id || null;
      if (!iid) { setError("The intake session came back without an id."); return null; }
      setIntakeId(iid);
      persist({ intakeId: iid });
      void refreshStatus(iid);
      return iid;
    } catch (e) {
      setError(describeError(e));
      return null;
    }
  }, [files.length, intakeId, persist, refreshStatus, targetMode, text, transcript, vehicleId]);

  /* ---------- photos ---------- */
  const uploadFiles = useCallback(async (items: ShotFile[]) => {
    const iid = await ensureIntake();
    if (!iid) {
      setFiles((xs) => xs.map((x) => (items.some((i) => i.key === x.key) ? { ...x, status: "failed", error: "No intake session yet." } : x)));
      return;
    }
    const savedIds: string[] = [];
    const failures: Array<{ upload_id?: string; name: string; error: string }> = [];
    for (const item of items) {
      setFiles((xs) => xs.map((x) => (x.key === item.key ? { ...x, status: "uploading", progress: 0, error: undefined } : x)));
      let uploadId: string | undefined;
      try {
        const blob = item.file ?? (item.dataUrl ? dataUrlToBlob(item.dataUrl) : null);
        if (!blob) throw new Error("The photo data is no longer on this device; add it again.");
        const carrier = new File([blob], item.name, { type: item.type });
        const assetId = await uploadOne(carrier, {
          purpose: "intake",
          intakeId: iid,
          onProgress: (pct) => setFiles((xs) => xs.map((x) => (x.key === item.key ? { ...x, progress: pct } : x))),
          onSlot: (uid) => { uploadId = uid; },
        });
        savedIds.push(assetId);
        setFiles((xs) => xs.map((x) => (x.key === item.key ? { ...x, assetId, uploadId, status: "saved", progress: 100 } : x)));
      } catch (e) {
        const off = e instanceof UploadsUnavailable;
        if (off) setUploadsOff(true);
        const msg = off ? "Uploads aren't available yet." : describeError(e);
        failures.push({ upload_id: uploadId, name: item.name, error: msg });
        setFiles((xs) => xs.map((x) => (x.key === item.key ? { ...x, uploadId, status: "failed", error: msg } : x)));
      }
    }
    if (savedIds.length || failures.length) {
      try {
        await command(`/api/vehicle-intakes/${encodeURIComponent(iid)}/continue`, {
          asset_ids: savedIds,
          failed: failures,
        });
        await refreshStatus(iid);
      } catch (e) {
        setError(describeError(e));
      }
    }
  }, [ensureIntake, refreshStatus]);

  const addFiles = async (e: ChangeEvent<HTMLInputElement>) => {
    const list = Array.from(e.target.files || []);
    e.target.value = "";
    if (!list.length) return;
    const next: ShotFile[] = [];
    for (const f of list) {
      next.push({
        key: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        name: f.name || "photo.jpg", type: f.type || "image/jpeg", size: f.size,
        dataUrl: await readAsDataUrl(f), file: f, status: "waiting", progress: 0,
      });
    }
    setFiles((xs) => [...xs, ...next]);
    void uploadFiles(next);
  };

  const removeFile = (key: string) => setFiles((xs) => xs.filter((x) => x.key !== key));
  const moveFile = (key: string, dir: -1 | 1) => setFiles((xs) => {
    const i = xs.findIndex((x) => x.key === key);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= xs.length) return xs;
    const copy = [...xs];
    [copy[i], copy[j]] = [copy[j], copy[i]];
    return copy;
  });
  const retryFile = (key: string) => {
    const f = files.find((x) => x.key === key);
    if (f) void uploadFiles([f]);
  };
  const retryAll = () => {
    const bad = files.filter((f) => f.status === "failed" || f.status === "waiting");
    if (bad.length) void uploadFiles(bad);
  };

  /* ---------- voice ---------- */
  const [recording, setRecording] = useState(false);
  const [liveText, setLiveText] = useState("");
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const recogRef = useRef<RecognitionLike | null>(null);
  const speechOk = !!recognitionCtor();
  const recordOk = canRecord();

  const stopRecording = useCallback(() => {
    try { recorderRef.current?.stop(); } catch { /* already stopped */ }
    try { recogRef.current?.stop(); } catch { /* already stopped */ }
    setRecording(false);
  }, []);

  const startRecording = useCallback(async () => {
    setError(null);
    const Ctor = recognitionCtor();
    if (Ctor) {
      try {
        const r = new Ctor();
        r.continuous = true;
        r.interimResults = true;
        r.lang = "en-US";
        r.onresult = (ev) => {
          let interim = "";
          let finalAdd = "";
          for (let i = ev.resultIndex; i < ev.results.length; i++) {
            const res = ev.results[i];
            if (res.isFinal) finalAdd += res[0].transcript;
            else interim += res[0].transcript;
          }
          if (finalAdd) setTranscript((t) => (t ? `${t} ${finalAdd.trim()}` : finalAdd.trim()));
          setLiveText(interim);
        };
        r.onerror = () => { /* the typed box stays available */ };
        r.onend = () => setLiveText("");
        recogRef.current = r;
        r.start();
      } catch { recogRef.current = null; }
    }
    if (!recordOk) { setRecording(true); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (ev) => { if (ev.data.size) chunksRef.current.push(ev.data); };
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || "audio/webm" });
        chunksRef.current = [];
        if (!blob.size) return;
        const iid = await ensureIntake();
        if (!iid) return;
        try {
          const carrier = new File([blob], "voice-note.webm", { type: blob.type || "audio/webm" });
          const assetId = await uploadOne(carrier, { purpose: "voice", intakeId: iid });
          await command(`/api/vehicle-intakes/${encodeURIComponent(iid)}/continue`, {
            audio_asset_id: assetId,
            transcript: transcript.trim() || undefined,
            source: speechOk ? "service" : "typed",
          });
          await refreshStatus(iid);
          toast({ message: transcript.trim() ? "Voice note saved with its transcript." : "Voice note saved. Type what you said so it can be used.", tone: transcript.trim() ? "ok" : "risk" });
        } catch (e) {
          toast({ message: `The recording didn't reach AZKT: ${describeError(e)} Type what you said instead.`, tone: "risk" });
        }
      };
      recorderRef.current = rec;
      rec.start();
      setRecording(true);
    } catch {
      setRecording(false);
      toast({ message: "Microphone permission was declined. Type what you said instead.", tone: "risk" });
    }
  }, [ensureIntake, recordOk, refreshStatus, speechOk, toast, transcript]);

  useEffect(() => () => { try { recorderRef.current?.stop(); recogRef.current?.stop(); } catch { /* unmounting */ } }, []);

  /* ---------- analyze / choose / apply ---------- */
  const saveNotes = useCallback(async (iid: string) => {
    const body: Record<string, unknown> = {};
    if (text.trim()) body.text = text.trim();
    if (transcript.trim()) body.transcript = transcript.trim();
    if (!Object.keys(body).length) return true;
    try {
      await command(`/api/vehicle-intakes/${encodeURIComponent(iid)}/continue`, body);
      return true;
    } catch (e) {
      setError(describeError(e));
      return false;
    }
  }, [text, transcript]);

  const analyze = async () => {
    setError(null);
    setBusy("analyze");
    const iid = await ensureIntake();
    if (!iid) { setBusy(null); return; }
    const ok = await saveNotes(iid);
    if (!ok) { setBusy(null); return; }
    try {
      const r = await command(`/api/vehicle-intakes/${encodeURIComponent(iid)}/analyze`, {});
      if (r.status !== "ok") setError(r.decision.reasons.join(" · ") || "The analysis was not run.");
      await refreshStatus(iid);
    } catch (e) {
      const msg = describeError(e);
      setError(e instanceof ApiError && e.code === "blocked"
        ? `${msg} Your photos and notes are kept — add a note or a photo and try again.`
        : `${msg} Your photos and notes are kept; you can still write the tasks yourself.`);
    } finally {
      setBusy(null);
    }
  };

  const choose = async (candidateId: string | null) => {
    if (!intakeId) return;
    setBusy("choose");
    try {
      const r = await command(`/api/vehicle-intakes/${encodeURIComponent(intakeId)}/choose`,
        candidateId ? { vehicle_id: candidateId } : { create_new: true });
      if (r.status !== "ok") setError(r.decision.reasons.join(" · ") || "The choice was not saved.");
      else { setVehicleId(candidateId); setTargetMode(candidateId ? "existing" : "new"); }
      await refreshStatus(intakeId);
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(null);
    }
  };

  const observations = status?.observations?.filter((o) => !o.removed && o.status !== "skipped") || [];
  const conditions = observations.filter((o) => o.kind === "condition");
  const requests = observations.filter((o) => o.kind === "request");
  const identifiers = observations.filter((o) => o.kind === "identifier");
  const milestones = observations.filter((o) => o.kind === "milestone");

  const proposedTasks: ProposedTask[] = useMemo(() => requests.map((o) => ({
    observationId: o.id,
    title: o.text,
    detail: String((o.meta as { detail?: string } | undefined)?.detail || ""),
    assignee: taskEdits[o.id]?.assignee ?? "",
    priority: taskEdits[o.id]?.priority ?? String((o.meta as { priority?: string } | undefined)?.priority || "normal"),
  })), [requests, taskEdits]);

  const apply = async () => {
    if (!intakeId) return;
    setError(null);
    setBusy("apply");
    const ok = await saveNotes(intakeId);
    if (!ok) { setBusy(null); return; }
    const assignees = Array.from(new Set(proposedTasks.map((t) => t.assignee).filter(Boolean)));
    const priorities = Array.from(new Set(proposedTasks.map((t) => t.priority).filter((p) => p && p !== "normal")));
    const body: Record<string, unknown> = {};
    if (assignees.length === 1) body.assignee_user_id = assignees[0];
    if (priorities.length === 1) body.priority = priorities[0];
    try {
      const r = await command<{ decision?: string; status?: string; result?: IntakeApplyResult; reasons?: string[] }>(
        `/api/vehicle-intakes/${encodeURIComponent(intakeId)}/apply`, body);
      const data = r.data;
      if (r.status !== "ok") { setError(r.decision.reasons.join(" · ") || "Nothing was written."); setBusy(null); await refreshStatus(intakeId); return; }
      if (data?.decision === "Blocked") {
        setError(data.reasons?.join(" · ") || "Choose the vehicle before anything is written.");
        await refreshStatus(intakeId);
        setBusy(null);
        return;
      }
      const result = data?.result || null;
      setApplied(result);
      setAppliedStatus(data?.status || null);
      // Per-task assignees that differ from the single default go through intake.correct.
      if (result && assignees.length > 1) {
        const created = result.tasks_created || [];
        for (const t of proposedTasks) {
          if (!t.assignee) continue;
          const match = created.find((c) => c.title && c.title.toLowerCase() === t.title.toLowerCase());
          if (!match?.id) continue;
          try {
            await command(`/api/vehicle-intakes/${encodeURIComponent(intakeId)}/correct`, { task_id: match.id, assignee_user_id: t.assignee });
          } catch { /* the result card still shows the task; assign from there */ }
        }
      }
      await refreshStatus(intakeId);
      // Uploaded photos are in AZKT now; the local copies are no longer the only copy.
      if (result && !(result.photos_failed || []).length) {
        setFiles((xs) => xs.filter((f) => f.status !== "saved"));
      }
    } catch (e) {
      setError(`${describeError(e)} Nothing partial was saved as complete.`);
    } finally {
      setBusy(null);
    }
  };

  const undo = async () => {
    if (!intakeId) return;
    setBusy("undo");
    try {
      const r = await command(`/api/vehicle-intakes/${encodeURIComponent(intakeId)}/undo`, {});
      if (r.status === "ok") {
        toast({ message: "Reversed. The photos and notes are kept on the intake.", tone: "ok" });
        setApplied(null);
        setAppliedStatus(null);
      } else {
        toast({ message: r.decision.reasons.join(" · ") || "Nothing was reversed.", tone: "risk" });
      }
      await refreshStatus(intakeId);
    } catch (e) {
      toast({ message: describeError(e), tone: "blocked" });
    } finally {
      setBusy(null);
    }
  };

  const addMore = () => {
    setApplied(null);
    setAppliedStatus(null);
    setText("");
    setTranscript("");
    setError(null);
  };

  /* ---------- derived ---------- */
  const st = status?.intake;
  const needsChoice = st?.status === "needs_choice";
  const candidates = (st?.choice?.candidates as IntakeCandidate[] | undefined) || [];
  const allowNew = (st?.choice?.allow_new as boolean | undefined) ?? true;
  const analyzed = !!st && ["analyzed", "needs_info", "partially_applied", "undone"].includes(st.status);
  const onDevice = files.filter((f) => f.status !== "saved");
  const savedCount = files.filter((f) => f.status === "saved").length;
  const hasContent = files.length > 0 || !!text.trim() || !!transcript.trim();
  const targetLabel = status?.target?.label
    || (targetMode === "new" ? "New vehicle" : targetMode === "existing" ? (vehicles?.find((v) => v.id === vehicleId) ? vehicleLabel(vehicles.find((v) => v.id === vehicleId)) : "Existing vehicle") : "Find the vehicle");

  if (!canIntake) {
    return (
      <div className="page">
        <PageHeader title="Book in vehicle" crumbs={[{ label: "Vehicles", to: "/vehicles" }, { label: "Book in" }]} />
        <GlassPanel clip><EmptyState title="Intake isn't part of your role" body="Ask the owner if you need to add photos or notes to a vehicle." /></GlassPanel>
      </div>
    );
  }

  return (
    <div className="page page-wide">
      <PageHeader
        title={routeVehicleId ? "Add photos / update" : "Book in vehicle"}
        crumbs={[
          { label: "Vehicles", to: "/vehicles" },
          ...(routeVehicleId ? [{ label: "Vehicle", to: `/vehicles/${encodeURIComponent(routeVehicleId)}` }] : []),
          { label: routeVehicleId ? "Add update" : "Book in" },
        ]}
        subtitle="Take pictures, say or type what you see, then AZKT proposes the card and the work. Nothing is written until you apply."
        actions={
          intakeId ? (
            <Button
              variant="ghost"
              onClick={async () => {
                if (!intakeId) return;
                try { await command(`/api/vehicle-intakes/${encodeURIComponent(intakeId)}/abandon`, {}); } catch { /* it may already be closed */ }
                writeDraft(scope, null);
                nav(routeVehicleId ? `/vehicles/${encodeURIComponent(routeVehicleId)}` : "/vehicles");
              }}
            >
              Discard
            </Button>
          ) : undefined
        }
      />

      {error ? <Notice tone="blocked" lead="Not saved" role="alert">{error}</Notice> : null}
      {uploadsOff ? <Notice tone="risk" lead="Uploads unavailable">Photos stay on this device. Your notes still save.</Notice> : null}
      {draftWarning ? <Notice tone="risk" lead="Limited device storage">The words are kept on this device; the photos are not. Send them before leaving this page.</Notice> : null}
      {onDevice.length ? (
        <Notice
          tone="risk"
          lead="Saved on device, not in AZKT"
          role="alert"
          action={<Button size="sm" variant="primary" onClick={retryAll} disabled={uploadsOff} disabledReason="Uploads aren't available yet.">Retry</Button>}
        >
          {onDevice.length} photo{onDevice.length === 1 ? "" : "s"} still waiting. Nothing is complete until they are saved.
        </Notice>
      ) : null}

      <div className="ik-grid">
        {/* ---------------- composer ---------------- */}
        <div className="stack">
          <GlassPanel padded>
            <div className="stack">
              <div className="vh-sect__head"><h2>Photos <span className="count">{savedCount} saved · {onDevice.length} waiting</span></h2></div>
              <div className="row-wrap">
                <Button variant="glass" size="xl" className="ik-photo-btn" onClick={() => captureRef.current?.click()}>
                  <span aria-hidden="true">📷</span> Take photo
                </Button>
                <Button variant="soft" size="xl" className="ik-photo-btn" onClick={() => galleryRef.current?.click()}>
                  Choose from gallery
                </Button>
                <input ref={captureRef} type="file" accept="image/*" capture="environment" onChange={addFiles} className="sr-only" aria-label="Take a photo" />
                <input ref={galleryRef} type="file" accept="image/*" multiple onChange={addFiles} className="sr-only" aria-label="Choose photos" />
              </div>
              {files.length ? (
                <div className="ik-shots">
                  {files.map((f, i) => (
                    <div key={f.key} className="ik-shot">
                      {f.dataUrl ? <img className="ik-shot__img" src={f.dataUrl} alt={f.name} /> : <span className="ik-shot__ph">No preview on this device</span>}
                      <div className="ik-shot__body">
                        <span className="ik-shot__name" title={f.name}>{f.name}</span>
                        <span className="fs12" style={{ color: f.status === "failed" ? "var(--blocked)" : f.status === "saved" ? "var(--ok)" : "var(--t3)" }}>
                          {f.status === "saved" ? "Saved to AZKT"
                            : f.status === "uploading" ? `Sending… ${f.progress}%`
                              : f.status === "failed" ? "On this device only" : `Waiting · ${humanSize(f.size)}`}
                        </span>
                        {f.status === "uploading" ? (
                          <div className="ik-progress" role="progressbar" aria-valuenow={f.progress} aria-valuemin={0} aria-valuemax={100} aria-label={`Sending ${f.name}`}>
                            <span style={{ width: `${f.progress}%` }} />
                          </div>
                        ) : null}
                        {f.error ? <span className="fs12" style={{ color: "var(--blocked)" }}>{f.error}</span> : null}
                        <span className="ik-shot__acts">
                          {f.status === "failed" ? <button type="button" className="linklike fs12" onClick={() => retryFile(f.key)}>Retry</button> : null}
                          {f.status !== "uploading" ? <button type="button" className="linklike fs12" onClick={() => removeFile(f.key)}>Remove</button> : null}
                          <button type="button" className="linklike fs12" onClick={() => moveFile(f.key, -1)} disabled={i === 0} aria-label={`Move ${f.name} earlier`}>←</button>
                          <button type="button" className="linklike fs12" onClick={() => moveFile(f.key, 1)} disabled={i === files.length - 1} aria-label={`Move ${f.name} later`}>→</button>
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <span className="fs13 t4">JPEG, PNG or HEIC. Originals are kept; AZKT makes its own viewing copy.</span>
              )}
            </div>
          </GlassPanel>

          <GlassPanel padded>
            <div className="stack">
              <div className="vh-sect__head"><h2>What you saw</h2></div>
              <div className="row-wrap">
                {recordOk || speechOk ? (
                  recording ? (
                    <>
                      <Button variant="danger" size="xl" onClick={stopRecording}>Stop recording</Button>
                      <span className="ik-rec"><span className="ik-rec__dot" aria-hidden="true" />Recording…</span>
                    </>
                  ) : (
                    <Button variant="glass" size="xl" onClick={() => void startRecording()}>
                      <span aria-hidden="true">🎙</span> Record a note
                    </Button>
                  )
                ) : (
                  <span className="fs13 t3">Recording isn't available in this browser — type what you said.</span>
                )}
              </div>
              {recording && liveText ? <div className="ik-live">{liveText}</div> : null}
              <Field
                label={speechOk ? "Transcript" : "Type what you said"}
                hint={speechOk ? "Editable. Correct anything the transcription got wrong before analysing." : "Your words, kept verbatim as the source of the bullets."}
              >
                <Textarea rows={3} value={transcript} onChange={(e) => setTranscript(e.target.value)} placeholder="e.g. Dent on the left door, A/C not cold, needs tires and a detail" />
              </Field>
              <Field label="Notes" hint="Anything else worth recording — kept verbatim.">
                <Textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="Optional" />
              </Field>
              <div className="row-wrap">
                <Button
                  variant="primary"
                  size="xl"
                  loading={busy === "analyze"}
                  disabled={!hasContent || (targetMode === "existing" && !vehicleId)}
                  disabledReason={!hasContent ? "Add a photo, a note or a voice transcript first." : "Pick the vehicle this update is for."}
                  onClick={() => void analyze()}
                >
                  {analyzed ? "Analyze again" : "Analyze"}
                </Button>
                <span className="fs12 t4">AZKT proposes; you apply. Nothing reaches the card until you do.</span>
              </div>
            </div>
          </GlassPanel>

          {needsChoice ? (
            <GlassPanel padded>
              <div className="stack-sm">
                <div className="vh-sect__head"><h2>Which vehicle?</h2></div>
                <Notice tone="amber" lead="Choose before anything is written">
                  {String(st?.choice?.reason || "More than one vehicle matches what you sent.")}
                </Notice>
                {candidates.map((c) => (
                  <div key={c.vehicle_id} className="ik-cand">
                    <VehicleThumb assetId={c.hero_asset_id} />
                    <span className="ik-cand__body">
                      <span style={{ fontWeight: 500 }}>{c.title || "Vehicle"}</span>
                      <span className="ik-cand__meta">
                        {[c.stock_no, c.frame_no_raw, c.model_year ? String(c.model_year) : null, c.color, c.location].filter(Boolean).join(" · ") || "No identifying details recorded"}
                      </span>
                      {c.reasons?.length ? <span className="ik-cand__meta">Matched on: {c.reasons.join(", ")}</span> : null}
                    </span>
                    <Button size="sm" variant="primary" loading={busy === "choose"} onClick={() => void choose(c.vehicle_id)}>This one</Button>
                  </div>
                ))}
                {allowNew ? (
                  <Button variant="soft" loading={busy === "choose"} onClick={() => void choose(null)}>None of these — it's a new vehicle</Button>
                ) : (
                  <span className="fs12 t4">A new card isn't offered here: an existing card already has this identity.</span>
                )}
              </div>
            </GlassPanel>
          ) : null}

          {analyzed && !applied ? (
            <GlassPanel padded>
              <div className="stack">
                <div className="vh-sect__head">
                  <h2>What AZKT found <span className="count">revision {st?.revision}</span></h2>
                  <span className="fs12 t4">{String((st?.analysis as { provenance?: { path?: string } } | undefined)?.provenance?.path || "")}</span>
                </div>

                <section className="vh-sect">
                  <h2>Condition</h2>
                  {conditions.length ? conditions.map((o) => <ObservationRow key={o.id} o={o} />) : <span className="not-recorded">Nothing extracted from your words or photos.</span>}
                </section>

                <section className="vh-sect">
                  <h2>Proposed work <span className="count">{proposedTasks.length}</span></h2>
                  {proposedTasks.length ? proposedTasks.map((t) => (
                    <div key={t.observationId} className="vh-item" style={{ padding: "10px 0" }}>
                      <div className="vh-item__main">
                        <div className="vh-item__title">{t.title}</div>
                        {t.detail ? <div className="vh-item__meta">{t.detail}</div> : null}
                      </div>
                      <div className="vh-item__acts">
                        <Select
                          aria-label={`Who does "${t.title}"`}
                          value={t.assignee}
                          style={{ width: "auto", minWidth: 150 }}
                          onChange={(e) => setTaskEdits((x) => ({ ...x, [t.observationId]: { assignee: e.target.value, priority: t.priority } }))}
                        >
                          <option value="">Unassigned</option>
                          {user ? <option value={user.id}>{user.display_name} (you)</option> : null}
                          {people.people.filter((p) => p.id !== user?.id).map((p) => <option key={p.id} value={p.id}>{p.display_name}</option>)}
                        </Select>
                        <Select
                          aria-label={`Priority for "${t.title}"`}
                          value={t.priority}
                          style={{ width: "auto", minWidth: 120 }}
                          onChange={(e) => setTaskEdits((x) => ({ ...x, [t.observationId]: { assignee: t.assignee, priority: e.target.value } }))}
                        >
                          <option value="low">Low</option>
                          <option value="normal">Normal</option>
                          <option value="high">High</option>
                          <option value="urgent">Urgent</option>
                        </Select>
                      </div>
                    </div>
                  )) : <span className="not-recorded">No work proposed. You can add tasks on the vehicle afterwards.</span>}
                </section>

                {identifiers.length ? (
                  <section className="vh-sect">
                    <h2>Identifiers</h2>
                    {identifiers.map((o) => (
                      <div key={o.id} className="ik-obs">
                        <span className="ik-obs__body">
                          <span>{o.field ? `${o.field.replace(/_/g, " ")}: ` : ""}{o.value || o.text}</span>
                          <span className="ik-obs__src">
                            {o.confidence === "uncertain"
                              ? "Needs confirmation — not written to the card. A focused task will ask for it."
                              : o.confidence === "observed" ? "Read from a photo" : "Stated by you"}
                          </span>
                        </span>
                        {o.confidence === "uncertain" ? <Chip size="sm" tone="risk">Uncertain</Chip> : null}
                      </div>
                    ))}
                  </section>
                ) : null}

                {milestones.length ? (
                  <section className="vh-sect">
                    <h2>Dates</h2>
                    {milestones.map((o) => (
                      <div key={o.id} className="ik-obs">
                        <span className="ik-obs__body">
                          <span>{o.text}</span>
                          <span className="ik-obs__src">{o.value ? `Recorded as ${o.value}` : "No date stated — nothing will be recorded"}</span>
                        </span>
                      </div>
                    ))}
                  </section>
                ) : null}

                {st?.missing_fields?.length ? (
                  <Notice tone="amber" lead="Missing">{st.missing_fields.map((m) => m.replace(/_/g, " ")).join(", ")}. Applying still works; focused tasks track the gaps.</Notice>
                ) : null}

                <div className="row-wrap">
                  <Button
                    variant="primary" size="xl" loading={busy === "apply"}
                    disabled={needsChoice}
                    disabledReason="Choose the vehicle first."
                    onClick={() => void apply()}
                  >
                    Apply to {targetLabel}
                  </Button>
                  <span className="fs12 t4">Routine internal work is applied without a separate approval for each bullet.</span>
                </div>
              </div>
            </GlassPanel>
          ) : null}

          {applied ? (
            <ResultCard
              result={applied}
              status={appliedStatus}
              busyUndo={busy === "undo"}
              onUndo={() => void undo()}
              onAddMore={addMore}
              onAssign={() => setAssignOpen(true)}
            />
          ) : null}
        </div>

        {/* ---------------- target, always beside the composer ---------------- */}
        <GlassPanel padded as="aside" aria-label="Intake target" className="ik-target">
          <div className="vh-sect__head"><h2>This update is for</h2></div>
          <SegmentedControl<TargetMode>
            label="Target"
            block
            size="sm"
            value={targetMode}
            onChange={(m) => {
              if (intakeId || routeVehicleId) {
                toast({ message: intakeId ? "The target is fixed for this intake. Use “Which vehicle?” or discard and start again." : "This update is for the vehicle you came from. Book in from Vehicles to target another.", tone: "risk" });
                return;
              }
              setTargetMode(m);
              if (m !== "existing") setVehicleId(null);
            }}
            options={[
              { value: "new", label: "New vehicle", disabled: !canCreateCard, disabledReason: "Creating a new card needs permission to edit vehicles." },
              { value: "existing", label: "Existing" },
              { value: "find", label: "Find it" },
            ]}
          />
          <div className="fs13 t2">{targetLabel}</div>

          {targetMode === "existing" ? (
            <div className="stack-sm">
              {vehicles === undefined ? <Loading rows={1} label="Loading vehicles" /> : vehicles === null ? (
                <Field label="Vehicle id" hint="The vehicle list isn't available — paste the id.">
                  <Input value={vehicleId || ""} onChange={(e) => setVehicleId(e.target.value || null)} placeholder="Vehicle id" />
                </Field>
              ) : (
                <>
                  <Field label="Find the vehicle">
                    <Input value={vehicleFilter} onChange={(e) => setVehicleFilter(e.target.value)} placeholder="Stock number, model, year" />
                  </Field>
                  <div className="opts">
                    {vehicles
                      .filter((v) => !vehicleFilter || vehicleLabel(v).toLowerCase().includes(vehicleFilter.toLowerCase()))
                      .slice(0, 8)
                      .map((v) => (
                        <button
                          key={v.id}
                          type="button"
                          role="radio"
                          aria-checked={vehicleId === v.id}
                          className="opt"
                          disabled={!!intakeId || !!routeVehicleId}
                          title={intakeId ? "The target is fixed for this intake." : routeVehicleId ? "This update is for the vehicle you came from." : undefined}
                          onClick={() => setVehicleId(v.id)}
                        >
                          <span className="opt__mark" aria-hidden="true" />
                          <span className="opt__label">{vehicleLabel(v)}</span>
                        </button>
                      ))}
                    {!vehicles.length ? <span className="not-recorded">No vehicles to choose from yet.</span> : null}
                  </div>
                </>
              )}
            </div>
          ) : targetMode === "new" ? (
            <span className="fs13 t3">A new card is created with an internal stock number. Frame number, year and price are never invented — missing ones become focused tasks.</span>
          ) : !canCreateCard ? (
            <span className="fs13 t3">AZKT matches the photos and words to a vehicle you already work on. If nothing matches, ask the owner to book it in.</span>
          ) : (
            <span className="fs13 t3">AZKT matches the photos and words to an existing card. If several match, it asks you before writing anything.</span>
          )}

          {st ? (
            <div className="stack-sm" style={{ borderTop: "1px solid var(--line2)", paddingTop: 10 }}>
              <div className="between fs13">
                <span className="t3">Intake</span>
                <span>{INTAKE_STATUS_LABELS[st.status] || st.status}</span>
              </div>
              <div className="between fs13">
                <span className="t3">Saved to AZKT</span>
                <span className="tnum">{status?.saved_to_azkt?.assets ?? 0} photo{(status?.saved_to_azkt?.assets ?? 0) === 1 ? "" : "s"}</span>
              </div>
              <div className="between fs13">
                <span className="t3">On this device</span>
                <span className="tnum">{onDevice.length}</span>
              </div>
              {st.last_error ? <span className="fs12" style={{ color: "var(--blocked)" }}>{st.last_error}</span> : null}
            </div>
          ) : null}
        </GlassPanel>
      </div>

      <AssignDialog
        open={assignOpen}
        onClose={() => setAssignOpen(false)}
        tasks={(applied?.tasks_created || []).filter((t) => t.id)}
        people={people.people.map((p) => ({ id: p.id, name: p.display_name }))}
        me={user ? { id: user.id, name: `${user.display_name} (you)` } : null}
        onAssign={async (taskId, userId) => {
          if (!intakeId) return false;
          try {
            const r = await command(`/api/vehicle-intakes/${encodeURIComponent(intakeId)}/correct`, { task_id: taskId, assignee_user_id: userId || null, unassign: !userId });
            if (r.status === "ok") { toast({ message: "Assigned. It shows in their My tasks right away.", tone: "ok" }); return true; }
            toast({ message: r.decision.reasons.join(" · ") || "Not assigned.", tone: "risk" });
            return false;
          } catch (e) {
            toast({ message: describeError(e), tone: "blocked" });
            return false;
          }
        }}
      />
    </div>
  );
}

/* ---------- pieces ---------- */
const OBS_SOURCE_LABEL: Record<string, string> = {
  owner_text: "Owner reported", owner_voice: "Owner reported (voice)", image: "Seen in photos", proposed_check: "Proposed check",
};

function ObservationRow({ o }: { o: IntakeObservation }) {
  const isCheck = o.source === "proposed_check" || o.confidence === "uncertain";
  return (
    <div className="ik-obs">
      <span className="vh-bullet__dot" aria-hidden="true">•</span>
      <span className="ik-obs__body">
        <span>{o.text}</span>
        <span className="ik-obs__src">
          {OBS_SOURCE_LABEL[o.source] || o.source}
          {isCheck ? " · a check to make, not a finding" : ""}
          {o.asset_id ? " · linked to a photo" : ""}
        </span>
      </span>
    </div>
  );
}

function ResultCard({ result, status, busyUndo, onUndo, onAddMore, onAssign }: {
  result: IntakeApplyResult; status: string | null; busyUndo: boolean;
  onUndo: () => void; onAddMore: () => void; onAssign: () => void;
}) {
  const partial = status === "partially_applied" || (result.photos_failed || []).length > 0 || (result.failed_observations || []).length > 0;
  const needsInfo = status === "needs_info" || (result.missing || []).length > 0;
  const tone = partial ? "risk" : needsInfo ? "amber" : "ok";
  const lead = partial ? "Partially saved" : needsInfo ? "Saved · information missing" : "Saved to AZKT";

  return (
    <GlassPanel padded>
      <div className="ik-result">
        <Notice tone={tone} lead={lead} role={partial ? "alert" : "status"}>
          {partial
            ? "Some of this did not reach AZKT. What failed is listed below and can be retried — this is not complete."
            : needsInfo
              ? "The card is saved. Focused tasks track what is still missing."
              : "Everything reached AZKT."}
        </Notice>

        {result.vehicle_id ? (
          <Link to={`/vehicles/${encodeURIComponent(result.vehicle_id)}`} className="wrap" style={{ fontSize: 15 }}>
            {result.created ? "New card: " : "Card: "}{[result.stock_no, result.title].filter(Boolean).join(" · ") || "Open the vehicle"}
          </Link>
        ) : null}

        <div className="ik-result__lines">
          <span>{result.photos_saved} photo{result.photos_saved === 1 ? "" : "s"} saved{(result.photos_failed || []).length ? ` · ${result.photos_failed.length} failed` : ""}</span>
          <span>{(result.condition_bullets || []).length} condition bullet{(result.condition_bullets || []).length === 1 ? "" : "s"}</span>
          <span>{(result.tasks_created || []).length} task{(result.tasks_created || []).length === 1 ? "" : "s"} created · {(result.tasks_updated || []).length} updated</span>
          {(result.issues_created || []).length ? <span>{result.issues_created.length} recon issue{result.issues_created.length === 1 ? "" : "s"}</span> : null}
          {(result.needs_confirmation || []).length ? <span>{result.needs_confirmation.length} identifier{result.needs_confirmation.length === 1 ? "" : "s"} need confirmation — not written to the card</span> : null}
          {(result.missing || []).length ? <span>Missing: {result.missing.map((m) => m.replace(/_/g, " ")).join(", ")}</span> : null}
        </div>

        {(result.tasks_created || []).length ? (
          <div className="stack-sm">
            {result.tasks_created.filter((t) => t.id).map((t) => (
              <div key={t.id} className="vh-link">
                <Link to={`/tasks/${encodeURIComponent(t.id as string)}`} className="wrap">{t.title || "Task"}</Link>
                <span className="vh-link__meta">{t.missing_identity ? "Missing identity" : "New"}</span>
              </div>
            ))}
          </div>
        ) : null}

        {(result.photos_failed || []).length ? (
          <div className="stack-sm">
            {result.photos_failed.map((f, i) => (
              <span key={i} className="fs12" style={{ color: "var(--blocked)" }}>{f.name || f.asset_id || f.upload_id}: {f.error}</span>
            ))}
          </div>
        ) : null}
        {(result.failed_observations || []).length ? (
          <div className="stack-sm">
            {result.failed_observations.map((f, i) => (
              <span key={i} className="fs12" style={{ color: "var(--blocked)" }}>{f.text}: {f.error}</span>
            ))}
          </div>
        ) : null}

        <div className="row-wrap">
          {result.vehicle_id ? <Button variant="glass" to={`/vehicles/${encodeURIComponent(result.vehicle_id)}`}>Edit on the card</Button> : null}
          <Button variant="soft" onClick={onAddMore}>Add more</Button>
          <Button variant="soft" onClick={onAssign} disabled={!(result.tasks_created || []).some((t) => t.id)} disabledReason="No tasks were created.">Assign</Button>
          <Button variant="ghost" loading={busyUndo} onClick={onUndo}>Undo</Button>
        </div>
        <span className="fs12 t4">Undo reverses what can be reversed. Photos and notes stay as evidence.</span>
      </div>
    </GlassPanel>
  );
}

function AssignDialog({ open, onClose, tasks, people, me, onAssign }: {
  open: boolean; onClose: () => void;
  tasks: Array<{ id?: string; title?: string | null }>;
  people: Array<{ id: string; name: string }>;
  me: { id: string; name: string } | null;
  onAssign: (taskId: string, userId: string) => Promise<boolean>;
}) {
  const mobile = useIsMobile();
  const [picks, setPicks] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) setPicks({}); }, [open]);
  return (
    <ResponsiveDialog
      mobile={mobile} open={open} onClose={onClose} title="Assign the new work" size="md"
      description="It shows in their My tasks right away. The owner verifies when it's done."
      footer={
        <>
          <Button
            variant="primary" loading={busy}
            disabled={!Object.keys(picks).length} disabledReason="Choose who does at least one task."
            onClick={async () => {
              setBusy(true);
              for (const [taskId, userId] of Object.entries(picks)) await onAssign(taskId, userId);
              setBusy(false);
              onClose();
            }}
          >Assign</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <div className="stack-sm">
        {tasks.map((t) => (
          <div key={t.id} className="set-row">
            <div className="set-row__main"><span className="set-row__title">{t.title || "Task"}</span></div>
            <div className="set-row__right">
              <Select
                aria-label={`Who does "${t.title || "this task"}"`}
                value={picks[t.id as string] || ""}
                onChange={(e) => setPicks((p) => ({ ...p, [t.id as string]: e.target.value }))}
              >
                <option value="">Leave unassigned</option>
                {me ? <option value={me.id}>{me.name}</option> : null}
                {people.filter((p) => p.id !== me?.id).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </Select>
            </div>
          </div>
        ))}
        {!tasks.length ? <EmptyState title="No tasks to assign" /> : null}
      </div>
    </ResponsiveDialog>
  );
}
