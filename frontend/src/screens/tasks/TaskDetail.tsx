/* Employee task page (route /tasks/:id): vehicle identity, instructions, required evidence, evidence saved,
   Complete task (upload → attach → complete), I'm blocked, owner Verify / Reject, history.
   A failed upload keeps a local draft ("Waiting to upload") with Retry; the task never completes without the proof. */
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api, describeError } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, isEmployeeRole, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { TZ } from "../../lib/format";
import { useIsMobile } from "../../lib/viewport";
import { Button, Chip, EmptyState, ErrorState, Expander, Field, GlassPanel, HealthLabel, Input, KeyValues, Loading, Notice, PageHeader, ResponsiveDialog, Textarea, When, useToast } from "../../ui";
import { ReasonDialog, RescheduleSheet, AssignSheet, taskPath, useTaskCommand, useTaskDialogs } from "./TaskSheets";
import { taskHealth } from "./TaskRow";
import { usePeople } from "./usePeople";
import { evidenceHave, evidenceRequirements, isActive, isClosed, isOverdue, missingEvidence, reminderLabel, showsTokyo, typeLabel, STATUS_LABEL, type TaskRelated, type TaskView } from "./types";
import "./tasks.css";

interface TaskResp { task: TaskView; related: TaskRelated; }
interface ActivityRow { id: string; at?: string | null; what?: string; state?: string | null; exception?: boolean; actor?: { display_name?: string | null; kind?: string | null; agent_role?: string | null } | null; }

/* ---------- local draft (per task, per device) ---------- */
interface DraftFile { name: string; type: string; size: number; dataUrl?: string; assetId?: string; }
interface Draft { note: string; reading: string; files: DraftFile[]; savedAt: string; }
const draftKey = (id: string) => `azkt-task-draft:${id}`;
function readDraft(id: string): Draft | null {
  try { const raw = localStorage.getItem(draftKey(id)); return raw ? (JSON.parse(raw) as Draft) : null; } catch { return null; }
}
function writeDraft(id: string, d: Draft | null): boolean {
  try {
    if (!d) { localStorage.removeItem(draftKey(id)); return true; }
    localStorage.setItem(draftKey(id), JSON.stringify(d));
    return true;
  } catch {
    // Quota or private mode: keep the photos out, keep the words.
    if (d) {
      try { localStorage.setItem(draftKey(id), JSON.stringify({ ...d, files: d.files.map((f) => ({ ...f, dataUrl: undefined })) })); } catch { /* nothing we can do */ }
    }
    return false;
  }
}

/* ---------- uploads (assets contract) ---------- */
class UploadsUnavailable extends Error { constructor() { super("Uploads not available yet"); this.name = "UploadsUnavailable"; } }
interface PendingFile extends DraftFile { file?: File; status: "pending" | "uploading" | "done" | "failed"; progress: number; error?: string; }

