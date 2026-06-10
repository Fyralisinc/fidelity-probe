"""Ashby live ingestion — the HMAC-signed webhook.

When an entity changes, Ashby POSTs a delivery:

    {"action": "<eventType>", "data": {"<entity>": { …full entity… }}}

authenticated with header ``Ashby-Signature: sha256=<lowercase-hex(HMAC-SHA256(
secret, rawBody))>`` — the ``sha256=`` prefix IS present (it names the algorithm),
computed over the RAW request body, no timestamp / replay window (the same wire
shape as a GitHub ``X-Hub-Signature-256``). This slice verifies the signature,
contract-checks the ``{action, data}`` envelope, and — because Ashby webhooks
carry the full entity — re-fetches via ``<category>.info`` to confirm the entity
resolves.

Built blind from the official contract (developer.ashbyhq.com/docs/authenticating-webhooks).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from flask import Response, request

from ..config import AshbyConfig
from ..fidelity import FidelityReport
from .client import AshbyClient

ENDPOINT = "/webhooks/ashby"

# action -> the entity-kind key its `data` carries (and the .info category to re-fetch).
_ACTION_ENTITY = {
    "applicationSubmit": "application", "applicationUpdate": "application",
    "candidateHire": "application", "candidateStageChange": "application",
    "pushToHRIS": "application", "candidateDelete": "candidate",
    "jobCreate": "job", "jobUpdate": "job",
    "interviewScheduleCreate": "interviewSchedule",
    "interviewScheduleUpdate": "interviewSchedule",
    "offerCreate": "offer", "offerUpdate": "offer", "offerDelete": "offer",
}
# which entity keys we can re-fetch via .info (have a stable id-keyed read)
_REFETCHABLE = {"application", "candidate", "job", "offer", "interview"}


def verify_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of ``Ashby-Signature: sha256=<hex>`` over the RAW body."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _validate_event(body: Any, report: FidelityReport, seen_ok: set) -> str | None:
    """Validate the {action, data} envelope; return the entity kind if present."""
    problems: list[str] = []
    if not isinstance(body, dict):
        report.diverge("protocol", ENDPOINT, "event is not a JSON object")
        return None
    action = body.get("action")
    if not isinstance(action, str) or not action:
        problems.append("missing/empty `action`")
    data = body.get("data")
    if not isinstance(data, dict):
        problems.append("`data` must be an object")
        data = {}
    entity_key = _ACTION_ENTITY.get(action) if isinstance(action, str) else None
    if entity_key and entity_key not in data:
        problems.append(f"`data` missing the `{entity_key}` entity for action {action!r}")
    check = "Ashby webhook {action, data} envelope contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")
    return entity_key


def register(server, cfg: AshbyConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    client = AshbyClient(cfg, report)
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def ashby_webhook():  # noqa: ANN202
        raw = request.get_data()
        sig = request.headers.get("Ashby-Signature")
        valid = verify_signature(secret, raw, sig)
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "Ashby-Signature mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        entity_key = _validate_event(body, report, seen_ok)
        action = body.get("action") if isinstance(body, dict) else None
        report.record_live_event("ashby.object", f"action={action}")
        report.count(f"event:{action}")

        # Fetch-on-notify: Ashby carries the full entity, but re-fetch via .info to
        # confirm it resolves (and exercise the .info read).
        if entity_key in _REFETCHABLE:
            entity = (body.get("data") or {}).get(entity_key) or {}
            ent_id = entity.get("id")
            if ent_id:
                st, _, resp = client.get_entity(entity_key, ent_id)
                if (st == 200 and isinstance(resp, dict) and resp.get("success") is True
                        and isinstance(resp.get("results"), dict)
                        and resp["results"].get("id") == ent_id):
                    report.count(f"refetched:{entity_key}")
                    report.record_protocol("Ashby fetch-on-notify .info re-fetch", True, "")
                else:
                    report.record_protocol("Ashby fetch-on-notify .info re-fetch", False,
                                           f"re-fetch {entity_key} {ent_id} -> {st}")
        return Response("", status=200)
