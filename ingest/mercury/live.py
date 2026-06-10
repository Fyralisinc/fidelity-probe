"""Mercury live ingestion — the transaction webhook (JSON-merge-patch event).

When a transaction is created/updated, Mercury POSTs a merge-patch event:

  {"id":…, "resourceType":"transaction", "resourceId":…, "operationType":"create",
   "resourceVersion":1, "occurredAt":"…Z", "changedPaths":[…], "mergePatch":{…},
   "previousValues":{…}}

authenticated with header ``Mercury-Signature: t=<unix_seconds>,v1=<hex>`` — a
Stripe-style pair where the hex is HMAC-SHA256 over ``"{t}.{rawBody}"`` (bare
lowercase hex, NO ``sha256=`` prefix, NOT base64). This slice verifies the
signature, contract-checks the event envelope, and — because a ``create`` carries
the full resource (incl. ``accountId``) in the merge patch — re-fetches
``GET /account/{accountId}/transaction/{id}`` to confirm the resource resolves.

Built blind from the official contract (docs.mercury.com/reference/webhooks).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from flask import Response, request

from ..config import MercuryConfig
from ..fidelity import FidelityReport
from .client import MercuryClient

ENDPOINT = "/webhooks/mercury"
_TOP_KEYS = {"id", "resourceType", "resourceId", "operationType", "resourceVersion",
             "occurredAt", "mergePatch"}
_KNOWN_OPS = {"create", "update", "delete"}


def verify_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of ``Mercury-Signature: t=…,v1=…``.

    Recomputes ``hex(HMAC-SHA256(secret, "{t}.{rawBody}"))`` and compares ``v1``.
    """
    if not header:
        return False
    parts: dict[str, str] = {}
    for seg in header.split(","):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip()] = v.strip()
    ts, got = parts.get("t"), parts.get("v1")
    if not ts or not got:
        return False
    signed = ts.encode("ascii") + b"." + raw
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, got)


def _validate_event(body: Any, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not isinstance(body, dict):
        report.diverge("protocol", ENDPOINT, "event is not a JSON object")
        return
    missing = [k for k in _TOP_KEYS if k not in body]
    if missing:
        problems.append(f"event missing top-level keys {sorted(missing)}")
    if body.get("operationType") not in _KNOWN_OPS:
        problems.append(f"unknown operationType {body.get('operationType')!r}")
    if not isinstance(body.get("resourceVersion"), int):
        problems.append("resourceVersion must be an int")
    occurred = body.get("occurredAt")
    # occurredAt is RFC3339 UTC with microsecond precision (a '.' before the Z)
    if not isinstance(occurred, str) or not occurred.endswith("Z") or "." not in occurred:
        problems.append(f"occurredAt must be RFC3339 microsecond-Z: {occurred!r}")
    if not isinstance(body.get("mergePatch"), dict):
        problems.append("mergePatch must be an object (RFC-7396 merge patch)")
    check = "Mercury event envelope contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def register(server, cfg: MercuryConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_secret()
    client = MercuryClient(cfg, report)
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def mercury_webhook():  # noqa: ANN202
        raw = request.get_data()
        sig = request.headers.get("Mercury-Signature")
        valid = verify_signature(secret, raw, sig)
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "Mercury-Signature mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        _validate_event(body, report, seen_ok)
        if isinstance(body, dict) and body.get("resourceType") == "transaction":
            op = body.get("operationType")
            rid = body.get("resourceId")
            report.record_live_event("mercury.transaction", f"{op} transaction id={rid}")
            report.count(f"event:transaction.{op}")
            # Fetch-on-notify: the merge patch carries accountId for a create — re-fetch.
            patch = body.get("mergePatch") or {}
            account_id = patch.get("accountId")
            if account_id and rid:
                status, _, txn = client.get_transaction(account_id, rid)
                if status == 200 and isinstance(txn, dict) and txn.get("id") == rid:
                    report.count("refetched:transaction")
                    report.record_protocol("Mercury fetch-on-notify re-fetch", True, "")
                else:
                    report.record_protocol("Mercury fetch-on-notify re-fetch", False,
                                           f"re-fetch {rid} -> {status}")
        return Response("", status=200)
