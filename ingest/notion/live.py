"""Notion live ingestion — thin webhook + X-Notion-Signature verification + fetch-back.

Production-faithful to Notion's webhook model:

  * The FIRST delivery a subscription receives is the one-time **verification
    handshake** — an UNSIGNED POST whose body is ``{"verification_token":"secret_…"}``.
    There's nothing to verify against yet (the token *is* the future signing secret),
    so we capture it and ack 200.
  * Every steady-state delivery is signed ``X-Notion-Signature: sha256=<hex>`` =
    HMAC-SHA256 over the *raw* body keyed by the verification token. We recompute
    over the raw bytes and constant-time compare *before* trusting the payload (a
    bare-hex header is also accepted, mirroring header-format drift tolerance).
  * The event itself is **thin** — ``{id, timestamp, workspace_id, type, entity:{id,type}}``
    with no object body. A real consumer **fetches the page back** via
    ``GET /v1/pages/{id}`` and validates *that*. We do the same: assert the thin
    envelope's consumer-critical fields, then fetch + schema-validate the page.

Notion publishes no OpenAPI for webhook events, so — like the Slack/GitHub live
slices — the envelope is verified and structurally contract-checked, while the
fetched-back page object is validated against the hand-authored Notion spec.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from flask import Response, request

from ..config import NotionConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from ..webhook_server import WebhookServer
from .client import NotionClient

ENDPOINT = "/notion/events"

_PAGE_EVENTS = {
    "page.created", "page.content_updated", "page.properties_updated",
    "page.moved", "page.deleted", "page.undeleted", "page.locked", "page.unlocked",
}


def verify_signature(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time check of X-Notion-Signature; accepts ``sha256=<hex>`` or bare hex."""
    if not header:
        return False
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    for candidate in (f"sha256={digest}", digest):
        if hmac.compare_digest(candidate, header):
            return True
    return False


def _missing(body: dict, *paths: str) -> list[str]:
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


def _is_handshake(body: dict) -> bool:
    return isinstance(body, dict) and "verification_token" in body and "entity" not in body


def _validate_envelope(body: dict, report: FidelityReport, seen_ok: set) -> None:
    problems = _missing(body, "id", "timestamp", "workspace_id", "type",
                        "entity.id", "entity.type")
    etype = body.get("type")
    if etype not in _PAGE_EVENTS and isinstance(etype, str) and etype.startswith("page."):
        report.note(f"live notion: unrecognized page event type {etype!r}")
    check = "live notion thin-envelope contract"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def register(server: WebhookServer, cfg: NotionConfig, report: FidelityReport) -> None:
    secret = cfg.require_webhook_verification_token()
    sv = SpecValidator("notion")
    client = NotionClient(cfg, report)
    seen_ok: set = set()

    @server.app.post(ENDPOINT)
    def notion_events():  # noqa: ANN202
        raw = request.get_data()  # raw bytes — required for a correct HMAC
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            report.record_protocol("live notion JSON body", False, "body is not valid JSON")
            return Response("", status=200)

        # One-time verification handshake: unsigned, just the token.
        if _is_handshake(body):
            tok = body.get("verification_token")
            ok = isinstance(tok, str) and tok.startswith("secret_")
            report.record_protocol("live notion verification handshake", ok,
                                   "" if ok else "verification_token missing/!secret_ prefix")
            report.record_live_event("verification", f"token={str(tok)[:12]}…")
            return Response(json.dumps({"handled": "verification"}), mimetype="application/json")

        # Steady-state: verify signature BEFORE trusting the payload.
        sig = request.headers.get("X-Notion-Signature")
        valid = verify_signature(secret, raw, sig)
        report.record_signature(ENDPOINT, valid,
                                "" if valid else "X-Notion-Signature mismatch / missing")
        if not valid:
            return Response("invalid signature", status=401)

        _validate_envelope(body, report, seen_ok)
        etype = body.get("type", "unknown")
        entity = body.get("entity") or {}
        report.record_live_event(etype, f"entity={entity.get('type')}:{str(entity.get('id'))[:8]} "
                                        f"ws={str(body.get('workspace_id'))[:8]}")
        report.count(f"event:{etype}", 1)

        # Pages only (v1): fetch the full page back and validate it — exactly what a
        # production consumer does to hydrate a thin event.
        if entity.get("type") == "page" and entity.get("id"):
            status, _, page = client.get(f"/v1/pages/{entity['id']}", "live.pages.get")
            if status == 200 and isinstance(page, dict):
                sv.validate_against_component(page, "Page", report)
                report.count("live:page_fetched", 1)
            elif status in (404, 401):
                # A deleted/inaccessible page fetch-miss is acked, not retried.
                report.note(f"live notion fetch-back {etype}: GET /v1/pages/{entity['id']} -> {status}")
            else:
                report.diverge("protocol", "live.pages.get",
                               f"GET /v1/pages/{entity['id']} -> {status}")
        return Response("", status=200)
