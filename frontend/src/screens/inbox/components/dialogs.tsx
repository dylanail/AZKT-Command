/* Dialogs the inbox owns: paste a thread by hand, record a reply sent outside AZKT, and the small
   reason prompt used by Take over / Spam / Archive / Unlink. Every one posts through useCommand. */
import { useEffect, useRef, useState } from "react";
import { Button, Field, Input, ResponsiveDialog, Select, Textarea } from "../../../ui";
import { useCommand } from "../../../lib/useCommand";
import { PASTE_PATH, actionPath, type ThreadAction } from "../api";
import { CLASSIFICATIONS, CLASSIFICATION_LABELS } from "../types";

/* ---------- reason prompt ---------- */
export function ReasonDialog({
  open, onClose, title, description, label, placeholder, confirmLabel, required = false, onConfirm,
  tone = "primary", mobile = false,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  label: string;
  placeholder?: string;
  confirmLabel: string;
  required?: boolean;
  tone?: "primary" | "danger";
  mobile?: boolean;
  onConfirm: (reason: string) => Promise<boolean>;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (open) { setReason(""); setBusy(false); } }, [open]);

  const submit = async () => {
    if (required && !reason.trim()) return;
    setBusy(true);
    const ok = await onConfirm(reason.trim());
    setBusy(false);
    if (ok) onClose();
  };

  return (
    <ResponsiveDialog
      mobile={mobile}
      open={open}
      onClose={onClose}
      title={title}
      description={description}
      size="sm"
      initialFocusRef={ref}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant={tone === "danger" ? "danger" : "primary"} loading={busy} onClick={() => void submit()}
            disabled={required && !reason.trim()} disabledReason={required ? "Say why first." : undefined}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <Field label={label} hint={required ? undefined : "Optional. It is kept with the thread history."}>
        <Textarea ref={ref} rows={3} value={reason} placeholder={placeholder} onChange={(e) => setReason(e.target.value)} />
      </Field>
    </ResponsiveDialog>
  );
}

/* ---------- paste a thread (POST /api/inbox/paste → inbox.paste_thread) ---------- */
export function PasteThreadDialog({ open, onClose, onCreated, mobile }: {
  open: boolean; onClose: () => void; onCreated: (conversationId: string) => void; mobile: boolean;
}) {
  const { run, busy } = useCommand();
  const [from, setFrom] = useState("");
  const [subject, setSubject] = useState("");
  const [to, setTo] = useState("");
  const [account, setAccount] = useState("");
  const [body, setBody] = useState("");
  const ref = useRef<HTMLInputElement>(null);

  useEffect(() => { if (open) { setFrom(""); setSubject(""); setTo(""); setAccount(""); setBody(""); } }, [open]);

  const ready = from.trim().length > 2 && body.trim().length > 0;
  const submit = async () => {
    const r = await run<{ conversation?: { id?: string } }>("paste", PASTE_PATH, {
      from_addr: from.trim(),
      subject: subject.trim(),
      to: to.split(/[,;]/).map((s) => s.trim()).filter(Boolean),
      body,
      account: account.trim() || undefined,
    }, { success: "Thread added. AZKT can draft a reply from it now." });
    const id = r?.data?.conversation?.id;
    if (r?.status === "ok" && id) { onCreated(id); onClose(); }
  };

  return (
    <ResponsiveDialog
      mobile={mobile}
      open={open}
      onClose={onClose}
      title="Paste a thread"
      description="Use this while email is not connected. AZKT records it as entered by hand, never as a message it received."
      size="md"
      initialFocusRef={ref}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={busy("paste")} onClick={() => void submit()} disabled={!ready}
            disabledReason="Add who it is from and what they wrote.">Add thread</Button>
        </>
      }
    >
      <div className="stack-sm">
        <Field label="From" required hint="The customer's email address.">
          <Input ref={ref} value={from} onChange={(e) => setFrom(e.target.value)} placeholder="name@example.com" inputMode="email" />
        </Field>
        <Field label="Subject">
          <Input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="Re: 1994 Suzuki Carry" />
        </Field>
        <Field label="Sent to" hint="Which of your addresses received it. Separate several with commas.">
          <Input value={to} onChange={(e) => setTo(e.target.value)} placeholder="info@azkeitrucks.com" />
        </Field>
        <Field label="Mailbox label" hint="What to call this mailbox in the list. Leave blank for the default.">
          <Input value={account} onChange={(e) => setAccount(e.target.value)} placeholder="Business email" />
        </Field>
        <Field label="What they wrote" required>
          <Textarea rows={8} value={body} onChange={(e) => setBody(e.target.value)} placeholder="Paste the message here." />
        </Field>
      </div>
    </ResponsiveDialog>
  );
}

