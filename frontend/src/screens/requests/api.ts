/* Endpoints for import requests and candidates (backend/app/routers/requests.py, candidates.py).
   Every write goes through useCommand().run so the CommandResult envelope is explained. */

export const REQ_BASE = "/api/import-requests";
export const CAND_BASE = "/api/candidates";

export const requestsPath = (qs = "") => `${REQ_BASE}${qs}`;
export const requestPath = (id: string) => `${REQ_BASE}/${encodeURIComponent(id)}`;
/** action ∈ update | revise-requirements | attach-opportunity | set-agreement | set-deposit-rule
 *           | confirm-deposit | pause | resume | record-purchase | close */
export const requestAction = (id: string, action: string) => `${requestPath(id)}/${action}`;
export const requestMatch = (id: string) => `${requestPath(id)}/match`;

export const candidatePath = (id: string) => `${CAND_BASE}/${encodeURIComponent(id)}`;
/** action ∈ pass | interest | prepare-buyer-message | mark-sent */
export const matchAction = (matchId: string, action: string) => `${CAND_BASE}/matches/${encodeURIComponent(matchId)}/${action}`;
/** action ∈ detected | complete | revise */
export const translationAction = (tid: string, action: string) => `${CAND_BASE}/translations/${encodeURIComponent(tid)}/${action}`;
export const requestTranslation = (candidateId: string) => `${candidatePath(candidateId)}/translations/request`;
/** action ∈ submit-for-approval | record-submitted | record-result | cancel */
export const bidAction = (bidId: string, action: string) => `${CAND_BASE}/bids/${encodeURIComponent(bidId)}/${action}`;
export const prepareBid = (candidateId: string) => `${candidatePath(candidateId)}/bids/prepare`;

export const activityPath = (entityKind: string, entityId: string, limit = 50) =>
  `/api/activity?entity_kind=${encodeURIComponent(entityKind)}&entity_id=${encodeURIComponent(entityId)}&limit=${limit}`;

export const CURRENCIES = ["USD", "JPY", "EUR", "GBP", "CAD", "AUD", "MXN"];
