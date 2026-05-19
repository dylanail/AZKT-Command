"""Minimal, build-free passkey enrollment page.

Served directly by FastAPI so a phone can register its first passkey today
without the Vite/PWA bundle existing on the box. It MUST be reached over the
WEBAUTHN_RP_ID origin (dash.*) for the ceremony to validate, so in prod the
dash server block proxies to this backend. The full PWA later supersedes
this by serving its static bundle at the same origin; this route then just
becomes an unused fallback.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["enroll"])

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<title>AZKT Command — Enroll</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:#0b0f14; color:#e6edf3; display:flex; min-height:100vh;
    align-items:center; justify-content:center; padding:24px; }
  .card { width:100%; max-width:380px; background:#11161d; border:1px solid #1e2630;
    border-radius:16px; padding:28px; }
  h1 { font-size:34px; margin:0 0 2px; letter-spacing:.5px; }
  .sub { color:#7d8896; margin:0 0 22px; font-size:14px; }
  label { display:block; font-size:13px; color:#9aa6b2; margin:0 0 6px; }
  input { width:100%; padding:13px 14px; border-radius:10px; border:1px solid #2a333f;
    background:#0b0f14; color:#e6edf3; font-size:16px; }
  button { width:100%; margin-top:18px; padding:14px; border:0; border-radius:10px;
    background:#1f9d55; color:#fff; font-size:16px; font-weight:600; }
  button:disabled { opacity:.5; }
  .msg { margin-top:16px; font-size:14px; padding:11px 13px; border-radius:9px; display:none; }
  .err { background:#2a1416; color:#ff8a8a; border:1px solid #4a1d20; }
  .ok  { background:#10241a; color:#74e0a3; border:1px solid #1f5138; }
  a { color:#74e0a3; }
</style>
</head>
<body>
  <div class="card">
    <h1>AZKT</h1>
    <p class="sub">Arizona Kei Trucks · Command</p>

    <div id="loading" class="sub">Checking this device…</div>

    <div id="setup" style="display:none">
      <label for="tok">One-time setup token (from <code>.env</code>)</label>
      <input id="tok" autocomplete="off" autocapitalize="off" autocorrect="off"
             spellcheck="false" placeholder="SETUP_TOKEN" />
      <button id="create">Create passkey on this device</button>
      <p class="sub" style="margin:14px 0 0">
        Uses Face&nbsp;ID, Touch&nbsp;ID, Windows&nbsp;Hello, or a security key —
        whatever this device supports.
      </p>
    </div>

    <div id="signin" style="display:none">
      <p class="sub" style="margin:0 0 4px">An account already exists on this server.</p>
      <button id="login">Sign in with your passkey</button>
    </div>

    <div id="done" style="display:none">
      <p class="sub" style="margin:0 0 14px">You're signed in.</p>
      <a id="dash" href="/"><button>Open the dashboard</button></a>
      <button id="addkey" style="background:#1e2630;margin-top:10px">
        Add another passkey to this account
      </button>
      <button id="signout" style="background:#2a1416;color:#ff8a8a;margin-top:10px">
        Sign out
      </button>
    </div>

    <div id="m" class="msg"></div>
  </div>
<script>
const $ = (id) => document.getElementById(id);
const show = (cls, html) => { const m=$("m"); m.className="msg "+cls; m.innerHTML=html; m.style.display="block"; };
const view = (which) => {
  for (const id of ["loading","setup","signin","done"])
    $(id).style.display = (id === which) ? "block" : "none";
};

const q = new URLSearchParams(location.search).get("token");

function b64uToBuf(s) {
  s = s.replace(/-/g,"+").replace(/_/g,"/");
  s += "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(s), buf = new Uint8Array(bin.length);
  for (let i=0;i<bin.length;i++) buf[i]=bin.charCodeAt(i);
  return buf.buffer;
}
function bufToB64u(buf) {
  const b = new Uint8Array(buf); let s="";
  for (let i=0;i<b.length;i++) s+=String.fromCharCode(b[i]);
  return btoa(s).replace(/\\+/g,"-").replace(/\\//g,"_").replace(/=+$/,"");
}
async function jp(path, body) {
  const r = await fetch(path, {
    method:"POST", credentials:"include",
    headers:{"Content-Type":"application/json"},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error((await r.text()) || ("HTTP "+r.status));
  return r.json();
}

async function refresh() {
  const s = await fetch("/auth/state", { credentials:"include" }).then(r => r.json());
  if (s.authed) { view("done"); return; }
  if (s.registered) { view("signin"); return; }
  if (q) $("tok").value = q;
  view("setup");
}

// First registration: needs the one-time setup token.
async function doRegister(body) {
  if (!window.PublicKeyCredential) throw new Error("This browser has no passkey support.");
  const opts = await jp("/auth/register/options", body);
  opts.challenge = b64uToBuf(opts.challenge);
  opts.user.id = b64uToBuf(opts.user.id);
  if (opts.excludeCredentials) {
    opts.excludeCredentials = opts.excludeCredentials.map(c => ({ ...c, id: b64uToBuf(c.id) }));
  }
  const cred = await navigator.credentials.create({ publicKey: opts });
  const r = cred.response;
  await jp("/auth/register/verify", {
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment || undefined,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: bufToB64u(r.clientDataJSON),
      attestationObject: bufToB64u(r.attestationObject),
      transports: r.getTransports ? r.getTransports() : [],
    },
  });
}

$("create").onclick = async () => {
  const token = $("tok").value.trim();
  if (!token) { show("err","Enter the setup token first."); return; }
  $("create").disabled = true;
  show("ok","Starting…");
  try {
    await doRegister({ handle:"owner", setup_token:token });
    await refresh();
    show("ok","Passkey created. You're signed in.");
  } catch (e) {
    show("err", (e && e.message) ? e.message : String(e));
    $("create").disabled = false;
  }
};

$("login").onclick = async () => {
  if (!window.PublicKeyCredential) { show("err","This browser has no passkey support."); return; }
  $("login").disabled = true;
  show("ok","Waiting for your passkey…");
  try {
    const opts = await jp("/auth/login/options");
    opts.challenge = b64uToBuf(opts.challenge);
    if (opts.allowCredentials) {
      opts.allowCredentials = opts.allowCredentials.map(c => ({ ...c, id: b64uToBuf(c.id) }));
    }
    const cred = await navigator.credentials.get({ publicKey: opts });
    const r = cred.response;
    await jp("/auth/login/verify", {
      id: cred.id,
      rawId: bufToB64u(cred.rawId),
      type: cred.type,
      clientExtensionResults: cred.getClientExtensionResults(),
      response: {
        clientDataJSON: bufToB64u(r.clientDataJSON),
        authenticatorData: bufToB64u(r.authenticatorData),
        signature: bufToB64u(r.signature),
        userHandle: r.userHandle ? bufToB64u(r.userHandle) : undefined,
      },
    });
    await refresh();
    show("ok","Signed in.");
  } catch (e) {
    show("err", (e && e.message) ? e.message : String(e));
    $("login").disabled = false;
  }
};

// Adding a device while signed in needs no setup token (server requires
// an authenticated session instead).
$("addkey").onclick = async () => {
  $("addkey").disabled = true;
  show("ok","Follow your browser's prompt for the new passkey…");
  try {
    await doRegister({ handle:"owner" });
    show("ok","New passkey added to this account.");
  } catch (e) {
    show("err", (e && e.message) ? e.message : String(e));
  } finally {
    $("addkey").disabled = false;
  }
};

$("signout").onclick = async () => {
  try { await jp("/auth/logout"); } catch (e) { /* ignore */ }
  await refresh();
};

refresh().catch(e => show("err", (e && e.message) ? e.message : String(e)));
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
@router.get("/enroll", response_class=HTMLResponse)
async def enroll_page() -> HTMLResponse:
    return HTMLResponse(_PAGE)
