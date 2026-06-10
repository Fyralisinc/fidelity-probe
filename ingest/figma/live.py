"""Figma live ingestion — the Webhooks-v2 **body-PASSCODE** delivery (NO HMAC).

When a watched Figma file changes, Figma POSTs a Webhooks-v2 delivery whose
authenticity is a **plaintext ``passcode`` carried as a top-level JSON field in the
body** — there is NO signature header and NO HMAC
(developers.figma.com/docs/rest-api/webhooks-security): "compare the ``passcode``
we pass back to you … with the ``passcode`` originally provided … a wrong passcode
→ respond 400". So this slice constant-time-compares ``body["passcode"]`` and
returns **400** (not 401) on a mismatch — the documented contract.

Every delivery shares ``{event_type, passcode, timestamp, webhook_id}``. The two
ingestion-relevant events are metadata-ish, so the slice fetch-on-notify correlates:
  * FILE_VERSION_UPDATE → ``GET /v1/files/{key}/versions`` and match ``version_id``;
  * FILE_COMMENT        → ``GET /v1/files/{key}/comments`` and match ``comment_id``.
A PING is explicitly NOT an observation.

Built blind from the official Figma webhook contract.
"""
from __future__ import annotations

import hmac
import json
from typing import Any

from flask import Response, request

from ..config import FigmaConfig
from ..fidelity import FidelityReport
from .client import FigmaClient

ENDPOINT = "/webhooks/figma"
_KNOWN_EVENTS = {
    "PING", "FILE_UPDATE", "FILE_VERSION_UPDATE", "FILE_DELETE", "FILE_COMMENT",
    "LIBRARY_PUBLISH", "DEV_MODE_STATUS_UPDATE",
}
_OBSERVABLE = {"FILE_VERSION_UPDATE", "FILE_COMMENT", "FILE_UPDATE", "FILE_DELETE",
               "LIBRARY_PUBLISH", "DEV_MODE_STATUS_UPDATE"}


def verify_passcode(expected: str, body: Any) -> bool:
    """Constant-time compare of the body's top-level ``passcode`` (Figma's whole auth)."""
    if not isinstance(body, dict):
        return False
    got = body.get("passcode")
    if not isinstance(got, str):
        return False
    return hmac.compare_digest(expected, got)


def _validate_envelope(body: Any, report: FidelityReport, seen_ok: set) -> dict:
    """Validate the base ``{event_type, passcode, timestamp, webhook_id}`` envelope."""
    problems: list[str] = []
    if not isinstance(body, dict):
        report.diverge("protocol", ENDPOINT, "event is not a JSON object")
        return {}
    if body.get("event_type") not in _KNOWN_EVENTS:
        problems.append(f"unknown `event_type` {body.get('event_type')!r}")
    if not isinstance(body.get("passcode"), str):
        problems.append("`passcode` must be a string (the body-passcode auth)")
    if not isinstance(body.get("timestamp"), str):
        problems.append("`timestamp` must be a string")
    if not isinstance(body.get("webhook_id"), str):
        problems.append("`webhook_id` must be a string")
    check = "Figma webhook base envelope (event_type, passcode, timestamp, webhook_id)"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")
    return body


def register(server, cfg: FigmaConfig, report: FidelityReport) -> None:
    passcode = cfg.require_webhook_passcode()
    client = FigmaClient(cfg, report)
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def figma_webhook():  # noqa: ANN202
        raw = request.get_data()
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        # Figma auth = a plaintext body passcode (NO signature header). A wrong
        # passcode → respond 400 (the documented contract), NOT 401.
        valid = verify_passcode(passcode, body)
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "body passcode mismatch / missing")
        if not valid:
            return Response("invalid passcode", status=400)

        _validate_envelope(body, report, seen_ok)
        etype = body.get("event_type") if isinstance(body, dict) else None
        if etype == "PING":
            report.record_protocol("PING is acknowledged but is NOT an observation", True, "")
            report.record_live_event("figma.ping", "PING")
            return Response("", status=200)
        if etype in _OBSERVABLE:
            report.record_live_event("figma.event", f"{etype} file={body.get('file_key')}")
            report.count(f"event:{etype}")
            file_key = body.get("file_key")
            # Fetch-on-notify: re-fetch the changed object to correlate.
            if etype == "FILE_VERSION_UPDATE" and file_key:
                vid = body.get("version_id")
                st, _, vb = client.file_versions(file_key, page_size=50)
                found = (st == 200 and isinstance(vb, dict)
                         and any(v.get("id") == vid for v in vb.get("versions", [])))
                report.record_protocol("Figma fetch-on-notify version_id correlation",
                                       found, "" if found else f"version {vid} not found")
                if found:
                    report.count("correlated:version")
            elif etype == "FILE_COMMENT" and file_key:
                cid = body.get("comment_id")
                st, _, cb = client.file_comments(file_key)
                found = (st == 200 and isinstance(cb, dict)
                         and any(c.get("id") == cid for c in cb.get("comments", [])))
                report.record_protocol("Figma fetch-on-notify comment_id correlation",
                                       found, "" if found else f"comment {cid} not found")
                if found:
                    report.count("correlated:comment")
        return Response("", status=200)
