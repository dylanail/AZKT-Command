/* Settings › Telegram. Pairing lives in the Telegram domain (backend/app/routers/telegram.py):
   GET  /api/telegram/status                      — bot username, webhook secret, pairings
   POST /api/telegram/pair                        — owner; one-use 10-minute start token + deep link
   POST /api/telegram/set-webhook                 — owner; queues a fenced external action
   POST /api/telegram/pairings/{id}/confirm|revoke — owner
   The owner confirms the Telegram identity in the web app before it is enabled (spec §5.5); everyone
   else sees their own pairing state and is told to ask the owner. */
import { useCallback, useEffect, useState } from "react";
import { api } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { can } from "../../../lib/perms";
import { useCommand } from "../../../lib/useCommand";
import { useQuery } from "../../../lib/useQuery";
import { Badge, Button, ErrorState, Expander, GlassPanel, Loading, Notice, ResponsiveDialog, When, useToast } from "../../../ui";
import { useIsMobile } from "../../../lib/viewport";
import { PAIRING_STATUS_LABELS, type PairStarted, type TelegramPairing, type TelegramStatus, type WebhookQueued } from "./notifyTypes";
import "../../../styles/notify.css";

function statusLabel(s: string): string { return PAIRING_STATUS_LABELS[s] || s; }

function pairingWho(p: TelegramPairing): string {
  if (p.username) return `@${p.username}`;
  if (p.telegram_user_id) return `Telegram account ${p.telegram_user_id}`;
  return "No Telegram account has used the link yet";
}

async function copy(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through to the manual path */ }
  return false;
}

