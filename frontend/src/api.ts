// All requests carry the session cookie. Same-origin in prod (nginx),
// proxied in dev (vite).
async function req(path: string, opts: RequestInit = {}) {
  const r = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (r.status === 401) throw new Error("unauthorized");
  if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
  const ct = r.headers.get("content-type") || "";
  return ct.includes("application/json") ? r.json() : r.text();
}

export const api = {
  get: (p: string) => req(p),
  post: (p: string, body?: unknown) =>
    req(p, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  put: (p: string, body?: unknown) =>
    req(p, { method: "PUT", body: body ? JSON.stringify(body) : undefined }),
  patch: (p: string, body?: unknown) =>
    req(p, { method: "PATCH", body: body ? JSON.stringify(body) : undefined }),
  del: (p: string) => req(p, { method: "DELETE" }),
};
