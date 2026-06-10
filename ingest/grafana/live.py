"""Grafana live ingestion — the Alerting webhook (contact point).

When an alert fires/resolves, Grafana POSTs an **Alertmanager-superset alert
group** to the webhook edge:

  {"receiver":…, "status":"firing", "alerts":[{labels, annotations, startsAt,
   endsAt, fingerprint, …}], "groupKey":…, "commonLabels":{…},
   "externalURL":"https://<host>/", "version":"1", "title"/"state"/"message"…}

authenticated with header ``X-Grafana-Alerting-Signature`` = a **bare lowercase
hex** HMAC-SHA256 digest over the raw body alone (NO ``sha256=`` prefix; Grafana
12.0+). This slice verifies the signature, then contract-checks the alert-group
envelope: status, a non-empty ``alerts[]`` each carrying labels/annotations/
timestamps/fingerprint, a still-firing alert's ``endsAt`` zero sentinel, a
parseable ``externalURL`` host (the tenant-resolution key), and ``groupKey``.

Built blind from the official Grafana webhook-notifier contract.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import urlsplit

from flask import Response, request

from ..config import GrafanaConfig
from ..fidelity import FidelityReport

ENDPOINT = "/webhooks/grafana"
_ZERO_TIME = "0001-01-01T00:00:00Z"
_TOP_KEYS = {"receiver", "status", "alerts", "groupLabels", "commonLabels",
             "commonAnnotations", "externalURL", "version", "groupKey"}


def verify_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of X-Grafana-Alerting-Signature = hex(HMAC-SHA256(secret, raw)).

    A bare lowercase hex digest with NO algorithm prefix.
    """
    if not header:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def _validate_group(body: Any, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not isinstance(body, dict):
        report.diverge("protocol", ENDPOINT, "alert group is not a JSON object")
        return

    status = body.get("status")
    if status not in ("firing", "resolved"):
        problems.append(f"top-level status must be firing|resolved, got {status!r}")
    missing_top = [k for k in _TOP_KEYS if k not in body]
    if missing_top:
        problems.append(f"alert group missing top-level keys {sorted(missing_top)}")

    # externalURL host is the tenant-resolution key — must be a parseable host.
    ext = body.get("externalURL")
    if not isinstance(ext, str) or not ext or not urlsplit(ext).netloc:
        problems.append(f"externalURL has no parseable host: {ext!r}")
    if not body.get("groupKey"):
        problems.append("missing groupKey (the dedup grouping key)")

    alerts = body.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        problems.append("alerts[] must be a non-empty array")
        alerts = []
    for al in alerts:
        if not isinstance(al, dict):
            problems.append("alert element is not an object")
            continue
        if al.get("status") not in ("firing", "resolved"):
            problems.append(f"alert.status invalid: {al.get('status')!r}")
        if not isinstance(al.get("labels"), dict) or not al["labels"]:
            problems.append("alert.labels must be a non-empty object")
        if not isinstance(al.get("annotations"), dict):
            problems.append("alert.annotations must be an object")
        if not al.get("startsAt"):
            problems.append("alert missing startsAt (RFC3339)")
        if "endsAt" not in al:
            problems.append("alert missing endsAt")
        elif al.get("status") == "firing" and al.get("endsAt") != _ZERO_TIME:
            problems.append(f"firing alert endsAt should be the zero sentinel "
                            f"{_ZERO_TIME}, got {al.get('endsAt')!r}")
        if not al.get("fingerprint"):
            problems.append("alert missing fingerprint")

    check = "Grafana alert-group envelope contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def register(server, cfg: GrafanaConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def grafana_webhook():  # noqa: ANN202
        raw = request.get_data()
        sig = request.headers.get("X-Grafana-Alerting-Signature")
        valid = verify_signature(secret, raw, sig)
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "X-Grafana-Alerting-Signature mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        _validate_group(body, report, seen_ok)
        if isinstance(body, dict):
            alerts = body.get("alerts") or []
            names = sorted({(a.get("labels") or {}).get("alertname", "?")
                            for a in alerts if isinstance(a, dict)})
            report.record_live_event(
                "grafana.alert",
                f"{body.get('status')} [{len(alerts)}] {','.join(names)}")
            report.count(f"alert:{body.get('status')}")
        return Response("", status=200)
