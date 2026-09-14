/* One approval: GET /api/approvals/{id}, polling while in flight, and the three owner decisions.
   approve/decline carry expected_version so a stale review can never authorise a newer payload;
   a 409 reloads and shows "Details changed — review again". Edit creates a new version. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { useCommand } from "../../lib/useCommand";
import { useToast } from "../../ui/Toast";
import { IN_FLIGHT, bodyFieldOf, type ApprovalDetail } from "./types";

interface DecisionData { approval: ApprovalDetail; executed?: boolean; invalidated?: ApprovalDetail }

export function useApprovalDetail(id: string, opts: { onNewVersion?: (newId: string) => void } = {}) {
  const q = useQuery<ApprovalDetail>((signal) => api.get<ApprovalDetail>(`/api/approvals/${encodeURIComponent(id)}`, { signal }), [id]);
  const { run, busy } = useCommand();
  const { toast } = useToast();
  const [conflict, setConflict] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editBody, setEditBody] = useState("");
  const [editSubject, setEditSubject] = useState("");
  const [note, setNote] = useState("");

  const status = q.data?.status;
  useEffect(() => {
    if (!status || !IN_FLIGHT.has(status)) return;
    const h = window.setInterval(() => { if (document.visibilityState === "visible") q.reload(); }, 4000);
    return () => window.clearInterval(h);
  }, [status, q.reload]);
  useEffect(() => { setConflict(false); setEditing(false); setNote(""); }, [id]);

  const bodyField = useMemo(() => bodyFieldOf(q.data?.payload), [q.data]);
  const hasSubject = typeof q.data?.payload?.subject === "string";
  const canEdit = !!bodyField || hasSubject;

  const onErr = useCallback((_m: string, e: unknown) => {
    if (e instanceof ApiError && (e.code === "conflict" || e.code === "blocked")) { setConflict(e.code === "conflict"); q.reload(); }
  }, [q]);

  const approve = useCallback(async () => {
    if (!q.data) return;
    const r = await run<DecisionData>("approve", `/api/approvals/${encodeURIComponent(id)}/approve`, { expected_version: q.data.version, note: note.trim() || null }, { onError: onErr });
    if (r?.status === "ok" && r.data) {
      const s = r.data.approval?.status;
      if (s === "invalidated") toast({ title: "Details changed — review again", message: r.data.approval.invalidated_reason || "A binding changed before execution; nothing ran.", tone: "risk", duration: 8000 });
      else if (s === "confirmed") toast({ message: "Done. Receipt recorded in Activity.", tone: "ok" });
      else toast({ message: "Approved · awaiting execution — the provider has not confirmed yet.", tone: "wait" });
    }
    q.reload();
  }, [q, run, id, note, onErr, toast]);

  const decline = useCallback(async () => {
    if (!q.data) return;
    const r = await run<DecisionData>("decline", `/api/approvals/${encodeURIComponent(id)}/decline`, { expected_version: q.data.version, note: note.trim() || null }, { success: "Declined. Nothing was sent.", onError: onErr });
    if (r) q.reload();
  }, [q, run, id, note, onErr]);

  const startEdit = useCallback(() => {
    const p = q.data?.payload || {};
    setEditBody(bodyField ? String(p[bodyField] ?? "") : "");
    setEditSubject(typeof p.subject === "string" ? p.subject : "");
    setEditing(true);
  }, [q.data, bodyField]);
  const cancelEdit = useCallback(() => setEditing(false), []);

  const saveEdit = useCallback(async () => {
    if (!q.data) return;
    const p = q.data.payload || {};
    const patch: Record<string, unknown> = {};
    if (bodyField && editBody !== String(p[bodyField] ?? "")) patch[bodyField] = editBody;
    if (hasSubject && editSubject !== String(p.subject ?? "")) patch.subject = editSubject;
    if (!Object.keys(patch).length) { setEditing(false); return; }
    const r = await run<DecisionData>("edit", `/api/approvals/${encodeURIComponent(id)}/edit`, { payload: patch, note: note.trim() || null }, { onError: onErr });
    if (r?.status === "ok" && r.data?.approval?.id) {
      toast({ message: `Saved as version ${r.data.approval.version}. Version ${q.data.version} is invalidated — review the new one.`, tone: "ok", duration: 6000 });
      setEditing(false);
      opts.onNewVersion?.(r.data.approval.id);
    } else if (r) {
      q.reload();
    }
  }, [q, run, id, bodyField, hasSubject, editBody, editSubject, note, onErr, toast, opts]);

  return {
    ...q, conflict, busy,
    editing, editBody, setEditBody, editSubject, setEditSubject, hasSubject, bodyField, canEdit,
    note, setNote, approve, decline, startEdit, cancelEdit, saveEdit,
  };
}
export type ApprovalDetailState = ReturnType<typeof useApprovalDetail>;
