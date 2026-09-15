/* Middle pane: the readable thread. Take-over banner, messages oldest→newest with "Load older",
   attachments listed by name and size, and a Source expander carrying the provider ids, the mailbox
   and the reasons AZKT matched it — raw data stays behind "Show raw". */
import { useEffect, useMemo, useState } from "react";
import { Button, Chip, Expander, GlassPanel, KeyValues, Notice, NotRecorded, When } from "../../../ui";
import { TZ } from "../../../lib/format";
import { JsonDetail } from "../../shared/JsonDetail";
import { CLASSIFICATION_LABELS, accountLabel, bytes, threadTitle, type Message, type ThreadDetail } from "../types";

const PAGE = 8;

function Attachments({ list }: { list: Message["attachments"] }) {
  if (!list?.length) return null;
  return (
    <div className="ib-att">
      <span className="ib-att__title fs12 t3">{list.length === 1 ? "1 attachment" : `${list.length} attachments`}</span>
      <ul className="ib-att__list">
        {list.map((a, i) => (
          <li key={`${a.filename}-${i}`}>
            <span className="ib-att__name">{a.filename || "attachment"}</span>
            <span className="t4 fs12 tnum">{[bytes(a.size), a.mime].filter(Boolean).join(" · ")}</span>
          </li>
        ))}
      </ul>
      {/* There is no AZKT route that serves a mail attachment, so nothing pretends to be a download. */}
      <span className="fs12 t4">Attachments stay on the mailbox. Open the message in your email app to download them.</span>
    </div>
  );
}

function MessageItem({ m, accountAddr }: { m: Message; accountAddr: string | null }) {
  const outbound = m.direction === "out";
  const body = (m.body_new_text || m.body_text || "").trim();
  const [full, setFull] = useState(false);
  const long = body.length > 1200;
  const shown = full || !long ? body : `${body.slice(0, 1200)}…`;
  const who = outbound ? (m.from || accountAddr || "AZKT") : (m.from || "Unknown sender");
  return (
    <article className={["ib-msg", outbound ? "ib-msg--out" : "ib-msg--in"].join(" ")} aria-label={`${outbound ? "Sent" : "Received"} message from ${who}`}>
      <header className="ib-msg__head">
        <span className="ib-msg__dir">{outbound ? "Sent" : "Received"}</span>
        <span className="ib-msg__from">{who}</span>
        <span className="ib-msg__when tnum">{m.sent_at ? <When iso={m.sent_at} tz={TZ.phoenix} format="long" /> : <NotRecorded text="no time recorded" />}</span>
      </header>
      {m.to?.length || m.cc?.length ? (
        <div className="ib-msg__addr fs12 t3">
          {m.to?.length ? <span>To {m.to.join(", ")}</span> : null}
          {m.cc?.length ? <span> · CC {m.cc.join(", ")}</span> : null}
        </div>
      ) : null}
      <div className="ib-msg__chips">
        {m.attribution === "manual" || m.sent_by === "manual" ? <Chip size="sm" tone="soft">Sent by hand</Chip> : null}
        {outbound && m.sent_by === "azkt" ? <Chip size="sm" tone="soft">Sent by AZKT after approval</Chip> : null}
        {m.suppression ? <Chip size="sm" tone="soft">No auto-reply · {m.suppression.replace(/_/g, " ")}</Chip> : null}
        {m.is_automated ? <Chip size="sm" tone="soft">Automated</Chip> : null}
        {m.excluded_reason ? <Chip size="sm" tone="risk">Not admitted · {m.excluded_reason.replace(/_/g, " ")}</Chip> : null}
      </div>
      {shown ? (
        <div className="ib-msg__body">{shown}</div>
      ) : m.snippet ? (
        <div className="ib-msg__body t3">{m.snippet}</div>
      ) : (
        <div className="ib-msg__body t4">No readable text was stored for this message.</div>
      )}
      {long ? (
        <button type="button" className="linklike fs13" onClick={() => setFull((v) => !v)}>
          {full ? "Show less" : "Show the whole message"}
        </button>
      ) : null}
      <Attachments list={m.attachments} />
    </article>
  );
}