/* ---------- record a reply sent outside AZKT (inbox.manual_reply_recorded) ---------- */
export function ManualReplyDialog({ conversationId, open, onClose, onSaved, mobile, defaultTo, defaultSubject }: {
  conversationId: string; open: boolean; onClose: () => void; onSaved: () => void; mobile: boolean;
  defaultTo: string[]; defaultSubject: string;
}) {
  const { run, busy } = useCommand();
  const [to, setTo] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [note, setNote] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!open) return;
    setTo(defaultTo.join(", "));
    setSubject(defaultSubject);
    setBody("");
    setNote("");
  }, [open, defaultTo, defaultSubject]);

  const submit = async () => {
    const r = await run("manual-reply", actionPath(conversationId, "manual-reply" as ThreadAction), {
      body,
      subject: subject.trim() || undefined,
      to: to.split(/[,;]/).map((s) => s.trim()).filter(Boolean),
      note: note.trim(),
    }, { success: "Recorded. The thread now shows that you replied." });
    if (r?.status === "ok") { onSaved(); onClose(); }
  };

  return (
    <ResponsiveDialog
      mobile={mobile}
      open={open}
      onClose={onClose}
      title="I replied outside AZKT"
      description="Record what you sent from Gmail or your phone. This only writes down what happened — it does not let AZKT send on its own."
      size="md"
      initialFocusRef={ref}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={busy("manual-reply")} onClick={() => void submit()} disabled={!body.trim()}
            disabledReason="Paste what you sent first.">Record reply</Button>
        </>
      }
    >
      <div className="stack-sm">
        <Field label="Sent to"><Input value={to} onChange={(e) => setTo(e.target.value)} placeholder="name@example.com" /></Field>
        <Field label="Subject"><Input value={subject} onChange={(e) => setSubject(e.target.value)} /></Field>
        <Field label="What you sent" required>
          <Textarea ref={ref} rows={8} value={body} onChange={(e) => setBody(e.target.value)} placeholder="Paste the reply you sent." />
        </Field>
        <Field label="Note" hint="Anything worth remembering about why you replied by hand.">
          <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Called first, confirmed by email" />
        </Field>
      </div>
    </ResponsiveDialog>
  );
}

/* ---------- correct the classification (inbox.classify_override) ---------- */
export function ClassifyDialog({ conversationId, open, onClose, onSaved, current, version, mobile }: {
  conversationId: string; open: boolean; onClose: () => void; onSaved: () => void;
  current: string | null; version: number; mobile: boolean;
}) {
  const { run, busy } = useCommand();
  const [value, setValue] = useState(current || "customer");
  const [reason, setReason] = useState("");
  useEffect(() => { if (open) { setValue(current || "customer"); setReason(""); } }, [open, current]);

  const submit = async () => {
    const r = await run("classify", actionPath(conversationId, "classify"), {
      classification: value, reason: reason.trim(), expected_version: version,
    }, { success: `Filed as ${CLASSIFICATION_LABELS[value] || value}.` });
    if (r?.status === "ok") { onSaved(); onClose(); }
  };

  return (
    <ResponsiveDialog
      mobile={mobile}
      open={open}
      onClose={onClose}
      title="What kind of thread is this?"
      description="Correcting this changes how AZKT treats the thread. The original reasons are kept."
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={busy("classify")} onClick={() => void submit()} disabled={value === current}
            disabledReason="Pick a different kind first.">Save</Button>
        </>
      }
    >
      <div className="stack-sm">
        <Field label="Kind">
          <Select value={value} onChange={(e) => setValue(e.target.value)}>
            {CLASSIFICATIONS.map((c) => <option key={c} value={c}>{CLASSIFICATION_LABELS[c]}</option>)}
          </Select>
        </Field>
        <Field label="Why" hint="Optional.">
          <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="This is a buyer, not a newsletter" />
        </Field>
      </div>
    </ResponsiveDialog>
  );
}
