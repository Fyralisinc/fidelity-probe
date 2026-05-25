"""Google service-account auth with domain-wide delegation (DWD).

The real Workspace server-to-server flow (https://developers.google.com/identity/
protocols/oauth2/service-account):

  1. Build a JWT *assertion* signed (RS256) with the service account's private key,
     claims = {iss: <sa email>, sub: <user to impersonate>, scope: <space-delimited>,
               aud: <token endpoint>, iat, exp}. `sub` is the domain-wide-delegation
     hook: it mints a token *as that user* (userId = me/that email).
  2. POST it to the token endpoint as
     grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=<jwt>.
  3. Receive a per-user bearer access token; use it as `Authorization: Bearer …`.

We mint the assertion ourselves with PyJWT (RS256) — exactly what google-auth does
internally — so the wire shape is the official one. The SA key is supplied the way a
real key is (GOOGLE_PRIVATE_KEY / GOOGLE_PRIVATE_KEY_PATH). If none is configured we
generate an ephemeral RSA key and record that fact: the documented flow still runs,
and a target that doesn't verify the assertion signature will accept it.
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ..config import GoogleConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator

_ASSERTION_TTL = 3600
_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def resolve_key(cfg: GoogleConfig, report: FidelityReport) -> str:
    """Return the SA private key PEM, generating an ephemeral one if none configured."""
    if cfg.private_key:
        return cfg.private_key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    report.note("no GOOGLE_PRIVATE_KEY configured; signing the JWT assertion with an "
                "ephemeral key. The OAuth2 service-account flow is still exercised; this "
                "only works against a target that does not verify the assertion signature.")
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def mint_assertion(cfg: GoogleConfig, key_pem: str, sub: str, scope: str,
                   audience: str, *, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    claims = {
        "iss": cfg.service_account_email,
        "sub": sub,  # the impersonated user (domain-wide delegation)
        "scope": scope,
        "aud": audience,
        "iat": now,
        "exp": now + _ASSERTION_TTL,
    }
    tok = jwt.encode(claims, key_pem, algorithm="RS256")
    return tok.decode() if isinstance(tok, bytes) else tok


def fetch_token(cfg: GoogleConfig, key_pem: str, token_url: str, sub: str, scope: str,
                report: FidelityReport, *, session: requests.Session | None = None) -> str:
    """Exchange a signed assertion for a per-user bearer access token."""
    assertion = mint_assertion(cfg, key_pem, sub, scope, token_url)
    body = urlencode({"grant_type": _GRANT, "assertion": assertion})
    s = session or requests
    r = s.post(token_url, data=body,
               headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"token endpoint {token_url} returned {r.status_code}: {r.text[:200]}")
    data = r.json()
    token = data.get("access_token")
    if not token:
        report.diverge("protocol", "token", f"token response missing access_token: {data}")
        raise RuntimeError("token response had no access_token")
    if data.get("token_type", "").lower() != "bearer":
        report.diverge("protocol", "token",
                       f"token_type is {data.get('token_type')!r}, expected 'Bearer'")
    return token
