"""Jira live ingestion — dynamic-webhook delivery with X-Hub-Signature verification.

Production-faithful per Atlassian's webhook security docs
(https://developer.atlassian.com/cloud/jira/platform/webhooks/): a dynamic /
REST-registered webhook created with a **secret** signs the *raw* request body
with HMAC-SHA256 and presents it as ``X-Hub-Signature: sha256=<hexdigest>`` (the
WebSub ``method=signature`` form — the same scheme GitHub uses, but under the
un-suffixed header name; the algorithm lives in the prefix). There is **no
timestamp envelope** (contrast Slack), so we recompute over the raw bytes and
constant-time compare *before* trusting the payload.

Schema note: Atlassian does not publish a machine-readable schema for webhook
*event* payloads, so — like the Slack/GitHub live slices — events are verified
and structurally summarized, not OpenAPI-validated. Instead we assert the exact
fields a consumer depends on per ``webhookEvent`` (the dedup keys, the
state-transition changelog, the site host in ``issue.self``), and the integer
epoch-ms ``timestamp`` (a known fidelity trap — the embedded ``issue``/``comment``
objects use the ``.SSS+0000`` string format, but the envelope ``timestamp`` does
NOT).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import urlparse

from flask import Response, request

from ..config import JiraConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer

ENDPOINT = "/webhooks/jira"

# The webhookEvent values a Jira dynamic webhook delivers (issue + comment scope).
_KNOWN_EVENTS = {
    "jira:issue_created", "jira:issue_updated", "jira:issue_deleted",
    "comment_created", "comment_updated", "comment_deleted",
}


def verify_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of ``X-Hub-Signature`` (``sha256=<hexdigest>``)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _missing(body: dict, *paths: str) -> list[str]:
    """Return the dotted paths absent/empty in ``body``."""
    out: list[str] = []
    for path in paths:
        cur: Any = body
        ok = True
        for seg in path.split("."):
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                ok = False
                break
        if not ok or cur is None or cur == "":
            out.append(path)
    return out


def _validate_event(event: str, body: dict, report: FidelityReport, seen_ok: set) -> None:
    """Assert the consumer-critical fields per webhookEvent; diverge on anything missing."""
    problems: list[str] = []

    # Envelope `timestamp` MUST be an integer epoch-ms — not a `.SSS+0000` string.
    ts = body.get("timestamp")
    if not isinstance(ts, int) or isinstance(ts, bool):
        problems.append(f"timestamp is not an integer epoch-ms (got {type(ts).__name__})")

    if event.startswith("comment_"):
        problems += _missing(body, "comment.id", "comment.author", "comment.body",
                             "issue.self")
    elif event.startswith("jira:issue_"):
        # The tenant is resolved from the site host in issue.self — it must be a
        # real https URL the consumer can parse a host out of.
        problems += _missing(body, "issue.id", "issue.key", "issue.self", "issue.fields")
        self_url = (body.get("issue") or {}).get("self")
        if isinstance(self_url, str) and not urlparse(self_url).netloc:
            problems.append("issue.self has no host to resolve the tenant from")
        # A status/resolution change must carry the transition in changelog.items.
        if event == "jira:issue_updated" and "changelog" in body:
            items = (body.get("changelog") or {}).get("items")
            if not isinstance(items, list) or not items:
                problems.append("issue_updated changelog.items missing/empty")
            else:
                for it in items:
                    if "field" not in it or ("toString" not in it and "to" not in it):
                        problems.append("changelog item missing field/toString")
                        break
    else:
        report.note(f"live {event}: no structural contract asserted (recorded only)")
        return

    check = f"live {event} payload contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def register(server: WebhookServer, cfg: JiraConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def jira_events():  # noqa: ANN202
        raw = request.get_data()  # raw bytes — required for a correct HMAC
        sig = request.headers.get("X-Hub-Signature")
        valid = verify_signature(secret, raw, sig)
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "X-Hub-Signature mismatch / missing")
        if not valid:
            # Real Jira's digest is over the body alone (no replay window); a bad
            # signature is rejected before the payload is trusted.
            return Response("invalid signature", status=401)

        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        event = body.get("webhookEvent", "unknown")
        if event not in _KNOWN_EVENTS and f"unknown:{event}" not in seen_ok:
            seen_ok.add(f"unknown:{event}")
            report.record_protocol("known webhookEvent", False,
                                   f"unrecognized webhookEvent {event!r}")

        _validate_event(event, body, report, seen_ok)

        issue = body.get("issue") or {}
        summary = (f"event={event} issue={issue.get('key')} "
                   f"status={((issue.get('fields') or {}).get('status') or {}).get('name')}")
        report.record_live_event(event, summary)
        report.count(f"event:{event}", 1)
        return Response("", status=200)
