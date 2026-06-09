"""Gmail live ingestion — Cloud Pub/Sub push with OIDC-JWT verification.

Gmail is the only source whose live ingress is a Google Pub/Sub *push*, not a
content webhook. Google delivers, to the subscription's push endpoint, an envelope

  { "message": { "data": "<base64 of {emailAddress, historyId}>",
                 "messageId": ..., "publishTime": ... },
    "subscription": "projects/.../subscriptions/..." }

authenticated with an OIDC JWT in `Authorization: Bearer <jwt>` (RS256 against
Google's JWKS; iss ∈ {accounts.google.com, https://accounts.google.com}, aud ==
the configured audience, email == the push service account, email_verified ==
true). The notification carries NO message content — only {emailAddress,
historyId} — so it is a *trigger*: the consumer must call users.history.list(
startHistoryId) → users.messages.get to fetch the actual new message
("fetch-on-notify"). This slice does exactly that, then schema-validates the
fetched Message, so it exercises the full production-faithful live path.

Verification is done blind from the official contract (developers.google.com/
workspace/gmail/api/guides/push + the Pub/Sub authenticate-push docs).
"""
from __future__ import annotations

import base64
import json
from typing import Any

import jwt
import requests
from flask import Response, request

from ..config import GoogleConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from ..webhook_server import WebhookServer
from . import auth, transport

ENDPOINT = "/webhooks/gmail/pubsub"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_VALID_ISS = {"accounts.google.com", "https://accounts.google.com"}


def _b64decode(s: str) -> bytes:
    # Gmail's data field is base64 (std or url-safe); tolerate both + missing pad.
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def _verify_oidc(token: str, cfg: GoogleConfig, jwks: dict) -> tuple[bool, str]:
    """Verify the Pub/Sub push OIDC JWT against the JWKS + the expected claims."""
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001
        return False, f"unparseable JWT header: {exc}"
    kid = header.get("kid")
    key_jwk = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key_jwk is None:
        return False, f"no JWKS key for kid={kid!r}"
    try:
        pub = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_jwk))
        claims = jwt.decode(token, pub, algorithms=["RS256"],
                            audience=cfg.pubsub_oidc_audience)
    except Exception as exc:  # noqa: BLE001
        return False, f"JWT verification failed: {exc}"
    if claims.get("iss") not in _VALID_ISS:
        return False, f"bad iss {claims.get('iss')!r}"
    if cfg.pubsub_oidc_sa and claims.get("email") != cfg.pubsub_oidc_sa:
        return False, f"email {claims.get('email')!r} != expected push SA"
    if claims.get("email_verified") is not True:
        return False, "email_verified is not true"
    return True, ""


def _fetch_back(cfg: GoogleConfig, key_pem: str, email: str, history_id: int,
                sv: SpecValidator, report: FidelityReport, session: requests.Session) -> None:
    """Fetch-on-notify: history.list(startHistoryId) → messages.get → validate."""
    token = auth.fetch_token(cfg, key_pem, cfg.gmail_token_url, email, GMAIL_SCOPE,
                             report, session=session)
    base = f"{cfg.gmail_base}/users/{email}"
    # The notification's historyId is the NEW high-water; list changes since just
    # before it to capture the added message(s). For a mailbox's first-ever message
    # (historyId == 1) the floor is 0 so the change isn't excluded.
    start = max(0, history_id - 1)
    status, _, body = transport.get(session, f"{base}/history", token, "gmail.history.list",
                                    report, {"startHistoryId": start,
                                             "historyTypes": "messageAdded"})
    if status != 200 or not isinstance(body, dict):
        report.diverge("protocol", "gmail.history.list",
                       f"history.list -> {status}; {str(body)[:140]}")
        return
    added_ids = [a["message"]["id"]
                 for rec in (body.get("history") or [])
                 for a in (rec.get("messagesAdded") or []) if a.get("message", {}).get("id")]
    if not added_ids:
        report.record_protocol("Gmail fetch-on-notify yields the added message", False,
                               "history.list returned no messagesAdded for the notified historyId")
        return
    for mid in added_ids:
        st2, _, full = transport.get(session, f"{base}/messages/{mid}", token,
                                     "gmail.messages.get", report, {"format": "full"})
        if st2 != 200:
            report.diverge("protocol", "gmail.messages.get", f"message {mid} -> {st2}")
            continue
        sv.validate_against_component(full, "Message", report)
        report.count("live_message")
    report.record_protocol("Gmail fetch-on-notify yields the added message", True, "")


def register(server: WebhookServer, cfg: GoogleConfig, report: FidelityReport) -> None:
    if not cfg.pubsub_oidc_audience:
        raise SystemExit("GMAIL_PUBSUB_OIDC_AUDIENCE is required to verify Pub/Sub push tokens.")
    sv = SpecValidator("gmail")
    key_pem = auth.resolve_key(cfg, report)
    session = requests.Session()
    jwks = requests.get(cfg.jwks_url, timeout=10).json()
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def gmail_pubsub():  # noqa: ANN202
        authz = request.headers.get("Authorization", "")
        token = authz[7:] if authz.startswith("Bearer ") else ""
        valid, detail = _verify_oidc(token, cfg, jwks)
        report.record_signature(ENDPOINT, valid, "" if valid else detail)
        if not valid:
            return Response("invalid oidc token", status=401)

        raw = request.get_data()
        try:
            env = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "push body is not JSON")
            return Response("bad body", status=400)

        # Envelope contract.
        problems: list[str] = []
        msg = env.get("message")
        if not isinstance(msg, dict):
            problems.append("missing message object")
        else:
            for k in ("data", "messageId", "publishTime"):
                if not msg.get(k):
                    problems.append(f"message.{k} missing")
        if not env.get("subscription"):
            problems.append("subscription missing")
        notif: dict[str, Any] = {}
        if isinstance(msg, dict) and msg.get("data"):
            try:
                notif = json.loads(_b64decode(msg["data"]))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"data is not base64 JSON: {exc}")
        if notif and set(notif) != {"emailAddress", "historyId"}:
            problems.append(f"notification carries unexpected keys {sorted(notif)} "
                            "(must be exactly emailAddress + historyId — no message content)")
        check = "Gmail Pub/Sub push envelope contract"
        if problems:
            report.record_protocol(check, False, "; ".join(problems))
            return Response("", status=200)  # ack so Pub/Sub doesn't storm-retry
        if check not in seen_ok:
            seen_ok.add(check)
            report.record_protocol(check, True, "")

        email = notif["emailAddress"]
        history_id = int(notif["historyId"])
        report.record_live_event("gmail.pubsub", f"email={email} historyId={history_id}")
        _fetch_back(cfg, key_pem, email, history_id, sv, report, session)
        return Response("", status=200)