function readAsDataUrl(file: File): Promise<string | undefined> {
  if (file.size > 4 * 1024 * 1024) return Promise.resolve(undefined);
  return new Promise((res) => { const r = new FileReader(); r.onload = () => res(typeof r.result === "string" ? r.result : undefined); r.onerror = () => res(undefined); r.readAsDataURL(file); });
}
function dataUrlToBlob(dataUrl: string): Blob {
  const [head, body] = dataUrl.split(",");
  const type = /data:([^;]+)/.exec(head)?.[1] || "application/octet-stream";
  const bin = atob(body || "");
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type });
}
function putWithProgress(url: string, blob: Blob, type: string, onProgress: (pct: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url, true);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", type || "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100)); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) { onProgress(100); resolve(); }
      else if (xhr.status === 404 || xhr.status === 501) reject(new UploadsUnavailable());
      else reject(new ApiError(xhr.status, "http", xhr.responseText ? xhr.responseText.slice(0, 200) : `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new ApiError(0, "network", "Could not reach AZKT. Your photo is kept on this device."));
    xhr.onabort = () => reject(new ApiError(0, "network", "Upload cancelled."));
    xhr.send(blob);
  });
}
type Loose = Record<string, unknown> | null | undefined;
const pick = (o: Loose, ...keys: string[]): unknown => { for (const k of keys) { const v = o?.[k]; if (v !== undefined && v !== null) return v; } return undefined; };
async function uploadEvidence(f: PendingFile, onProgress: (pct: number) => void): Promise<string> {
  // Prepare a slot. The assets router exposes POST /api/uploads; /api/uploads/prepare is tried first for the spec'd shape.
  const prepBody = { purpose: "evidence", content_type: f.type, size: f.size, size_bytes: f.size, filename: f.name };
  let prep = await api.post<Loose>("/api/uploads/prepare", prepBody, { tolerate: [404, 405, 501] });
  if (prep === null) prep = await api.post<Loose>("/api/uploads", prepBody, { tolerate: [404, 405, 501] });
  if (prep === null) throw new UploadsUnavailable();
  const d = ((prep as Loose)?.data as Loose) ?? prep;
  const upload = pick(d, "upload") as Loose;
  const uploadId = (pick(upload, "id") ?? pick(d, "upload_id", "id")) as string | undefined;
  if (!uploadId) throw new Error("Upload slot missing an id.");
  const putUrl = (pick(d, "put_url") as string | undefined) || `/api/uploads/${encodeURIComponent(uploadId)}`;
  const blob = f.file ?? (f.dataUrl ? dataUrlToBlob(f.dataUrl) : null);
  if (!blob) throw new Error("Photo data is no longer on this device; add it again.");
  await putWithProgress(putUrl, blob, f.type, onProgress);
  const fin = await api.post<Loose>(`/api/uploads/${encodeURIComponent(uploadId)}/finalize`, { source: "upload" }, { tolerate: [404, 501] });
  if (fin === null) throw new UploadsUnavailable();
  const fd = ((fin as Loose)?.data as Loose) ?? fin;
  if (pick(fd, "status") === "failed") throw new Error(String(pick(fd, "error") || "The file failed its checks."));
  const asset = pick(fd, "asset") as Loose;
  const assetId = (pick(asset, "id") ?? pick(fd, "asset_id")) as string | undefined;
  if (!assetId) throw new Error("Upload finished without an asset id.");
  return assetId;
}

/* ---------- thumbnails with 404 fallback ---------- */
function Thumb({ assetId, alt }: { assetId: string; alt: string }) {
  const [bad, setBad] = useState(false);
  if (bad) return <span className="td-thumb-ph" title={alt}>Photo saved · preview unavailable</span>;
  return <img src={`/api/assets/${encodeURIComponent(assetId)}/thumb`} alt={alt} loading="lazy" onError={() => setBad(true)} />;
}
function VehicleThumb({ assetId }: { assetId?: string | null }) {
  const [bad, setBad] = useState(false);
  if (!assetId || bad) return <span className="placeholder-thumb td-thumb--ph" aria-label="No photo yet" />;
  return <img className="td-thumb" src={`/api/assets/${encodeURIComponent(assetId)}/thumb`} alt="" onError={() => setBad(true)} />;
}

/* ---------- page ---------- */
export default function TaskDetail() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const mobile = useIsMobile();
  const { toast } = useToast();
  const run = useTaskCommand();
  const employee = isEmployeeRole(user?.role);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const q = useQuery<TaskResp | null>((signal) => api.get<TaskResp | null>(`/api/tasks/${encodeURIComponent(id)}`, { signal, tolerate: [404] }), [id, tick]);
  const history = useQuery<{ items: ActivityRow[] } | null>((signal) => api.get<{ items: ActivityRow[] } | null>(`/api/activity?entity_kind=task&entity_id=${encodeURIComponent(id)}&limit=50`, { signal, tolerate: [403, 404, 501] }), [id, tick]);
  const people = usePeople(!employee);
  const dialogs = useTaskDialogs();
  const [draft, setDraft] = useState<Draft | null>(() => readDraft(id));
  const [completeOpen, setCompleteOpen] = useState(false);
  const [blockedOpen, setBlockedOpen] = useState(false);
  const [retryOnOpen, setRetryOnOpen] = useState(false);
  useEffect(() => { setDraft(readDraft(id)); }, [id]);

  const task = q.data?.task || null;
  const related = q.data?.related || {};
  const canWrite = can(user, "tasks.write");
  const mine = !!task && task.owner_user_id === user?.id;
  const canAct = !!task && isActive(task) && task.status !== "awaiting_verification" && canWrite && (mine || !employee);
  const canVerify = can(user, "tasks.verify");
  const reqs = task ? evidenceRequirements(task) : [];
  const missing = task ? missingEvidence(task) : [];
  const health = task ? taskHealth(task) : null;

  const verify = async () => {
    if (!task) return;
    const out = await run(taskPath(task.id, "verify"), { expected_version: task.version }, { okMessage: `${task.title} verified · the vehicle can move on` });
    if (out.result?.status === "ok") reload();
  };
  const start = async () => {
    if (!task) return;
    const out = await run(taskPath(task.id, "start"), { expected_version: task.version }, { okMessage: "Marked in progress" });
    if (out.result?.status === "ok") reload();
  };

  const crumbs = [{ label: employee ? "My tasks" : "Tasks", to: "/tasks" }, { label: task?.title || `Task ${id.slice(0, 8)}` }];

  if (q.loading) return <div className="page"><PageHeader title={<span className="skeleton" style={{ display: "inline-block", width: 220, height: 28 }} />} crumbs={crumbs} /><GlassPanel clip><Loading rows={4} label="Loading task" /></GlassPanel></div>;
  if (q.error) return <div className="page"><PageHeader title="Task" crumbs={crumbs} /><ErrorState error={q.error} onRetry={q.reload} /></div>;
  if (!task) return <div className="page"><PageHeader title="Task not found" crumbs={crumbs} /><GlassPanel clip><EmptyState title="Task not found" body={`Nothing recorded for ${id}. It may have been removed or isn't yours to see.`} action={<Button variant="soft" to="/tasks">Back to tasks</Button>} /></GlassPanel></div>;

  const v = related.vehicle;
  const overdue = isOverdue(task);
  const assignedBy = task.assigned_by ? people.nameOf(task.assigned_by) : null;

  return (
    <div className="page">
      <PageHeader
        title={task.title}
        crumbs={crumbs}
        subtitle={
          <span className="row-wrap" style={{ gap: 6 }}>
            <Chip size="sm" tone="soft">{typeLabel(task)}</Chip>
            <span className={overdue ? "" : undefined} style={overdue ? { color: "var(--blocked)", fontWeight: 500 } : undefined}>
              {task.due_at ? <>{overdue ? "Overdue · " : "Due "}<When iso={task.due_at} tz={TZ.phoenix} format="long" withTokyo={showsTokyo(task)} /></> : "No due time"}
            </span>
            <span>· {reminderLabel(task)}</span>
            {assignedBy ? <span>· assigned by {assignedBy}</span> : null}
            {!employee ? <span>· {people.nameOf(task.owner_user_id)}</span> : null}
            {health ? <HealthLabel health={health.health} label={health.label} /> : null}
          </span>
        }
        actions={
          <div className="td-actions">
            {canAct ? (
              <>
                <Button variant="primary" size="xl" onClick={() => { setRetryOnOpen(false); setCompleteOpen(true); }}>Complete task</Button>
                <Button variant="glass" size="xl" onClick={() => setBlockedOpen(true)}>I'm blocked</Button>
                {task.status === "open" ? <Button variant="ghost" size="xl" onClick={start}>Start</Button> : null}
              </>
            ) : null}
            {task.status === "awaiting_verification" ? (
              <>
                <Button variant="primary" size="xl" onClick={verify} disabled={!canVerify} disabledReason={whyNot("tasks.verify")}>Verify</Button>
                <Button variant="soft" size="xl" onClick={() => dialogs.setReject(task)} disabled={!canVerify} disabledReason={whyNot("tasks.verify")}>Reject</Button>
              </>
            ) : null}
            {!employee && !isClosed(task) ? (
              <>
                <Button variant="soft" onClick={() => dialogs.setReschedule(task)} disabled={!canWrite} disabledReason={whyNot("tasks.write")}>Reschedule</Button>
                <Button variant="soft" onClick={() => dialogs.setAssign(task)} disabled={!can(user, "tasks.assign")} disabledReason="Only owner/manager assign tasks.">{task.owner_user_id ? "Reassign" : "Assign"}</Button>
                <Button variant="ghost" onClick={() => dialogs.setCancel(task)} disabled={!canWrite} disabledReason={whyNot("tasks.write")}>Cancel task</Button>
              </>
            ) : null}
          </div>
        }
      />

      {/* status notices */}
      {task.status === "awaiting_verification" ? (
        <Notice tone="wait" lead="Awaiting verification">saved {task.updated_at ? <When iso={task.updated_at} format="time" /> : "just now"}. The owner verifies before the vehicle moves on.</Notice>
      ) : null}
      {task.status === "blocked" ? (
        <Notice tone="blocked" lead="Blocked" role="alert">{task.block_reason || "Reason not recorded"} · owner notified.</Notice>
      ) : null}
      {task.verification_status === "rejected" && task.status !== "completed" ? (
        <Notice tone="risk" lead="Evidence rejected" role="alert">{task.rejection_reason || "No reason given"}. Fix it and complete the task again.</Notice>
      ) : null}
      {task.status === "completed" ? (
        <Notice tone="ok" lead={task.verification_status === "verified" ? "Verified" : "Done"}>{task.verified_at ? <When iso={task.verified_at} format="long" /> : task.completed_at ? <When iso={task.completed_at} format="long" /> : null}</Notice>
      ) : null}
      {task.status === "cancelled" ? <Notice lead="Cancelled">{task.cancel_reason || "No reason recorded"}</Notice> : null}
      {draft && !isClosed(task) ? (
        <Notice tone="risk" lead="Waiting to upload" role="alert" action={<Button size="sm" variant="primary" onClick={() => { setRetryOnOpen(true); setCompleteOpen(true); }}>Retry</Button>}>
          draft saved on this device {draft.savedAt ? <When iso={draft.savedAt} format="time" /> : null}. Not complete yet.
        </Notice>
      ) : null}

      {/* vehicle identity */}
      {v ? (
        <GlassPanel clip>
          <Link to={`/vehicles/${encodeURIComponent(v.id)}`} className="td-vehicle">
            <VehicleThumb assetId={v.hero_asset_id} />
            <span className="grow" style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <span style={{ fontWeight: 600 }}>{v.title || "Vehicle"}</span>
              <span className="fs13 t3">{[v.stock_no, v.location].filter(Boolean).join(" · ") || "Location not recorded"}</span>
            </span>
            <span className="list-row__chev" aria-hidden="true">›</span>
          </Link>
        </GlassPanel>
      ) : task.vehicle_id ? (
        <GlassPanel clip><Link to={`/vehicles/${encodeURIComponent(task.vehicle_id)}`} className="td-vehicle"><span className="placeholder-thumb td-thumb--ph" /><span>Vehicle {task.vehicle_id.slice(0, 8)}</span></Link></GlassPanel>
      ) : null}

      {(related.opportunity || related.contact) ? (
        <div className="row-wrap fs13">
          {related.opportunity ? <Link to={`/sales?lead=${encodeURIComponent(related.opportunity.id)}`}>Open lead · {related.opportunity.stage_label || related.opportunity.stage}</Link> : null}
          {related.contact ? <Link to={`/contacts/${encodeURIComponent(related.contact.id)}`}>{related.contact.name || "Contact"}{related.contact.company ? ` · ${related.contact.company}` : ""}</Link> : null}
        </div>
      ) : null}

      <GlassPanel clip>
        <section className="td-section">
          <div className="td-section__title">Instructions</div>
          {task.instructions ? <div className="td-body">{task.instructions}</div> : <span className="not-recorded">No instructions recorded.</span>}
          {task.notes ? <div className="td-note">{task.notes}</div> : null}
        </section>
      </GlassPanel>

      <GlassPanel clip>
        <section className="td-section">
          <div className="td-section__title"><span>Required evidence</span>{reqs.length ? <span>{missing.length ? `${missing.length} missing` : "All provided"}</span> : null}</div>
          {reqs.length === 0 ? <span className="not-recorded">No proof required. Done completes it straight away.</span> : reqs.map((r, i) => {
            const have = evidenceHave(task, r.kind);
            const ok = have >= r.min;
            return (
              <div key={i} className="td-ev">
                <span>{r.label}{r.min > 1 ? ` (${r.min})` : ""}</span>
                <span className="td-ev__state" style={{ color: ok ? "var(--ok)" : "var(--risk)" }}>{ok ? "Provided" : `${have}/${r.min} · Not yet`}</span>
              </div>
            );
          })}
          {reqs.length ? <span className="fs12 t4">The owner verifies before the vehicle can move on.</span> : null}
        </section>
      </GlassPanel>

      <GlassPanel clip>
        <section className="td-section">
          <div className="td-section__title">Evidence saved</div>
          {!(task.evidence || []).length ? <span className="not-recorded">Nothing saved yet.</span> : (task.evidence || []).map((e, i) => (
            <div key={i} className="stack-sm" style={{ paddingBottom: 8, borderBottom: "1px solid var(--line2)" }}>
              <span className="fs12 t4">{e.at ? <When iso={e.at} format="long" /> : "Time not recorded"}{e.by ? ` · ${people.nameOf(e.by)}` : ""}</span>
              {(e.asset_ids || []).length ? <div className="td-thumbs">{(e.asset_ids || []).map((a) => <Thumb key={a} assetId={a} alt="Evidence photo" />)}</div> : null}
              {e.note ? <div className="td-note">{e.note}</div> : null}
              {e.reading && Object.keys(e.reading).length ? <div className="td-note">{Object.entries(e.reading).map(([k, val]) => `${k}: ${String(val)}`).join(" · ")}</div> : null}
            </div>
          ))}
        </section>
      </GlassPanel>

      <Expander title="History">
        <div className="td-history" style={{ paddingTop: 6 }}>
          {history.loading ? <Loading rows={2} /> : history.data === null ? <span className="t4">History isn't available for your role.</span> : !history.data?.items?.length ? <span className="t4">No activity recorded yet.</span> : history.data.items.map((h) => (
            <div key={h.id} className="td-history__row">
              <span className="td-history__when">{h.at ? <When iso={h.at} format="long" /> : "—"}</span>
              <span style={h.exception ? { color: "var(--blocked)" } : undefined}>{h.what}{h.actor?.display_name ? <span className="t4"> · {h.actor.display_name}</span> : null}</span>
            </div>
          ))}
        </div>
      </Expander>

      <Expander title="Sources and technical details">
        <KeyValues items={[["Task id", task.id], ["Status", STATUS_LABEL[task.status] || task.status], ["Version", String(task.version)], ["Schedule revision", String(task.schedule_revision ?? 1)], ["Time zone", task.timezone || "America/Phoenix"], ["Source", task.source_kind || "manual"]]} />
      </Expander>

      <CompletionSheet
        task={task} open={completeOpen} onClose={() => setCompleteOpen(false)} draft={draft} autoRetry={retryOnOpen}
        onDraft={(d) => { setDraft(d); const persisted = writeDraft(task.id, d); if (d && !persisted) toast({ message: "Draft words saved; photos too large to keep offline on this device.", tone: "risk" }); }}
        onDone={() => { setDraft(null); writeDraft(task.id, null); setCompleteOpen(false); reload(); }}
      />
      <BlockedSheet task={task} open={blockedOpen} onClose={() => setBlockedOpen(false)} onDone={() => { setBlockedOpen(false); reload(); }} />
      <RescheduleSheet task={dialogs.reschedule} open={!!dialogs.reschedule} onClose={() => dialogs.setReschedule(null)} onSaved={() => reload()} />
      <AssignSheet task={dialogs.assign} open={!!dialogs.assign} onClose={() => dialogs.setAssign(null)} onSaved={() => reload()} />
      <ReasonDialog open={!!dialogs.cancel} onClose={() => dialogs.setCancel(null)} title="Cancel task" description={task.title} label="Why (optional)" confirmLabel="Cancel task" tone="danger"
        onConfirm={async (reason) => { const out = await run(taskPath(task.id, "cancel"), { reason: reason || undefined, expected_version: task.version }, { okMessage: "Cancelled" }); if (out.result?.status === "ok") reload(); return !out.error; }} />
      <ReasonDialog open={!!dialogs.reject} onClose={() => dialogs.setReject(null)} title="Reject evidence" description={task.title} label="What's missing or wrong" confirmLabel="Reject and reopen" required tone="danger"
        onConfirm={async (reason) => { const out = await run(taskPath(task.id, "reject"), { reason, expected_version: task.version }, { okMessage: "Reopened with your reason" }); if (out.result?.status === "ok") reload(); return !out.error; }} />
    </div>
  );
}

