"""End-to-end auth ceremony test (no browser needed).

Replicates EXACTLY what frontend/enroll.html does over the wire — same
base64url codec, same JSON payload shape — using a software authenticator,
then exercises the existing login flow so we prove a freshly enrolled
passkey can actually sign in. Run against a live server on :8799.
"""
import base64
import json
import sys

import httpx
import soft_webauthn as sw
from soft_webauthn import SoftWebauthnDevice

BASE = "https://localhost:8799"
ORIGIN = "https://localhost:8799"


class UVDevice(SoftWebauthnDevice):
    """soft-webauthn never sets the User-Verified flag; a real platform
    authenticator sets it after Face ID / Touch ID / Windows Hello. The
    backend requires UV, so simulate the biometric by flipping that bit
    (UP|UV|AT = 0x45 on create, UP|UV = 0x05 on get)."""

    def create(self, options, origin):
        self.cred_init(options["publicKey"]["rp"]["id"],
                       options["publicKey"]["user"]["id"])
        client_data = {
            "type": "webauthn.create",
            "challenge": sw.urlsafe_b64encode(
                options["publicKey"]["challenge"]).decode("ascii").rstrip("="),
            "origin": origin,
        }
        rp_id_hash = sw.sha256(self.rp_id.encode("ascii"))
        flags = b"\x45"
        sign_count = sw.pack(">I", self.sign_count)
        cred_id_len = sw.pack(">H", len(self.credential_id))
        cose_key = sw.cbor.encode(
            sw.ES256.from_cryptography_key(self.private_key.public_key()))
        attestation_object = {
            "authData": rp_id_hash + flags + sign_count + self.aaguid
            + cred_id_len + self.credential_id + cose_key,
            "fmt": "none",
            "attStmt": {},
        }
        return {
            "id": sw.urlsafe_b64encode(self.credential_id),
            "rawId": self.credential_id,
            "response": {
                "clientDataJSON": sw.json.dumps(client_data).encode("utf-8"),
                "attestationObject": sw.cbor.encode(attestation_object),
            },
            "type": "public-key",
        }

    def get(self, options, origin):
        if self.rp_id != options["publicKey"]["rpId"]:
            raise ValueError("Requested rpID does not match current credential")
        self.sign_count += 1
        client_data = sw.json.dumps({
            "type": "webauthn.get",
            "challenge": sw.urlsafe_b64encode(
                options["publicKey"]["challenge"]).decode("ascii").rstrip("="),
            "origin": origin,
        }).encode("utf-8")
        client_data_hash = sw.sha256(client_data)
        rp_id_hash = sw.sha256(self.rp_id.encode("ascii"))
        flags = b"\x05"
        sign_count = sw.pack(">I", self.sign_count)
        authenticator_data = rp_id_hash + flags + sign_count
        signature = self.private_key.sign(
            authenticator_data + client_data_hash,
            sw.ec.ECDSA(sw.hashes.SHA256()))
        return {
            "id": sw.urlsafe_b64encode(self.credential_id),
            "rawId": self.credential_id,
            "response": {
                "authenticatorData": authenticator_data,
                "clientDataJSON": client_data,
                "signature": signature,
                "userHandle": self.user_handle,
            },
            "type": "public-key",
        }


