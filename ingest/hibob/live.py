"""HiBob live ingestion — the ``Bob-Signature`` HMAC-SHA512-signed webhook.

When an HR change happens, HiBob POSTs a **Webhooks v2** metadata-only payload
(apidocs.hibob.com/changelog/introducing-bob-webhooks-v2):

  {"companyId": 636192, "type": "employee.updated",
   "triggeredBy": "…", "triggeredAt": "2024-12-30T12:56:18.955603",
   "version": "v2", "data": {"employeeId": "…", "fieldUpdatesIds": [{"id": "root.surname"}]}}

signed (apidocs.hibob.com/reference/getting-started-webhooks) with a **base64
HMAC-SHA512 over the raw body alone**, in the header ``Bob-Signature`` (no prefix,
no timestamp). The ``data`` block is metadata-only (IDs + field-update ids) — NOT
the full object — so this slice fetch-on-notify correlates:
  * employee events → ``POST /v1/people/search`` filtered on ``data.employeeId``;
  * time-off events → ``GET /v1/timeoff/requests/changes`` and match the
    ``data.timeoffRequestId``.

Built blind from the official HiBob webhook contract.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Response, request

from ..config import HibobConfig
from ..fidelity import FidelityReport
from .client import HibobClient

ENDPOINT = "/webhooks/hibob"
_ISO_NOZ_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{1,6}$")
_KNOWN_EVENTS = {
    "employee.created", "employee.updated", "employee.deleted", "employee.joined",
    "employee.left", "employee.activated", "employee.inactivated",
    "timeoff.request.requested", "timeoff.request.approved",
    "timeoff.request.declined", "timeoff.request.cancelled",
}


def verify_signature(secret: str, raw: bytes, *, header: str | None) -> bool:
    """Constant-time verify of ``Bob-Signature`` = base64(HMAC-SHA512(secret, body))."""
    if not header:
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode(), raw, hashlib.sha512).digest()).decode()
    return hmac.compare_digest(expected, header.strip())


def _validate_envelope(body: Any, report: FidelityReport, seen_ok: set) -> dict:
    """Validate the v2 {companyId, type, triggeredBy, triggeredAt, version, data} envelope."""
    problems: list[str] = []
    if not isinstance(body, dict):
        report.diverge("protocol", ENDPOINT, "event is not a JSON object")
        return {}
    if not isinstance(body.get("companyId"), (int, float)) or isinstance(body.get("companyId"), bool):
        problems.append(f"`companyId` must be a number: {body.get('companyId')!r}")
    if body.get("type") not in _KNOWN_EVENTS:
        problems.append(f"unknown `type` {body.get('type')!r}")
    if not isinstance(body.get("triggeredBy"), str):
        problems.append("`triggeredBy` must be a string")
    ta = body.get("triggeredAt")
    if not (isinstance(ta, str) and _ISO_NOZ_RE.match(ta)):
        problems.append(f"`triggeredAt` must be ISO-8601 µs with NO Z: {ta!r}")
    if body.get("version") != "v2":
        problems.append(f"`version` must be 'v2': {body.get('version')!r}")
    data = body.get("data")
    if not isinstance(data, dict):
        problems.append("`data` must be an object")
        data = {}
    check = "HiBob v2 webhook envelope contract (companyId, type, triggeredAt, version, data)"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")
    return data


def register(server, cfg: HibobConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    client = HibobClient(cfg, report)
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def hibob_webhook():  # noqa: ANN202
        raw = request.get_data()
        valid = verify_signature(secret, raw, header=request.headers.get("Bob-Signature"))
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "Bob-Signature mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        data = _validate_envelope(body, report, seen_ok)
        etype = body.get("type") if isinstance(body, dict) else None
        if etype in _KNOWN_EVENTS:
            report.record_live_event("hibob.event", f"{etype} data_keys={sorted(data)}")
            report.count(f"event:{etype}")
            # Fetch-on-notify: the v2 payload is metadata-only, so re-fetch the record.
            if etype.startswith("employee."):
                eid = data.get("employeeId")
                if eid:
                    st, _, fb = client.people_search(
                        filters=[{"fieldPath": "root.id", "operator": "equals",
                                  "values": [str(eid)]}], show_inactive=True)
                    found = (st == 200 and isinstance(fb, dict)
                             and any(e.get("id") == str(eid)
                                     for e in fb.get("employees", [])))
                    report.record_protocol("HiBob fetch-on-notify employee id correlation",
                                           found, "" if found else f"employee {eid} not found")
                    if found:
                        report.count("correlated:employee")
            elif etype.startswith("timeoff."):
                rid = data.get("timeoffRequestId")
                if rid is not None:
                    # The v2 payload carries no change date, so walk the changes feed
                    # in ≤6-month windows (forward-cushioned for a frozen run whose
                    # virtual clock can lead wall-clock) and match the requestId. Each
                    # individual call is ≤180 days, within the documented window cap.
                    now = datetime.now(timezone.utc)
                    found = False
                    window_to = now + timedelta(days=200)
                    for _ in range(3):
                        window_since = window_to - timedelta(days=180)
                        st, _, arr = client.timeoff_changes(
                            since=window_since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            to=window_to.strftime("%Y-%m-%dT%H:%M:%SZ"))
                        if st == 200 and isinstance(arr, list) and any(
                                c.get("requestId") == rid for c in arr):
                            found = True
                            break
                        window_to = window_since
                    report.record_protocol("HiBob fetch-on-notify timeoff requestId correlation",
                                           found, "" if found else f"requestId {rid} not found")
                    if found:
                        report.count("correlated:timeoff")
        return Response("", status=200)
