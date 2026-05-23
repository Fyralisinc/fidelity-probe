"""GitHub live ingestion — webhook delivery with X-Hub-Signature-256 verification.

Production-faithful: GitHub signs each webhook delivery with HMAC-SHA256 over the
raw request body, keyed by the App's webhook secret, and sends it as
`X-Hub-Signature-256: sha256=<hex>`. We recompute it over the *raw* bytes and
compare in constant time *before* trusting the payload; the event type comes from
the `X-GitHub-Event` header and the delivery id from `X-GitHub-Delivery`.

Schema note: the api.github.com OpenAPI description does not model webhook *event*
payloads (GitHub publishes those in a separate webhooks spec), so — exactly as on
the Slack live slice — events are verified and structurally summarized but not
schema-validated here, because we have no official contract in this spec to hold
them to, and we say so rather than inventing one.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from flask import Response, request

from ..config import GitHubConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer

ENDPOINT = "/github/events"


def verify_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of X-Hub-Signature-256 (`sha256=<hexdigest>`)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def register(server: WebhookServer, cfg: GitHubConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()

    @server.app.post(ENDPOINT)
    def github_events():  # noqa: ANN202
        raw = request.get_data()  # raw bytes — required for a correct HMAC
        sig = request.headers.get("X-Hub-Signature-256")
        valid = verify_signature(secret, raw, sig)
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "X-Hub-Signature-256 mismatch / missing")
        if not valid:
            return Response("invalid signature", status=403)

        event = request.headers.get("X-GitHub-Event", "unknown")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        body = json.loads(raw or b"{}")

        if event == "ping":
            # GitHub's webhook setup handshake.
            report.record_live_event("ping", f"zen={body.get('zen')!r} hook_id={body.get('hook_id')}")
            return Response(json.dumps({"ok": True}), mimetype="application/json")

        action = body.get("action")
        repo = (body.get("repository") or {}).get("full_name")
        summary = f"action={action} repo={repo} delivery={delivery[:8]}"
        report.record_live_event(event, summary)
        report.count(f"event:{event}", 1)
        return Response("", status=200)
