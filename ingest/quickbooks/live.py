"""QuickBooks Online live ingestion — Intuit `eventNotifications` webhook.

QBO posts a THIN change notification (no entity body):

  {"eventNotifications":[{"realmId":"...","dataChangeEvent":{"entities":[
     {"name":"Bill","id":"...","operation":"Create","lastUpdated":"...-0700"}]}}]}

authenticated with header `intuit-signature` = **base64**(HMAC-SHA256(rawBody,
verifierToken)) — base64, NOT hex. Because the notification carries no body, the
consumer must RE-QUERY the named entity to fetch it. This slice verifies the
signature, contract-checks the thin envelope, then re-queries
`SELECT * FROM {name} WHERE Id = '{id}'` and validates the fetched object.

Built blind from the official contract (developer.intuit.com/.../webhooks).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from flask import Response, request

from ..config import QuickBooksConfig
from ..fidelity import FidelityReport
from .client import QuickBooksClient

ENDPOINT = "/webhooks/quickbooks"
_THIN_KEYS = {"name", "id", "operation", "lastUpdated"}
_KNOWN_OPS = {"Create", "Update", "Delete", "Merge", "Void"}


def verify_signature(verifier: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of `intuit-signature` = base64(HMAC-SHA256(raw, verifier))."""
    if not header:
        return False
    expected = base64.b64encode(
        hmac.new(verifier.encode(), raw, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, header)


def _validate_envelope(body: Any, report: FidelityReport, seen_ok: set) -> list[dict]:
    """Contract-check the eventNotifications envelope; return the flat entity list."""
    problems: list[str] = []
    entities: list[dict] = []
    notifs = body.get("eventNotifications") if isinstance(body, dict) else None
    if not isinstance(notifs, list) or not notifs:
        problems.append("missing/empty eventNotifications[]")
    else:
        for n in notifs:
            if not n.get("realmId"):
                problems.append("eventNotification missing realmId")
            dce = n.get("dataChangeEvent") or {}
            ents = dce.get("entities")
            if not isinstance(ents, list):
                problems.append("dataChangeEvent.entities is not an array")
                continue
            for e in ents:
                entities.append(e)
                if set(e) - _THIN_KEYS:
                    problems.append(f"entity carries non-thin keys {sorted(set(e)-_THIN_KEYS)} "
                                    "(QBO notifications are thin: name/id/operation/lastUpdated only)")
                for k in ("name", "id", "operation", "lastUpdated"):
                    if not e.get(k):
                        problems.append(f"entity missing {k}")
                if e.get("operation") not in _KNOWN_OPS:
                    problems.append(f"unknown operation {e.get('operation')!r}")
    check = "QBO eventNotifications envelope contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")
    return entities


def register(server, cfg: QuickBooksConfig, report: FidelityReport) -> None:
    verifier = cfg.require_webhook_verifier()
    client = QuickBooksClient(cfg, report)
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def quickbooks_webhook():  # noqa: ANN202
        raw = request.get_data()
        sig = request.headers.get("intuit-signature")
        valid = verify_signature(verifier, raw, sig)
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "intuit-signature mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.diverge("protocol", ENDPOINT, "webhook body is not JSON")
            return Response("bad body", status=400)

        entities = _validate_envelope(body, report, seen_ok)
        for e in entities:
            name, eid = e.get("name"), e.get("id")
            report.record_live_event("qbo.change",
                                     f"{e.get('operation')} {name} id={eid}")
            report.count(f"notify:{name}")
            # Fetch-on-notify: the notification is thin → re-query the entity.
            status, _, qbody = client.query(
                f"SELECT * FROM {name} WHERE Id = '{eid}'", f"refetch.{name}")
            items = (qbody.get("QueryResponse", {}).get(name)
                     if isinstance(qbody, dict) else None) or []
            if status == 200 and items and items[0].get("Id") == eid:
                report.count(f"refetched:{name}")
                report.record_protocol(f"QBO fetch-on-notify re-query ({name})", True, "")
            else:
                report.record_protocol(f"QBO fetch-on-notify re-query ({name})", False,
                                       f"re-query Id={eid} -> {status}, {len(items)} item(s)")
        return Response("", status=200)
