/* Endpoints for shipments and shipping-quote cases (backend/app/routers/shipping.py). */

export const SHIPMENTS = "/api/shipments";
export const QUOTES = "/api/shipping/quotes";

export const shipmentsPath = (qs = "") => `${SHIPMENTS}${qs}`;
export const shipmentPath = (id: string) => `${SHIPMENTS}/${encodeURIComponent(id)}`;
/** action ∈ update | record-milestone | set-storage-deadline | add-leg */
export const shipmentAction = (id: string, action: string) => `${shipmentPath(id)}/${action}`;
/** action ∈ update | remove */
export const legAction = (legId: string, action: string) => `${SHIPMENTS}/legs/${encodeURIComponent(legId)}/${action}`;

export const quotesPath = (qs = "") => `${QUOTES}${qs}`;
export const quotePath = (id: string) => `${QUOTES}/${encodeURIComponent(id)}`;
export const startQuoteCase = () => `${QUOTES}/start`;
/** action ∈ request | record-reply | compare | forward | book | decline */
export const quoteAction = (id: string, action: string) => `${quotePath(id)}/${action}`;

export const activityPath = (entityKind: string, entityId: string, limit = 50) =>
  `/api/activity?entity_kind=${encodeURIComponent(entityKind)}&entity_id=${encodeURIComponent(entityId)}&limit=${limit}`;

export const CURRENCIES = ["USD", "JPY", "EUR", "GBP", "CAD", "AUD", "MXN"];
