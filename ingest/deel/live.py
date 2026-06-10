"""Deel live ingestion — the ``x-deel-signature`` HMAC-signed webhook.

When a contract/invoice changes, Deel POSTs a nested envelope:

  {"data": {"meta": {"event_type": "invoice.paid", "organization_id": "…"},
            "resource": [ { …the contract/invoice… } ]},
   "timestamp": "2025-02-05T15:39:38.070Z"}

signed (developer.deel.com webhook verification) with a **bare lowercase-hex**
HMAC-SHA256 over the string **``"POST" + rawBody``** (the literal method string
prepended to the raw body; NO ``sha256=`` prefix, NOT base64, NO timestamp in the
signed string). The signature rides ``x-deel-signature`` with companion
``x-deel-hmac-label`` (key id) + ``x-deel-webhook-version`` headers.

This slice verifies the signature, contract-checks the nested envelope, and —
because the resource is an invoice (Deel's "payment" record) — fetch-on-notify
correlates the resource ``id`` against ``GET /invoices?status=all`` (the same id
must appear). Built blind from the official Deel webhook contract.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from flask import Response, request

from ..config import DeelConfig
from ..fidelity import FidelityReport
from .client import DeelClient

ENDPOINT = "/webhooks/deel"
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_KNOWN_EVENTS = {
    "contract.created", "contract.updated", "contract.status.updated",
    "invoice.created", "invoice.paid", "payment.statement.mark-paid",
}


def verify_signature(secret: str, raw: bytes, *, header: str | None) -> bool:
    """Constant-time verify of ``x-deel-signature``.

    Recomputes the bare lowercase-hex ``HMAC-SHA256(secret, "POST" + rawBody)`` and
    compares it against the header value (no ``sha256=`` prefix, no base64)."""
    if not header:
        return False
    expected = hmac.new(secret.encode(), b"POST" + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def _validate_envelope(body: Any, report: FidelityReport, seen_ok: set) -> list:
    """Validate the nested {data:{meta, resource:[…]}, timestamp} envelope; return resources."""
    problems: list[str] = []
    resources: list = []
    if not isinstance(body, dict):
        report.diverge("protocol", ENDPOINT, "event is not a JSON object")
        return resources
    data = body.get("data")
    if not isinstance(data, dict):
        problems.append("`data` must be an object")
    else:
        meta = data.get("meta")
        if not isinstance(meta, dict):
            problems.append("`data.meta` must be an object")
        else:
            if meta.get("event_type") not in _KNOWN_EVENTS:
                problems.append(f"unknown data.meta.event_type {meta.get('event_type')!r}")
            if not isinstance(meta.get("organization_id"), str):
                problems.append("`data.meta.organization_id` must be a string")
        res = data.get("resource")
        if not isinstance(res, list):
            problems.append("`data.resource` must be an ARRAY")
        else:
            resources = res
    ts = body.get("timestamp")
    if not (isinstance(ts, str) and _TS_RE.match(ts)):
        problems.append(f"`timestamp` must be RFC3339 ms+Z: {ts!r}")
    check = "Deel webhook envelope contract (data.meta + resource[] + timestamp)"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")
    return resources


def register(server, cfg: DeelConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    client = DeelClient(cfg, report)
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def deel_webhook():  # noqa: ANN202
        raw = request.get_data()
        valid = verify_signature(secret, raw, header=request.headers.get("x-deel-signature"))
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "x-deel-signature mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        resources = _validate_envelope(body, report, seen_ok)
        meta = (body.get("data") or {}).get("meta") if isinstance(body, dict) else {}
        etype = (meta or {}).get("event_type")
        if etype in _KNOWN_EVENTS:
            report.record_live_event("deel.event", f"{etype} resources={len(resources)}")
            report.count(f"event:{etype}")
            # Fetch-on-notify: correlate an invoice resource id against the invoices
            # list. The notified invoice is the most recent, so narrow the re-fetch to
            # its issued window (issued_from_date from the resource's own issued_at)
            # rather than walking from the oldest page.
            inv_resources = [r for r in resources
                             if isinstance(r, dict) and str(r.get("id", "")).startswith("inv_")]
            inv_ids = [r.get("id") for r in inv_resources]
            if inv_ids:
                since = min((str(r.get("issued_at", ""))[:10] for r in inv_resources
                             if r.get("issued_at")), default=None)
                _, _, page = client.list_invoices(limit=100, status="all",
                                                  issued_from_date=since)
                known = ({i.get("id") for i in page.get("data", [])}
                         if isinstance(page, dict) else set())
                if all(iid in known for iid in inv_ids):
                    report.count("correlated:invoice")
                    report.record_protocol("Deel fetch-on-notify invoice id correlation",
                                           True, "")
                else:
                    report.record_protocol("Deel fetch-on-notify invoice id correlation",
                                           False, f"resource ids {inv_ids} not all in invoices")
        return Response("", status=200)
