"""Fireflies live ingestion — the x-hub-signature transcript webhook.

When a meeting transcript completes, Fireflies POSTs a THIN V2 event:

  {"event": "meeting.transcribed",
   "timestamp": <unix MILLISECONDS>,
   "meeting_id": "<transcript id>",
   "client_reference_id": "<optional>"}

signed with one header (docs.fireflies.ai/graphql-api/webhooks-v2):

  x-hub-signature: sha256=<hex HMAC-SHA256(secret, rawBody)>

— the legacy ``x-hub-signature`` header NAME but a SHA-256 digest with the
``sha256=`` prefix, over the raw body alone (no timestamp). This slice verifies the
signature, contract-checks the thin envelope, and — because the event carries only
the meeting id — fetch-on-notify correlates ``meeting_id`` against the GraphQL
``transcript(id:)`` query. Built blind from the official Fireflies contract.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from flask import Response, request

from ..config import FirefliesConfig
from ..fidelity import FidelityReport
from .client import FirefliesClient

ENDPOINT = "/webhooks/fireflies"
# V2 event names (docs.fireflies.ai/graphql-api/webhooks-v2); V1 used
# eventType:"Transcription completed" — both are "a transcript completed".
_KNOWN_EVENTS = {"meeting.transcribed", "meeting.summarized"}


def verify_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of ``x-hub-signature`` = ``sha256=<hex HMAC-SHA256(secret, body)>``."""
    if not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def _validate_event(body: Any, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not isinstance(body, dict):
        report.diverge("protocol", ENDPOINT, "event is not a JSON object")
        return
    if body.get("event") not in _KNOWN_EVENTS:
        problems.append(f"unknown `event` {body.get('event')!r}")
    if not isinstance(body.get("meeting_id"), str):
        problems.append("`meeting_id` must be a string (the transcript id)")
    if "timestamp" in body and not isinstance(body["timestamp"], (int, float)):
        problems.append("`timestamp` must be a Number (unix ms)")
    check = "Fireflies thin webhook envelope contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def register(server, cfg: FirefliesConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    client = FirefliesClient(cfg, report)
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def fireflies_webhook():  # noqa: ANN202
        raw = request.get_data()
        valid = verify_signature(secret, raw, request.headers.get("x-hub-signature"))
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "x-hub-signature mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        _validate_event(body, report, seen_ok)
        if isinstance(body, dict) and body.get("event") in _KNOWN_EVENTS:
            event = body.get("event")
            mid = body.get("meeting_id")
            report.record_live_event("fireflies.transcript", f"{event} meeting_id={mid}")
            report.count(f"event:{event}")
            # Fetch-on-notify: the thin event carries only the meeting id → hydrate
            # the full transcript via the GraphQL transcript(id:) query.
            if mid:
                st, single, errs = client.get_transcript(mid)
                one = ((single or {}).get("data", {}).get("transcript")
                       if isinstance(single, dict) else None)
                if st == 200 and isinstance(one, dict) and one.get("id") == mid:
                    report.count("correlated:transcript")
                    report.record_protocol("Fireflies fetch-on-notify transcript correlation",
                                           True, "")
                else:
                    report.record_protocol("Fireflies fetch-on-notify transcript correlation",
                                           False, f"transcript {mid} not fetchable (-> {st}; "
                                           f"{str(errs)[:80]})")
        return Response("", status=200)