/* ---------- Completion sheet ---------- */
function CompletionSheet({ task, open, onClose, draft, autoRetry, onDraft, onDone }: {
  task: TaskView; open: boolean; onClose: () => void; draft: Draft | null; autoRetry: boolean;
  onDraft: (d: Draft | null) => void; onDone: () => void;
}) {
  const mobile = useIsMobile();
  const { toast } = useToast();
  const reqs = useMemo(() => evidenceRequirements(task), [task]);
  const wantsPhoto = reqs.some((r) => r.kind === "photo" || r.kind === "receipt");
  const readingReq = reqs.find((r) => r.kind === "reading");
  const noteReq = reqs.find((r) => r.kind === "note");
  const [files, setFiles] = useState<PendingFile[]>([]);
  const [note, setNote] = useState("");
  const [reading, setReading] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploadsOff, setUploadsOff] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [blockedMsg, setBlockedMsg] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const startedRetry = useRef(false);

  useEffect(() => {
    if (!open) { setReady(false); return; }
    setNote(draft?.note || "");
    setReading(draft?.reading || "");
    setFiles((draft?.files || []).map((f) => ({ ...f, status: f.assetId ? "done" : "pending", progress: f.assetId ? 100 : 0 })));
    setFailed(null);
    setBlockedMsg(null);
    startedRetry.current = false;
    setReady(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const persist = useCallback((fs: PendingFile[], n: string, r: string) => {
    if (!fs.length && !n && !r) { onDraft(null); return; }
    onDraft({ note: n, reading: r, files: fs.map(({ name, type, size, dataUrl, assetId }) => ({ name, type, size, dataUrl, assetId })), savedAt: new Date().toISOString() });
  }, [onDraft]);

  const addFiles = async (e: ChangeEvent<HTMLInputElement>) => {
    const list = Array.from(e.target.files || []);
    e.target.value = "";
    const next: PendingFile[] = [];
    for (const f of list) next.push({ name: f.name, type: f.type || "image/jpeg", size: f.size, dataUrl: await readAsDataUrl(f), file: f, status: "pending", progress: 0 });
    setFiles((xs) => { const merged = [...xs, ...next]; persist(merged, note, reading); return merged; });
  };
  const removeFile = (i: number) => setFiles((xs) => { const merged = xs.filter((_, j) => j !== i); persist(merged, note, reading); return merged; });

  const photosHave = files.filter((f) => f.status === "done").length + evidenceHave(task, "photo") + files.filter((f) => f.status !== "done").length;
  const photoMin = reqs.filter((r) => r.kind === "photo" || r.kind === "receipt").reduce((a, r) => a + r.min, 0);
  const readingOk = !readingReq || !!reading.trim() || evidenceHave(task, "reading") > 0;
  const noteOk = !noteReq || !!note.trim() || evidenceHave(task, "note") > 0;
  const canSave = photosHave >= photoMin && readingOk && noteOk;
  const whyNotSave = !canSave ? [photosHave < photoMin ? "the photo" : null, !readingOk ? "the reading" : null, !noteOk ? "a note" : null].filter(Boolean).join(" and ") : "";

  const save = useCallback(async () => {
    setBusy(true);
    setFailed(null);
    setBlockedMsg(null);
    let current = files;
    // 1) uploads
    for (let i = 0; i < current.length; i++) {
      const f = current[i];
      if (f.status === "done" && f.assetId) continue;
      current = current.map((x, j) => (j === i ? { ...x, status: "uploading", progress: 0, error: undefined } : x));
      setFiles(current);
      try {
        const assetId = await uploadEvidence(f, (pct) => setFiles((xs) => xs.map((x, j) => (j === i ? { ...x, progress: pct } : x))));
        current = current.map((x, j) => (j === i ? { ...x, status: "done", progress: 100, assetId } : x));
        setFiles(current);
        persist(current, note, reading);
      } catch (e) {
        const off = e instanceof UploadsUnavailable;
        current = current.map((x, j) => (j === i ? { ...x, status: "failed", error: describeError(e) } : x));
        setFiles(current);
        persist(current, note, reading);
        setUploadsOff(off);
        setFailed(off ? "Uploads not available yet. Your photo and note are saved as a draft on this device. The task is not complete." : `Upload failed · ${describeError(e)} Your photo and note are saved as a draft on this device. The task is not complete.`);
        setBusy(false);
        return;
      }
    }
    // 2) attach evidence
    const assetIds = current.filter((f) => f.assetId).map((f) => f.assetId as string);
    const readingObj = reading.trim() ? { [readingReq?.label || "reading"]: reading.trim() } : null;
    try {
      if (assetIds.length || note.trim() || readingObj) {
        await api.post(taskPath(task.id, "evidence"), { asset_ids: assetIds, note: note.trim() || null, reading: readingObj, expected_version: task.version });
      }
      // 3) complete
      await api.post(taskPath(task.id, "complete"), {});
      toast({ message: reqs.length ? "Saved · Awaiting verification" : "Task completed", tone: "ok" });
      onDone();
    } catch (e) {
      if (e instanceof ApiError && e.code === "blocked") {
        const miss = e.detail["missing"];
        setBlockedMsg(`${e.message}${Array.isArray(miss) && miss.length ? `: ${miss.join(", ")}` : ""}`);
      } else if (e instanceof ApiError && e.status === 0) {
        persist(current, note, reading);
        setFailed("No connection. Your photo and note are saved as a draft on this device. The task is not complete.");
      } else {
        setFailed(describeError(e));
      }
    } finally {
      setBusy(false);
    }
  }, [files, note, reading, readingReq, reqs.length, task.id, task.version, persist, toast, onDone]);

  // Retry from the "Waiting to upload" notice: runs once the draft state is loaded, with fresh closures.
  useEffect(() => {
    if (open && ready && autoRetry && !startedRetry.current && (files.length || note.trim() || reading.trim())) { startedRetry.current = true; void save(); }
  }, [open, ready, autoRetry, files.length, note, reading, save]);

  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="Complete task" description={task.title}
      footer={
        <div className="stack-sm" style={{ width: "100%" }}>
          {failed ? (
            <div className="row-wrap">
              <Button variant="primary" onClick={save} loading={busy} disabled={uploadsOff && wantsPhoto} disabledReason="Uploads aren't available yet; try again later.">Retry upload</Button>
              <Button variant="soft" onClick={onClose}>Keep draft</Button>
            </div>
          ) : (
            <div className="row-wrap">
              <Button variant="primary" onClick={save} loading={busy} disabled={!canSave} disabledReason={`Add ${whyNotSave} first`}>Save &amp; complete</Button>
              <Button variant="ghost" onClick={onClose}>Cancel</Button>
              {!canSave ? <span className="fs12 t4">Add {whyNotSave} first</span> : null}
            </div>
          )}
        </div>
      }>
      <div className="stack">
        {wantsPhoto || !reqs.length ? (
          <div className="stack-sm">
            <Button variant="glass" size="xl" className="td-photo-btn" block>
              <span aria-hidden="true">📷</span> {files.length ? "Add another photo" : wantsPhoto ? "Add photo" : "Add photo (optional)"}
              <input type="file" accept="image/*" capture="environment" multiple onChange={addFiles} aria-label="Add photo" />
            </Button>
            {files.map((f, i) => (
              <div key={i} className="stack-sm" style={{ gap: 4 }}>
                <div className="td-file">
                  <span className="td-file__name">{f.name}</span>
                  <span className="row" style={{ gap: 6 }}>
                    <span className="fs12" style={{ color: f.status === "failed" ? "var(--blocked)" : f.status === "done" ? "var(--ok)" : "var(--t3)" }}>
                      {f.status === "done" ? "Uploaded" : f.status === "uploading" ? `Uploading… ${f.progress}%` : f.status === "failed" ? "Waiting to upload" : `${Math.round(f.size / 1024)} KB`}
                    </span>
                    {f.status !== "uploading" && f.status !== "done" ? <button type="button" className="linklike fs12" onClick={() => removeFile(i)}>Remove</button> : null}
                  </span>
                </div>
                {f.status === "uploading" ? <div className="td-progress" role="progressbar" aria-valuenow={f.progress} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${f.progress}%` }} /></div> : null}
                {f.error ? <span className="fs12" style={{ color: "var(--blocked)" }}>{f.error}</span> : null}
              </div>
            ))}
          </div>
        ) : null}
        {readingReq ? (
          <Field label={readingReq.label} required>
            <Input value={reading} onChange={(e) => { setReading(e.target.value); persist(files, note, e.target.value); }} placeholder="e.g. 480 g R134a" />
          </Field>
        ) : null}
        <Field label={noteReq ? noteReq.label : "Note"} required={!!noteReq} hint={noteReq ? undefined : "Anything unexpected, readings, part numbers."}>
          <Textarea value={note} onChange={(e) => { setNote(e.target.value); persist(files, e.target.value, reading); }} rows={3} placeholder="What you did, what you noticed…" />
        </Field>
        {failed ? <Notice tone="risk" lead="Upload failed" role="alert">{failed}</Notice> : null}
        {blockedMsg ? <Notice tone="blocked" lead="Can't complete yet" role="alert">{blockedMsg}</Notice> : null}
        {uploadsOff && !wantsPhoto ? <span className="fs12 t4">Uploads not available yet — notes still save.</span> : null}
      </div>
    </ResponsiveDialog>
  );
}

/* ---------- Blocked sheet ---------- */
const BLOCK_REASONS = ["Wrong part delivered", "Need a tool or lift", "Question for the owner"];
function BlockedSheet({ task, open, onClose, onDone }: { task: TaskView; open: boolean; onClose: () => void; onDone: () => void }) {
  const mobile = useIsMobile();
  const run = useTaskCommand();
  const [other, setOther] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => { if (open) setOther(""); }, [open]);
  const pick = async (reason: string) => {
    setBusy(reason);
    const out = await run(taskPath(task.id, "block"), { reason, expected_version: task.version }, { okMessage: "Owner notified · task marked Blocked" });
    setBusy(null);
    if (out.result?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="What's blocking you?" size="sm">
      <div className="td-reasons">
        {BLOCK_REASONS.map((r) => <Button key={r} variant="glass" size="xl" loading={busy === r} onClick={() => pick(r)}>{r}</Button>)}
        <Field label="Something else">
          <Input value={other} onChange={(e) => setOther(e.target.value)} placeholder="Say what's in the way" />
        </Field>
        <Button variant="primary" size="xl" disabled={!other.trim()} disabledReason="Write what's blocking you." loading={busy === other} onClick={() => pick(other.trim())}>Send</Button>
        <span className="fs12 t4">The owner is notified with the vehicle and this task. The task stays open as Blocked.</span>
      </div>
    </ResponsiveDialog>
  );
}
