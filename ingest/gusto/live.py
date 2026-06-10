"""Gusto live ingestion — the X-Gusto-Signature webhook.

When a payroll is processed / an employee changes, Gusto POSTs a THIN event:

  {"uuid": <event id>, "event_type": "payroll.processed",
   "resource_type": "Payroll", "resource_uuid": <payroll uuid>,
   "entity_type": "Company", "entity_uuid": <company uuid>,
   "timestamp": 1671058841}              # a numeric Unix EPOCH (not ISO)

signed with a single header (docs.gusto.com/embedded-payroll/docs/webhooks):

  X-Gusto-Signature: <lowercase hex HMAC-SHA256(verification_token, rawBody)>

— NO ``sha256=`` prefix, NO timestamp in the signed bytes (GitHub's
``X-Hub-Signature-256`` shape minus the prefix). The secret is the subscription's
``verification_token``.

This slice verifies the signature, contract-checks the thin envelope, and —
because the event carries only references — fetch-on-notify correlates
``resource_uuid`` against ``GET /v1/companies/{co}/payrolls/{uuid}``. Built blind
from the official Gusto contract. (The hex-vs-base64 encoding is the one INFERRED
detail; the slice defaults to hex.)
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from flask import Response, request

from ..config import GustoConfig
from ..fidelity import FidelityReport
from .client import GustoClient

ENDPOINT = "/webhooks/gusto"
_TOP_KEYS = {"uuid", "event_type", "resource_type", "resource_uuid",
             "entity_type", "entity_uuid", "timestamp"}
_KNOWN_EVENTS = {
    "payroll.created", "payroll.calculated", "payroll.submitted",
    "payroll.processed", "payroll.paid", "payroll.cancelled",
    "employee.created", "employee.updated", "employee.onboarded",
    "employee.terminated", "employee.rehired",
}


def verify_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of ``X-Gusto-Signature`` = hex HMAC-SHA256(secret, body)."""
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
    if body.get("event_type") not in _KNOWN_EVENTS:
        problems.append(f"unknown event_type {body.get('event_type')!r}")
    # timestamp is a numeric Unix EPOCH (NOT an ISO string)
    if not isinstance(body.get("timestamp"), int) or isinstance(body.get("timestamp"), bool):
        problems.append(f"timestamp must be a numeric Unix epoch: {body.get('timestamp')!r}")
    if not isinstance(body.get("resource_uuid"), str):
        problems.append("resource_uuid must be a string")
    if body.get("entity_type") != "Company":
        problems.append(f"entity_type should be Company: {body.get('entity_type')!r}")
    check = "Gusto thin event envelope contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def register(server, cfg: GustoConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    client = GustoClient(cfg, report)
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def gusto_webhook():  # noqa: ANN202
        raw = request.get_data()
        valid = verify_signature(secret, raw, request.headers.get("X-Gusto-Signature"))
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "X-Gusto-Signature mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        _validate_event(body, report, seen_ok)
        if isinstance(body, dict) and body.get("event_type") in _KNOWN_EVENTS:
            etype = body["event_type"]
            ruid = body.get("resource_uuid")
            rtype = body.get("resource_type")
            report.record_live_event("gusto.event", f"{etype} resource_uuid={ruid}")
            report.count(f"event:{etype}")
            # Fetch-on-notify: the thin event carries only references.
            if rtype == "Payroll" and ruid:
                st, _, payroll = client.get_payroll(ruid)
                if st == 200 and isinstance(payroll, dict) and payroll.get("uuid") == ruid:
                    report.count("correlated:payroll")
                    report.record_protocol("Gusto fetch-on-notify payroll correlation", True, "")
                else:
                    report.record_protocol("Gusto fetch-on-notify payroll correlation", False,
                                           f"payroll {ruid} not fetchable (-> {st})")
            elif rtype == "Employee" and ruid:
                # employees are not single-GET-by-uuid here; re-pull the list and find it.
                st, _, emps = client.list_employees(per=100)
                found = isinstance(emps, list) and any(
                    isinstance(e, dict) and e.get("uuid") == ruid for e in emps)
                report.record_protocol("Gusto fetch-on-notify employee correlation", found,
                                       "" if found else f"employee {ruid} not in directory")
                if found:
                    report.count("correlated:employee")
        return Response("", status=200)
