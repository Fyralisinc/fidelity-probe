"""Brex live ingestion — the Svix-signed transfer webhook.

When a transfer is processed/failed, Brex POSTs a thin event:

  {"event_type":"TRANSFER_PROCESSED", "transfer_id":…, "payment_type":…,
   "return_for_id":null, "company_id":…}

signed with **Svix's standard scheme** under Brex's renamed headers
(developer.brex.com/docs/webhooks):

  Webhook-Id:        msg_<id>
  Webhook-Timestamp: <unix_seconds>
  Webhook-Signature: v1,<base64(HMAC-SHA256(key, "{id}.{ts}.{rawBody}"))>

where ``key`` = base64-decode of the ``whsec_…`` secret (multiple space-delimited
``v1,<sig>`` tokens can appear during a rotation; any match passes). The
timestamp is a SEPARATE header, NOT inside the signature (≠ Stripe's t=,v1=).

This slice verifies the signature, contract-checks the transfer envelope, and —
because the event carries only ``transfer_id`` (the full transfer detail lives
behind the Payments API, out of scope) — fetch-on-notify correlates the
``transfer_id`` against the primary cash account's transactions (which expose a
``transfer_id`` field). Built blind from the official Svix + Brex contract.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any

from flask import Response, request

from ..config import BrexConfig
from ..fidelity import FidelityReport
from .client import BrexClient

ENDPOINT = "/webhooks/brex"
_TOP_KEYS = {"event_type", "transfer_id", "company_id"}
_KNOWN_EVENTS = {"TRANSFER_PROCESSED", "TRANSFER_FAILED"}


def verify_signature(secret: str, raw: bytes, *, msg_id: str | None,
                     timestamp: str | None, header: str | None) -> bool:
    """Constant-time Svix verification of ``Webhook-Signature``.

    Recomputes ``base64(HMAC-SHA256(key, "{id}.{ts}.{rawBody}"))`` where ``key``
    is the base64-decode of the secret after its ``whsec_`` prefix, and compares
    against any space-delimited ``v1,<sig>`` token in the header.
    """
    if not header or not msg_id or not timestamp:
        return False
    key_b64 = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    try:
        key = base64.b64decode(key_b64)
    except (ValueError, binascii.Error):
        key = secret.encode()
    signed = f"{msg_id}.{timestamp}.".encode() + raw
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for token in header.split(" "):
        token = token.strip()
        version, _, sig = token.partition(",")
        if version == "v1" and hmac.compare_digest(expected, sig):
            return True
    return False


def _validate_event(body: Any, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not isinstance(body, dict):
        report.diverge("protocol", ENDPOINT, "event is not a JSON object")
        return
    missing = [k for k in _TOP_KEYS if k not in body]
    if missing:
        problems.append(f"event missing top-level keys {sorted(missing)}")
    if body.get("event_type") not in _KNOWN_EVENTS:
        problems.append(f"unknown event_type {body.get('event_type')!r}")
    if not isinstance(body.get("transfer_id"), str):
        problems.append("transfer_id must be a string")
    if not isinstance(body.get("company_id"), str):
        problems.append("company_id must be a string")
    if "return_for_id" not in body:
        problems.append("event missing `return_for_id` key")
    check = "Brex transfer event envelope contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def register(server, cfg: BrexConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    client = BrexClient(cfg, report)
    seen_ok: set = set()
    # Resolve the primary cash account id once for fetch-on-notify correlation.
    _primary = {"id": None}
    st, _, primary = client.get_primary_cash_account()
    if st == 200 and isinstance(primary, dict):
        _primary["id"] = primary.get("id")

    @server.app.post(ENDPOINT)
    def brex_webhook():  # noqa: ANN202
        raw = request.get_data()
        valid = verify_signature(
            secret, raw,
            msg_id=request.headers.get("Webhook-Id"),
            timestamp=request.headers.get("Webhook-Timestamp"),
            header=request.headers.get("Webhook-Signature"))
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "Webhook-Signature (Svix) mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        _validate_event(body, report, seen_ok)
        if isinstance(body, dict) and body.get("event_type") in _KNOWN_EVENTS:
            etype = body.get("event_type")
            tid = body.get("transfer_id")
            report.record_live_event("brex.transfer", f"{etype} transfer_id={tid}")
            report.count(f"event:{etype}")
            # Fetch-on-notify: correlate transfer_id against cash transactions.
            if _primary["id"] and tid:
                _, _, page = client.list_cash_transactions(_primary["id"], limit=100)
                items = page.get("items", []) if isinstance(page, dict) else []
                if any(isinstance(t, dict) and t.get("transfer_id") == tid for t in items):
                    report.count("correlated:transfer")
                    report.record_protocol("Brex fetch-on-notify transfer_id correlation",
                                           True, "")
                else:
                    report.record_protocol("Brex fetch-on-notify transfer_id correlation",
                                           False, f"transfer {tid} not found in cash txns")
        return Response("", status=200)