export function TelegramSection() {
  const { user } = useAuth();
  const { toast } = useToast();
  const { run, busy } = useCommand();
  const mobile = useIsMobile();
  const owner = user?.role === "owner" && can(user, "connections");
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const q = useQuery<TelegramStatus | null>((signal) => api.get<TelegramStatus | null>("/api/telegram/status", { signal, tolerate: [404, 501] }), [tick]);
  const [started, setStarted] = useState<PairStarted | null>(null);
  const [webhook, setWebhook] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<TelegramPairing | null>(null);

  const s = q.data;
  const pending = s?.history.find((p) => p.status === "pending") || null;

  // While a start link is out, re-read the status so "used the link" shows up without a manual refresh.
  useEffect(() => {
    if (!pending && !started) return;
    const h = window.setInterval(() => { if (document.visibilityState === "visible") reload(); }, 10000);
    return () => window.clearInterval(h);
  }, [pending, started, reload]);

  if (q.loading) return <GlassPanel clip padded><Loading label="Loading Telegram status" rows={3} /></GlassPanel>;
  if (q.error) return <GlassPanel clip padded><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  if (!s) {
    return (
      <GlassPanel clip padded>
        <Notice tone="wait" lead="Telegram isn&apos;t available yet">
          The server doesn&apos;t serve Telegram pairing on this build. Email reminders are unaffected.
        </Notice>
      </GlassPanel>
    );
  }

  const active = s.active;

  const startPairing = async () => {
    setProblem(null);
    const r = await run<PairStarted>("tg-pair", "/api/telegram/pair", {}, {
      success: "Start link created. It works once and expires in 10 minutes.",
      onError: (m) => setProblem(m),
    });
    if (r?.status === "ok" && r.data) setStarted(r.data);
    reload();
  };

  const confirmPairing = async () => {
    if (!pending) return;
    setProblem(null);
    const r = await run("tg-confirm", `/api/telegram/pairings/${encodeURIComponent(pending.id)}/confirm`, { expected_version: pending.version }, {
      success: "Telegram paired. AZKT will use this chat.",
      onError: (m) => setProblem(m),
    });
    if (r?.status === "ok") { setStarted(null); reload(); }
  };

  const setWebhookNow = async () => {
    setProblem(null);
    setWebhook(null);
    const r = await run<WebhookQueued>("tg-webhook", "/api/telegram/set-webhook", {}, { onError: (m) => setProblem(m) });
    if (r?.status === "ok" && r.data) setWebhook(`Queued for ${r.data.url}. It runs as a checked external action; Connections shows the result.`);
    else if (r?.status === "blocked") setWebhook(r.decision.reasons.join(" · ") || "Blocked by a check.");
    reload();
  };

  const revoke = async (p: TelegramPairing) => {
    setProblem(null);
    const r = await run("tg-revoke", `/api/telegram/pairings/${encodeURIComponent(p.id)}/revoke`, { expected_version: p.version, reason: "revoked by the owner" }, {
      success: "Telegram access ended. Old buttons in the chat stop working.",
      onError: (m) => setProblem(m),
    });
    if (r?.status === "ok") { setRevoking(null); setStarted(null); reload(); }
  };

  const copyToken = async (text: string) => {
    const ok = await copy(text);
    toast(ok
      ? { message: "Copied.", tone: "ok" }
      : { message: "This browser wouldn't let AZKT copy. Select the text and copy it by hand.", tone: "risk" });
  };

  return (
    <div className="stack">
      {problem ? <Notice tone="blocked" lead="Not done" role="alert">{problem}</Notice> : null}

      {/* what the server has configured */}
      <GlassPanel clip>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Bot</span>
            <span className="set-row__meta">
              {s.bot_username
                ? <>Messages come from @{s.bot_username}.</>
                : "The bot username isn't configured on the server, so the one-tap link can't be built. The manual /start command below still works."}
            </span>
          </div>
          <div className="set-row__right">{s.bot_username ? <Badge tone="ok">@{s.bot_username}</Badge> : <span className="not-recorded">Not configured</span>}</div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Webhook secret</span>
            <span className="set-row__meta">
              {s.webhook_secret_configured
                ? "Messages from Telegram are checked against the secret before anything is stored."
                : "Until the secret is set on the server, AZKT refuses every incoming Telegram message. Pairing can start, but the chat can't reach AZKT."}
            </span>
          </div>
          <div className="set-row__right">
            {s.webhook_secret_configured ? <Badge tone="ok">Configured</Badge> : <Badge tone="risk">Not configured</Badge>}
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Your Telegram</span>
            <span className="set-row__meta">
              {active
                ? <>{pairingWho(active)}{active.confirmed_in_app_at ? <> · confirmed <When iso={active.confirmed_in_app_at} format="long" /></> : null}</>
                : "Not paired. Telegram choices in Reminders fall back to email until it is."}
            </span>
          </div>
          <div className="set-row__right">
            {active ? <Badge tone="ok">Paired</Badge> : <Badge tone="wait">Not paired</Badge>}
            {active && owner ? <Button size="sm" variant="ghost" onClick={() => setRevoking(active)}>Revoke</Button> : null}
          </div>
        </div>
        {active && (active.delivery_failures || active.last_error || active.blocked_at) ? (
          <div className="set-row">
            <div className="set-row__main">
              <span className="set-row__title">Delivery trouble</span>
              <span className="set-row__meta">
                {active.blocked_at ? <>The chat blocked the bot <When iso={active.blocked_at} format="long" />. </> : null}
                {active.delivery_failures ? `${active.delivery_failures} recent ${active.delivery_failures === 1 ? "failure" : "failures"}. ` : ""}
                {active.last_error || "Reminders set to Telegram fall back to email."}
              </span>
            </div>
            <div className="set-row__right"><Badge tone="risk">Check this</Badge></div>
          </div>
        ) : null}
      </GlassPanel>

      {!owner ? (
        <Notice tone="wait" lead="Pairing is the owner's job">
          Ask the owner to pair Telegram for you from Settings. Your reminders keep going to email until then.
        </Notice>
      ) : (
        <div className="stack">
          {/* start / finish pairing */}
          <GlassPanel clip padded>
            <div className="stack-sm">
              <div className="eyebrow">Pair a Telegram account</div>
              <span className="fs13 t3">
                The link works once and expires after 10 minutes. Open it in Telegram, then confirm here which account used it —
                AZKT never trusts a username or a forwarded message.
              </span>
              <div className="row-wrap">
                <Button variant="primary" loading={busy("tg-pair")} onClick={startPairing}>
                  {pending || started ? "Create a new link" : "Pair my Telegram"}
                </Button>
                <Button variant="ghost" onClick={reload}>Check again</Button>
              </div>

              {started?.setup_blocked ? (
                <Notice tone="risk" lead="One-tap link unavailable">{started.setup_blocked}. Send the command below to the bot by hand instead.</Notice>
              ) : null}

              {started ? (
                <div className="stack-sm">
                  {started.deep_link ? (
                    <div className="row-wrap">
                      <a className="btn btn--soft" href={started.deep_link} target="_blank" rel="noopener noreferrer">Open Telegram and start</a>
                      <span className="fs12 t4">Opens t.me in a new tab.</span>
                    </div>
                  ) : null}
                  <div className="tg-token">
                    <code>/start {started.start_token}</code>
                    <Button size="sm" variant="soft" onClick={() => copyToken(`/start ${started.start_token}`)}>Copy</Button>
                    <span className="fs12 t4">
                      Send this to {s.bot_username ? `@${s.bot_username}` : "the bot"} in a private chat.
                      {started.expires_at ? <> Expires <When iso={started.expires_at} relative />.</> : null}
                    </span>
                  </div>
                </div>
              ) : null}

              {pending ? (
                <div className="stack-sm">
                  <div className="tg-facts">
                    <span>Waiting link · {pairingWho(pending)}</span>
                    {pending.token_expires_at ? <span>Expires <When iso={pending.token_expires_at} format="long" /></span> : null}
                    {pending.confirmed_in_chat_at ? <span>Used in Telegram <When iso={pending.confirmed_in_chat_at} format="long" /></span> : null}
                  </div>
                  <div className="row-wrap">
                    <Button variant="primary" loading={busy("tg-confirm")} onClick={confirmPairing}
                      disabled={!pending.telegram_user_id || !pending.chat_id}
                      disabledReason="Open the link in Telegram first — AZKT has to see which account used it.">
                      Confirm this Telegram account
                    </Button>
                  </div>
                </div>
              ) : null}
            </div>
          </GlassPanel>

          {/* webhook registration */}
          <GlassPanel clip padded>
            <div className="stack-sm">
              <div className="eyebrow">Webhook</div>
              <span className="fs13 t3">
                Tells Telegram where to send messages, with the secret header. It runs as a checked external action, so the
                result appears in Connections rather than instantly here.
              </span>
              <div className="row-wrap">
                <Button variant="soft" loading={busy("tg-webhook")} onClick={setWebhookNow}
                  disabled={!s.webhook_secret_configured}
                  disabledReason="The webhook secret isn't configured on the server; unauthenticated updates would be refused.">
                  Set webhook
                </Button>
                {webhook ? <span className="fs13 t3">{webhook}</span> : null}
              </div>
            </div>
          </GlassPanel>

          {s.history.length ? (
            <Expander title={`Pairing history · ${s.history.length}`}>
              <div style={{ paddingTop: 6 }}>
                {s.history.map((p) => (
                  <div key={p.id} className="nf-del">
                    <span className="nf-del__task">{pairingWho(p)}</span>
                    <span className="nf-del__meta">
                      {statusLabel(p.status)}
                      {p.confirmed_in_app_at ? <> · confirmed <When iso={p.confirmed_in_app_at} format="long" /></> : null}
                      {p.revoked_at ? <> · revoked <When iso={p.revoked_at} format="long" /></> : null}
                    </span>
                    {p.revoke_reason ? <span className="nf-del__why">{p.revoke_reason}</span> : null}
                    {p.status === "active" ? (
                      <Button size="sm" variant="ghost" onClick={() => setRevoking(p)}>Revoke</Button>
                    ) : null}
                  </div>
                ))}
              </div>
            </Expander>
          ) : null}
        </div>
      )}

      <ResponsiveDialog
        mobile={mobile}
        open={!!revoking}
        onClose={() => setRevoking(null)}
        title="End Telegram access?"
        description={revoking ? pairingWho(revoking) : undefined}
        size="sm"
        footer={
          <>
            <Button variant="danger" loading={busy("tg-revoke")} onClick={() => revoking && revoke(revoking)}>Revoke</Button>
            <Button variant="ghost" onClick={() => setRevoking(null)}>Keep it</Button>
          </>
        }
      >
        <p className="fs13" style={{ margin: 0 }}>
          The chat stops receiving reminders straight away and buttons in older messages stop working. Telegram is told once.
          You can pair again later with a new link.
        </p>
      </ResponsiveDialog>
    </div>
  );
}