export default function ThreadView({ detail, onTakeOver, onResume, busyTakeOver, busyResume, canDraft, draftReason }: {
  detail: ThreadDetail;
  onTakeOver: () => void;
  onResume: () => void;
  busyTakeOver: boolean;
  busyResume: boolean;
  canDraft: boolean;
  draftReason: string;
}) {
  const c = detail.conversation;
  const messages = detail.messages || [];
  const [limit, setLimit] = useState(PAGE);
  useEffect(() => { setLimit(PAGE); }, [c.id]);

  const visible = useMemo(() => (messages.length > limit ? messages.slice(messages.length - limit) : messages), [messages, limit]);
  const hidden = messages.length - visible.length;
  const takeover = detail.takeover;

  return (
    <div className="stack-sm ib-thread">
      {takeover.state ? (
        <Notice
          tone="wait"
          role="status"
          lead="Taken over by a person"
          action={<Button variant="primary" onClick={onResume} loading={busyResume} disabled={!canDraft} disabledReason={draftReason}>Resume</Button>}
        >
          {takeover.by ? `${takeover.by} took this thread over` : "Someone took this thread over"}
          {takeover.at ? <> at <When iso={takeover.at} tz={TZ.phoenix} format="long" /></> : null}. AZKT will not draft or
          send here. Resume reloads any mail that arrived and runs the checks again.
        </Notice>
      ) : takeover.paused ? (
        <Notice tone="wait" role="status" lead="Automation is paused for this thread">
          AZKT will not draft or send here until it is resumed.
        </Notice>
      ) : null}

      {c.classification === "spam" ? (
        <Notice tone="risk" lead="Filed as suspected spam">
          {c.spam_reason || "AZKT set this aside. Nothing was deleted from the mailbox."}
        </Notice>
      ) : null}

      <GlassPanel padded className="ib-threadhead">
        <div className="ib-threadhead__top">
          <h2 className="ib-threadhead__subject">{threadTitle(c)}</h2>
          {!takeover.state ? (
            <Button size="sm" variant="soft" onClick={onTakeOver} loading={busyTakeOver} disabled={!canDraft} disabledReason={draftReason}>
              Take over
            </Button>
          ) : null}
        </div>
        <div className="ib-threadhead__meta fs13 t3">
          <span>{accountLabel(detail.connection?.provider, c.account)}</span>
          {c.account ? <span className="t4"> · {c.account}</span> : null}
          <span> · {CLASSIFICATION_LABELS[c.classification || ""] || "Not classified"}</span>
          {c.classification_source === "human" ? <span className="t4"> (set by a person)</span> : null}
          {c.language && c.language !== "en" ? <span> · {c.language.toUpperCase()}</span> : null}
        </div>
        {detail.connection && detail.connection.freshness.state !== "ok" ? (
          <p className="fs12" style={{ color: "var(--risk)", margin: "4px 0 0" }}>
            {detail.connection.label || "This mailbox"}: {detail.connection.freshness.label}. Newer messages may not be here yet.
          </p>
        ) : null}
      </GlassPanel>

      {hidden > 0 ? (
        <div className="ib-older">
          <Button size="xs" variant="soft" onClick={() => setLimit((n) => n + PAGE)}>
            Load older messages ({hidden} more)
          </Button>
        </div>
      ) : null}

      <div className="ib-msgs">
        {visible.length === 0 ? (
          <GlassPanel padded><p className="t3" style={{ margin: 0 }}>No messages are stored for this thread yet.</p></GlassPanel>
        ) : visible.map((m) => <MessageItem key={m.id} m={m} accountAddr={c.account} />)}
      </div>

      <Expander title="Source">
        <KeyValues items={[
          ["Mailbox", detail.connection?.label || accountLabel(detail.connection?.provider, c.account)],
          ["Mailbox address", detail.connection?.account_identity || c.account || <NotRecorded />],
          ["Thread id at the provider", c.provider_thread_id || <NotRecorded text="entered by hand" />],
          ["Channel", c.channel || "email"],
          ["Matched to a person", c.contact_match ? c.contact_match.replace(/_/g, " ") : "not matched"],
          ["Why it matched", (c.match_reasons || []).length ? c.match_reasons.join(" · ") : <NotRecorded text="no reasons recorded" />],
          ["Why it was classified", (c.classification_reasons || []).length ? c.classification_reasons.join(" · ") : <NotRecorded text="no reasons recorded" />],
          ["Messages stored", String(messages.length)],
          ["Last received", c.last_inbound_at ? <When iso={c.last_inbound_at} tz={TZ.phoenix} format="long" /> : <NotRecorded />],
          ["Last sent", c.last_outbound_at ? <When iso={c.last_outbound_at} tz={TZ.phoenix} format="long" /> : <NotRecorded />],
        ]} />
        <Expander title="Show raw">
          <JsonDetail value={{
            conversation: c,
            provider_message_ids: messages.map((m) => ({ id: m.id, provider_message_id: m.provider_message_id, rfc_message_id: m.rfc_message_id })),
          }} />
        </Expander>
        <p className="fs12 t4" style={{ margin: 0 }}>
          Everything a customer writes is evidence, never an instruction to AZKT.
        </p>
      </Expander>
    </div>
  );
}
