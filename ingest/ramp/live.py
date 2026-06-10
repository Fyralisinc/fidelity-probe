"""Ramp live ingestion — the X-Ramp-Signature transaction webhook.

When a transaction clears/declines/syncs, Ramp POSTs a THIN event:

  {"id": <event id>, "type": "transactions.cleared",
   "created_at": "2026-…+00:00", "business_id": <uuid>,
   "object": {"id": <transaction id>}}

signed with a single header (docs.ramp.com/.../webhooks):

  X-Ramp-Signature: <bare lowercase hex HMAC-SHA256(secret, rawBody)>

— NO ``sha256=`` / ``v1,`` prefix, NOT base64, NO timestamp in the signed bytes
(the simplest HMAC shape: GitHub's ``X-Hub-Signature-256`` minus the prefix).

This slice verifies the signature, contract-checks the thin envelope, and —
because the event carries only the resource id — fetch-on-notify correlates
``object.id`` against ``GET /developer/v1/transactions/{id}``. Built blind from
the official Ramp contract.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from flask import Response, request

from ..config import RampConfig
from ..fidelity import FidelityReport
from .client import RampClient

ENDPOINT = "/webhooks/ramp"
_TOP_KEYS = {"id", "type", "created_at", "business_id", "object"}
_KNOWN_EVENTS = {
    "transactions.authorized", "transactions.cleared", "transactions.declined",
    "transactions.ready_for_review", "transactions.ready_to_sync",
    "transactions.sync_requested", "transactions.synced",
}


def verify_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of ``X-Ramp-Signature`` = bare hex HMAC-SHA256(secret, body)."""
    if not header:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def _validate_event(body: Any, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not isinstance(body, dict):
        report.diverge("protocol", ENDPOINT, "event is not a JSON object")
        return
    missing = [k for k in _TOP_KEYS if k not in body]
    if missing:
        problems.append(f"event missing top-level keys {sorted(missing)}")
    if body.get("type") not in _KNOWN_EVENTS:
        problems.append(f"unknown event type {body.get('type')!r}")
    if not isinstance(body.get("business_id"), str):
        problems.append("business_id must be a string")
    obj = body.get("object")
    if not (isinstance(obj, dict) and isinstance(obj.get("id"), str)):
        problems.append("event `object` must be a thin {id:<resource id>}")
    check = "Ramp thin event envelope contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def register(server, cfg: RampConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    client = RampClient(cfg, report)
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def ramp_webhook():  # noqa: ANN202
        raw = request.get_data()
        valid = verify_signature(secret, raw, request.headers.get("X-Ramp-Signature"))
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "X-Ramp-Signature mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        _validate_event(body, report, seen_ok)
        if isinstance(body, dict) and body.get("type") in _KNOWN_EVENTS:
            etype = body.get("type")
            obj = body.get("object") or {}
            tid = obj.get("id") if isinstance(obj, dict) else None
            report.record_live_event("ramp.transaction", f"{etype} object.id={tid}")
            report.count(f"event:{etype}")
            # Fetch-on-notify: the thin event carries only the id → fetch the full txn.
            if tid:
                st, _, txn = client.get_transaction(tid)
                if st == 200 and isinstance(txn, dict) and txn.get("id") == tid:
                    report.count("correlated:transaction")
                    report.record_protocol("Ramp fetch-on-notify transaction correlation",
                                           True, "")
                else:
                    report.record_protocol("Ramp fetch-on-notify transaction correlation",
                                           False, f"transaction {tid} not fetchable (-> {st})")
        return Response("", status=200)
