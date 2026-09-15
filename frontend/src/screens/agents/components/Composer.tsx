/* The composer: pinned-context chip, photo/voice attachments through the existing upload flow, and the
   message box. Enter sends, Shift+Enter starts a new line, and an unsent draft is kept per role for this
   browser session so switching roles never loses what you typed. */
import { useCallback, useEffect, useRef, useState, type ChangeEvent, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { describeError } from "../../../lib/api";
import { Button, Chip, IconButton, MicIcon, PlusIcon, useToast } from "../../../ui";
import { UploadsUnavailable, humanSize, uploadOne } from "../../vehicles/uploads";
import type { PinnedContext } from "../context";
import { readDraft, writeDraft } from "../useChat";
import type { AgentRole } from "../types";

interface Pending {
  key: string;
  name: string;
  kind: "photo" | "voice" | "file";
  size: number;
  pct: number;
  assetId: string | null;
  error: string | null;
}

export interface ComposerProps {
  role: AgentRole;
  pinned: PinnedContext | null;
  onClearPinned: () => void;
  sending: boolean;
  blocked?: string | null;
  restored: string;
  onRestoredConsumed: () => void;
  focusSignal: number;
  onSend: (text: string, attachments: string[]) => Promise<void> | void;
}

const recorderSupported = () =>
  typeof window !== "undefined" && typeof window.MediaRecorder !== "undefined" && !!navigator.mediaDevices?.getUserMedia;

export function Composer({ role, pinned, onClearPinned, sending, blocked, restored, onRestoredConsumed, focusSignal, onSend }: ComposerProps) {
  const { toast } = useToast();
  const [text, setText] = useState(() => readDraft(role));
  const [pending, setPending] = useState<Pending[]>([]);
  const [recording, setRecording] = useState(false);
  const boxRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  useEffect(() => { setText(readDraft(role)); setPending([]); }, [role]);
  useEffect(() => { writeDraft(role, text); }, [role, text]);
  useEffect(() => {
    if (!restored) return;
    setText((t) => (t ? t : restored));
    onRestoredConsumed();
    boxRef.current?.focus();
  }, [restored, onRestoredConsumed]);
  useEffect(() => { if (focusSignal) boxRef.current?.focus(); }, [focusSignal]);

  // Grow with the text, up to a readable maximum.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  useEffect(() => () => {
    try { recRef.current?.stream?.getTracks().forEach((t) => t.stop()); } catch { /* already stopped */ }
  }, []);

  const addFiles = useCallback(async (files: File[], kind: Pending["kind"]) => {
    for (const file of files) {
      const key = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
      setPending((p) => [...p, { key, name: file.name || (kind === "voice" ? "Voice note" : "File"), kind, size: file.size, pct: 0, assetId: null, error: null }]);
      try {
        const assetId = await uploadOne(file, {
          purpose: kind === "voice" ? "voice" : "intake",
          onProgress: (pct) => setPending((p) => p.map((x) => (x.key === key ? { ...x, pct } : x))),
        });
        setPending((p) => p.map((x) => (x.key === key ? { ...x, assetId, pct: 100 } : x)));
      } catch (e) {
        const message = e instanceof UploadsUnavailable
          ? "Uploads aren't available right now. The file stays on this device."
          : describeError(e);
        setPending((p) => p.map((x) => (x.key === key ? { ...x, error: message } : x)));
        toast({ title: "That file wasn't saved", message, tone: "risk", duration: 7000 });
      }
    }
  }, [toast]);

  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length) return;
    void addFiles(files, files.every((f) => f.type.startsWith("audio/")) ? "voice" : "photo");
  };

  const startRecording = async () => {
    if (!recorderSupported()) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (ev) => { if (ev.data.size) chunksRef.current.push(ev.data); };
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || "audio/webm" });
        chunksRef.current = [];
        if (!blob.size) return;
        const file = new File([blob], "voice-note.webm", { type: blob.type || "audio/webm" });
        void addFiles([file], "voice");
      };
      recRef.current = rec;
      rec.start();
      setRecording(true);
    } catch (e) {
      toast({ title: "Microphone not available", message: describeError(e), tone: "risk" });
    }
  };

  const stopRecording = () => {
    try { recRef.current?.stop(); } catch { /* already stopped */ }
    recRef.current = null;
    setRecording(false);
  };

  const uploading = pending.some((p) => !p.assetId && !p.error);
  const ready = pending.filter((p) => p.assetId).map((p) => p.assetId as string);
  const hasVoice = pending.some((p) => p.kind === "voice" && p.assetId);
  const canSend = !sending && !uploading && !blocked && (!!text.trim() || ready.length > 0);

  const sendNow = async () => {
    if (!canSend) return;
    const body = text;
    setText("");
    writeDraft(role, "");
    setPending([]);
    await onSend(body, ready);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey) return;
    if ((e.nativeEvent as unknown as { isComposing?: boolean }).isComposing) return;
    e.preventDefault();
    void sendNow();
  };

  const sendReason = blocked
    || (sending ? "Waiting for the current answer." : uploading ? "Wait for the files to finish uploading." : "Type a message or attach a photo first.");

  return (
    <div className="ag-composer">
      <div className="ag-composer__ctx">
        {pinned ? (
          <>
            <span className="fs13 t3 nowrap">Talking about</span>
            {pinned.href ? <Link className="chip chip--sm" to={pinned.href}>{pinned.label}</Link> : <Chip size="sm" tone="soft">{pinned.label}</Chip>}
            <button type="button" className="linklike fs13" onClick={onClearPinned}>Talk about everything instead</button>
          </>
        ) : (
          <span className="fs13 t3">Not pinned to one record. Open a vehicle and use Ask to pin it.</span>
        )}
      </div>

      {pending.length ? (
        <ul className="ag-attach" aria-label="Files attached to this message">
          {pending.map((p) => (
            <li key={p.key} className={["ag-attach__item", p.error ? "ag-attach__item--bad" : ""].filter(Boolean).join(" ")}>
              <span className="truncate">{p.name}</span>
              <span className="fs12 t4 nowrap tnum">
                {p.error ? p.error : p.assetId ? `saved${p.size ? ` · ${humanSize(p.size)}` : ""}` : `${p.pct}%`}
              </span>
              <button type="button" className="linklike fs12" onClick={() => setPending((list) => list.filter((x) => x.key !== p.key))}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      {hasVoice ? (
        <div className="fs13 t3">
          Type what you said as well. If AZKT cannot transcribe the recording it keeps the audio and asks you for the words.
        </div>
      ) : null}

      <div className="ag-composer__row">
        <input ref={fileRef} type="file" accept="image/*,audio/*" multiple hidden onChange={onPick} />
        <IconButton
          label="Attach a photo or voice note"
          onClick={() => fileRef.current?.click()}
          disabled={!!blocked}
          disabledReason={blocked || undefined}
        >
          <PlusIcon size={16} />
        </IconButton>
        <IconButton
          label={recording ? "Stop recording" : "Record a voice note"}
          active={recording}
          onClick={() => (recording ? stopRecording() : void startRecording())}
          disabled={!!blocked || !recorderSupported()}
          disabledReason={blocked || "This browser can't record audio. Attach an audio file instead."}
        >
          <MicIcon size={16} />
        </IconButton>
        <label className="sr-only" htmlFor="ag-composer-box">Message</label>
        <textarea
          id="ag-composer-box"
          ref={boxRef}
          className="textarea ag-composer__box"
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          placeholder={blocked ? "Chat is unavailable for your role." : "Ask about a truck, a task or a customer…"}
          disabled={!!blocked}
        />
        <Button variant="primary" size="xl" loading={sending} disabled={!canSend} disabledReason={sendReason} onClick={() => void sendNow()}>
          Send
        </Button>
      </div>
      <div className="fs12 t4">
        {recording ? "Recording — press the microphone again to stop." : "Enter sends · Shift+Enter starts a new line"}
      </div>
    </div>
  );
}
