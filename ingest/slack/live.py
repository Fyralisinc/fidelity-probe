"""Slack live ingestion — Events API webhook with signature verification.

Production-faithful: the raw request body is verified with slack_sdk's
SignatureVerifier (the v0 HMAC scheme over `X-Slack-Request-Timestamp` + body)
*before* the payload is trusted, the url_verification handshake is answered, and
event_callback envelopes are recorded.

Note on schema validation: Slack's official Web API OpenAPI spec does not describe
Events API payloads, so live events are recorded and structurally summarized but
not schema-validated against the spec — we don't have an official contract to
hold them to, and we say so rather than inventing one.
"""
from __future__ import annotations

import json

from flask import Response, request
from slack_sdk.signature import SignatureVerifier

from ..config import SlackConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer

ENDPOINT = "/slack/events"


def register(server: WebhookServer, cfg: SlackConfig, report: FidelityReport) -> None:
    verifier = SignatureVerifier(signing_secret=cfg.require_signing_secret())

    @server.app.post(ENDPOINT)
    def slack_events():  # noqa: ANN202
        raw = request.get_data()  # raw bytes — required for a correct HMAC
        valid = verifier.is_valid_request(raw, dict(request.headers))
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "X-Slack-Signature mismatch / stale timestamp")
        if not valid:
            return Response("invalid signature", status=403)

        body = json.loads(raw or b"{}")
        kind = body.get("type")

        if kind == "url_verification":
            # Events API setup handshake: echo the challenge back verbatim.
            report.record_live_event("url_verification", "challenge answered")
            return Response(json.dumps({"challenge": body.get("challenge")}),
                            mimetype="application/json")

        if kind == "event_callback":
            ev = body.get("event") or {}
            summary = f"{ev.get('type')} ch={ev.get('channel')} ts={ev.get('ts')}"
            report.record_live_event(ev.get("type") or "event", summary)
            report.count(f"event:{ev.get('type')}", 1)
            return Response("", status=200)

        report.record_live_event(kind or "unknown", "non-event payload")
        return Response("", status=200)
