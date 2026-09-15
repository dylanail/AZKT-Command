/* Shapes for the reminder/Telegram surfaces, copied from the backend:
   - reminders.serialize_delivery + STATE_LABELS  (backend/app/services/reminders.py)
   - telegram_bot.serialize_pairing + the pairing commands (backend/app/services/telegram_bot.py)
   - routers/notifications.py GET /api/notifications/prefs
   Nothing here is invented: every field exists in those serializers. */

/* ---- scheduled deliveries (GET /api/notifications/deliveries?task_id=) ---- */
export interface Delivery {
  id: string;
  kind: string;
  channel: string;
  state: string;
  late: boolean;
  task_id: string | null;
  task_revision: number | null;
  entity_kind: string | null;
  entity_id: string | null;
  recipient_user_id: string | null;
  deliver_at: string | null;
  sent_at: string | null;
  delivered_at: string | null;
  attempts: number;
  last_error: string | null;
  cancel_reason: string | null;
  provider_ref: string | null;
  fallback_of_id: string | null;
  receipt: Record<string, unknown>;
  /** True when the reminder went to someone else: the state is shown, the address/chat is not. */
  destination_hidden?: boolean;
  dedupe_key: string | null;
  /** The server's own plain-language name for `state`. Render this, never a guess. */
  state_label: string;
}
export interface DeliveriesResp { task_id: string; deliveries: Delivery[]; note: string }

/** Only for tone; the words always come from the server's state_label. */
export function deliveryTone(state: string): "ok" | "risk" | "blocked" | "wait" | "neutral" {
  switch (state) {
    case "delivered": return "ok";
    case "sent":
    case "accepted": return "wait";
    case "failed": return "blocked";
    case "unknown": return "risk";
    case "cancelled":
    case "superseded":
    case "expired": return "neutral";
    default: return "wait";
  }
}

export const CHANNEL_LABELS: Record<string, string> = { email: "Email", telegram: "Telegram" };
export const DELIVERY_KIND_LABELS: Record<string, string> = {
  task_reminder: "Task reminder", overdue: "Overdue", digest: "Morning digest",
  deposit_confirmed: "Deposit paid", case_update: "Case update", connection_issue: "Connection issue",
};

/* ---- Telegram (GET /api/telegram/status, POST /api/telegram/pair) ---- */
export interface TelegramPairing {
  id: string;
  status: "pending" | "active" | "revoked" | "expired" | string;
  telegram_user_id: string | null;
  chat_id: string | null;
  username: string | null;
  version: number;
  confirmed_in_chat_at: string | null;
  confirmed_in_app_at: string | null;
  token_expires_at: string | null;
  revoked_at: string | null;
  revoke_reason: string | null;
  delivery_failures: number | null;
  blocked_at: string | null;
  context: Record<string, unknown>;
  last_error: string | null;
}
export interface TelegramStatus {
  active: TelegramPairing | null;
  history: TelegramPairing[];
  bot_username: string | null;
  webhook_secret_configured: boolean;
}
/** data of the telegram.start_pairing command. */
export interface PairStarted {
  pairing_id: string;
  status: string;
  expires_at: string | null;
  deep_link: string | null;
  start_token: string;
  setup_blocked: string | null;
}
/** data of the telegram.set_webhook command. */
export interface WebhookQueued { queued: boolean; external_action_id: string | null; url: string }

export const PAIRING_STATUS_LABELS: Record<string, string> = {
  pending: "Waiting for the link to be used", active: "Paired", revoked: "Revoked", expired: "Expired",
};

/* ---- business defaults (GET /api/notifications/prefs) ---- */
export interface NotificationPrefsResp {
  prefs: {
    timezone: string | null;
    reminder_email: string | null;
    reminder_email_verified: boolean;
    reminder_email_verified_at: string | null;
    notification_prefs: { channels: Record<string, string>; quiet_hours: { start: string; end: string } | null; digest_time: string };
  };
  business_defaults: {
    channels: Record<string, string>;
    digest_local_time: string;
    late_grace_minutes: number;
    obsolete_after_hours: number;
    overdue_delay_minutes: number;
  };
  telegram: TelegramStatus;
  write_with: string;
}