def b64u_to_bytes(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def bytes_to_b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def main():
    c = httpx.Client(base_url=BASE, timeout=10, verify=False)

    # 1. Enroll page serves, build-free, and references the real endpoints.
    r = c.get("/enroll")
    assert r.status_code == 200, r.status_code
    html = r.text
    for needle in ("/auth/register/options", "/auth/register/verify",
                   "navigator.credentials.create", "b64uToBuf", "bufToB64u"):
        if needle not in html:
            fail(f"enroll page missing {needle!r}")
    if "<script" not in html or "main.tsx" in html:
        fail("enroll page should be self-contained (no Vite bundle)")
    print("OK  /enroll serves a self-contained page wired to real endpoints")

    # bare domain also works during the interim
    assert c.get("/").status_code == 200
    print("OK  / serves the same page (bare dash domain works)")

    # 2. Fresh DB: not registered yet.
    assert c.get("/auth/state").json() == {"registered": False}

    # 3. Wrong setup token is rejected.
    bad = c.post("/auth/register/options",
                 json={"handle": "owner", "setup_token": "nope"})
    assert bad.status_code == 403, bad.status_code
    print("OK  bad setup token -> 403")

    dev = UVDevice()

    # 4. register/options with the real token (what the page POSTs).
    r = c.post("/auth/register/options",
               json={"handle": "owner", "setup_token": "test-setup-token"})
    assert r.status_code == 200, (r.status_code, r.text)
    opts = r.json()
    assert "reg_chal" in c.cookies, "reg_chal cookie not set"

    # 5. Exactly the b64url->buffer decode the page does in JS.
    pk = {
        "challenge": b64u_to_bytes(opts["challenge"]),
        "rp": opts["rp"],
        "user": {**opts["user"], "id": b64u_to_bytes(opts["user"]["id"])},
        "pubKeyCredParams": opts["pubKeyCredParams"],
    }
    if opts.get("excludeCredentials"):
        pk["excludeCredentials"] = [
            {**x, "id": b64u_to_bytes(x["id"])} for x in opts["excludeCredentials"]
        ]
    att = dev.create({"publicKey": pk}, ORIGIN)

    # 6. Serialize the credential exactly as enroll.html does.
    payload = {
        "id": bytes_to_b64u(att["rawId"]),
        "rawId": bytes_to_b64u(att["rawId"]),
        "type": att["type"],
        "clientExtensionResults": {},
        "response": {
            "clientDataJSON": bytes_to_b64u(att["response"]["clientDataJSON"]),
            "attestationObject": bytes_to_b64u(att["response"]["attestationObject"]),
            "transports": ["internal"],
        },
    }
    r = c.post("/auth/register/verify", json=payload)
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json() == {"ok": True}
    assert "azkt_session" in c.cookies, "session cookie not issued"
    print("OK  register/verify accepts the page's payload + issues a session")

    # 7. The session actually authorizes a protected route.
    assert c.get("/auth/state").json() == {"registered": True}
    prot = c.get("/api/agents")
    assert prot.status_code != 401, "session did not authorize /api/agents"
    print("OK  enrolled session authorizes a protected route")

    # 8. Second registration without a session is closed (single user).
    closed = httpx.Client(base_url=BASE, verify=False).post(
        "/auth/register/options",
        json={"handle": "owner", "setup_token": "test-setup-token"})
    assert closed.status_code == 401, closed.status_code
    print("OK  registration closes after first passkey (needs a session)")

    # 9. LOGIN with the enrolled passkey (usernameless: no allowCredentials).
    fresh = httpx.Client(base_url=BASE, timeout=10, verify=False)
    r = fresh.post("/auth/login/options")
    assert r.status_code == 200, r.text
    lo = r.json()
    assert "auth_chal" in fresh.cookies
    if lo.get("allowCredentials"):
        fail("login sends allowCredentials but design is usernameless "
             "-> credential MUST be discoverable/resident")
    gpk = {
        "challenge": b64u_to_bytes(lo["challenge"]),
        "rpId": lo["rpId"],
        "userVerification": lo.get("userVerification", "preferred"),
    }
    asr = dev.get({"publicKey": gpk}, ORIGIN)
    login_payload = {
        "id": bytes_to_b64u(asr["rawId"]),
        "rawId": bytes_to_b64u(asr["rawId"]),
        "type": asr["type"],
        "clientExtensionResults": {},
        "response": {
            "clientDataJSON": bytes_to_b64u(asr["response"]["clientDataJSON"]),
            "authenticatorData": bytes_to_b64u(asr["response"]["authenticatorData"]),
            "signature": bytes_to_b64u(asr["response"]["signature"]),
            "userHandle": bytes_to_b64u(asr["response"]["userHandle"])
            if asr["response"].get("userHandle") else None,
        },
    }
    r = fresh.post("/auth/login/verify", json=login_payload)
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json() == {"ok": True}
    assert "azkt_session" in fresh.cookies
    assert fresh.get("/api/agents").status_code != 401
    print("OK  enrolled passkey logs in (usernameless) + authorizes")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
