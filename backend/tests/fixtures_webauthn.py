"""A software passkey, so the sign-in and add-a-device ceremonies are tested for real.

`soft_webauthn.SoftWebauthnDevice` speaks raw bytes and never sets the user-verified flag; AZKT
requires user verification (Face ID / Touch ID / PIN) on every ceremony and speaks the base64url
JSON the browser sends. `SoftPasskey` bridges both: one instance is one device holding one passkey,
so "desktop" and "phone" in a test are two instances.
"""
from __future__ import annotations

import base64
import json
from struct import pack

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fido2.utils import sha256
from soft_webauthn import SoftWebauthnDevice

UP_UV_AT = b"\x45"  # user present + user verified + attested credential data
UP_UV = b"\x05"     # user present + user verified


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftPasskey(SoftWebauthnDevice):
    """One device, one passkey. Call `register(options_json, origin)` then `authenticate(...)`."""

    def __init__(self, transports: list[str] | None = None):
        super().__init__()
        self.transports = transports or ["internal"]

    # ── registration ────────────────────────────────────────────────────────
    def register(self, options: dict, origin: str) -> dict:
        """Browser JSON options in, the JSON attestation the frontend posts back out."""
        attestation = self.create({"publicKey": {
            **options,
            "challenge": unb64(options["challenge"]),
            "user": {**options["user"], "id": unb64(options["user"]["id"])},
            "attestation": options.get("attestation") or "none",
        }}, origin)
        return {
            "id": b64(attestation["rawId"]), "rawId": b64(attestation["rawId"]), "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64(attestation["response"]["clientDataJSON"]),
                "attestationObject": b64(attestation["response"]["attestationObject"]),
                "transports": self.transports,
            },
        }

    def create(self, options, origin):  # noqa: D102 - only to mark the credential user-verified
        attestation = super().create(options, origin)
        att = attestation["response"]["attestationObject"]
        from fido2 import cbor
        decoded = cbor.decode(att)
        auth_data = decoded["authData"]
        decoded["authData"] = auth_data[:32] + UP_UV_AT + auth_data[33:]
        attestation["response"]["attestationObject"] = cbor.encode(decoded)
        return attestation

    # ── authentication ──────────────────────────────────────────────────────
    def authenticate(self, options: dict, origin: str) -> dict:
        assertion = self.get({"publicKey": {**options, "challenge": unb64(options["challenge"])}}, origin)
        return {
            "id": b64(assertion["rawId"]), "rawId": b64(assertion["rawId"]), "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64(assertion["response"]["clientDataJSON"]),
                "authenticatorData": b64(assertion["response"]["authenticatorData"]),
                "signature": b64(assertion["response"]["signature"]),
                "userHandle": b64(assertion["response"]["userHandle"]) if assertion["response"].get("userHandle") else None,
            },
        }

    def get(self, options, origin):  # noqa: D102 - same, for the signed assertion
        if self.rp_id != options["publicKey"]["rpId"]:
            raise ValueError("Requested rpID does not match current credential")
        self.sign_count += 1
        client_data = json.dumps({
            "type": "webauthn.get",
            "challenge": base64.urlsafe_b64encode(options["publicKey"]["challenge"]).decode("ascii").rstrip("="),
            "origin": origin,
        }).encode()
        authenticator_data = sha256(self.rp_id.encode("ascii")) + UP_UV + pack(">I", self.sign_count)
        signature = self.private_key.sign(authenticator_data + sha256(client_data), ec.ECDSA(hashes.SHA256()))
        return {
            "id": base64.urlsafe_b64encode(self.credential_id), "rawId": self.credential_id, "type": "public-key",
            "response": {"authenticatorData": authenticator_data, "clientDataJSON": client_data,
                         "signature": signature, "userHandle": self.user_handle},
        }
