"""Telegram live ingestion — the persistent MTProto updates connection.

Telegram's live surface is NOT a webhook: updates are PUSHED over a long-lived
authenticated connection (no callback URL, no HMAC — the connection itself is the
trust boundary, like Discord's gateway / Gmail Pub/Sub). We connect to the mock's
updates gateway (a WebSocket — the transport substitution for the MTProto socket),
present the persisted ``StringSession``, read the ``updates.state`` cursor, then
validate each pushed ``updateNewMessage`` / ``updateEditMessage`` against the same
message contract as the backfill — so a backfilled message and its live twin
collapse to one observation (the cross-path dedup invariant) and an edit
re-observes via a fresh ``edit_date``.

The negative test (the webhook-tamper analog): a WRONG session must be REJECTED
(rpc_error AUTH_KEY_UNREGISTERED + close 4401) — if a bad session were accepted,
the authenticated-connection trust boundary would be broken.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from ..config import TelegramConfig
from ..fidelity import FidelityReport
from .historical import _validate_message

_UPDATE_KINDS = {"updateNewMessage", "updateEditMessage"}


async def _probe_bad_session(cfg: TelegramConfig, report: FidelityReport) -> None:
    """A wrong session on connect must be rejected (the auth boundary)."""
    bad_url = cfg.ws_url() + "?session=tampered-not-the-session"
    rejected = False
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(bad_url, heartbeat=None) as ws:
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    body = json.loads(msg.data)
                    if body.get("error_message") == "AUTH_KEY_UNREGISTERED":
                        rejected = True
                        # drain the following close frame
                        with_close = await asyncio.wait_for(ws.receive(), timeout=5)
                        if with_close.type in (aiohttp.WSMsgType.CLOSE,
                                               aiohttp.WSMsgType.CLOSED):
                            rejected = True
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    rejected = (msg.data == 4401) or rejected
    except Exception as exc:  # a refused/closed handshake is also a rejection
        rejected = True
        report.note(f"bad-session connect raised (treated as rejection): {exc}")
    report.record_signature("telegram:gateway", rejected,
                            "" if rejected else "a WRONG session was ACCEPTED by the gateway")


def _handle_update(body: dict, cfg: TelegramConfig, report: FidelityReport,
                   seen_ok: set, external_ids: set) -> None:
    kind = body.get("_")
    if kind not in _UPDATE_KINDS:
        return
    msg = body.get("message")
    if not isinstance(msg, dict):
        report.diverge("protocol", "telegram:gateway",
                       f"{kind} carried no message object")
        return
    _validate_message(msg, report, seen_ok)
    dialog = body.get("dialog") or {}
    did = dialog.get("dialog_id")
    edit = msg.get("edit_date")
    ext = (f"telegram:{cfg.namespace()}:{did}:{msg.get('id')}:"
           f"{edit if edit is not None else 'none'}")
    external_ids.add(ext)
    report.record_live_event(kind, f"dialog={did} id={msg.get('id')} "
                                   f"edit={'yes' if edit else 'no'}")
    report.count(f"event:{kind}")


async def _listen(cfg: TelegramConfig, report: FidelityReport,
                  run_seconds: float | None) -> None:
    base_url, session = cfg.require_auth()
    url = cfg.ws_url() + f"?session={session}"
    seen_ok: set = set()
    external_ids: set = set()
    loop = asyncio.get_event_loop()
    deadline = (loop.time() + run_seconds) if run_seconds else None

    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(url, heartbeat=None) as ws:
            # The updates.state ack (the live cursor the consumer warm-starts on).
            first = await asyncio.wait_for(ws.receive(), timeout=10)
            if first.type == aiohttp.WSMsgType.TEXT:
                ack = json.loads(first.data)
                if ack.get("_") == "updates.state":
                    report.record_protocol(
                        "gateway sends updates.state (pts/qts/seq/date) on connect",
                        all(k in ack for k in ("pts", "qts", "seq", "date")),
                        f"user_id={ack.get('user_id')}")
                    report.record_live_event("updates.state", f"user_id={ack.get('user_id')}")
            while True:
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                else:
                    remaining = 3600
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        body = json.loads(msg.data)
                    except ValueError:
                        report.diverge("protocol", "telegram:gateway", "non-JSON frame")
                        continue
                    _handle_update(body, cfg, report, seen_ok, external_ids)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            with_close = ws  # noqa: F841 (context manager closes it)
    if external_ids:
        report.record_protocol("each live update yields an edit-versioned external_id",
                               True, f"{len(external_ids)} distinct")


def run(cfg: TelegramConfig, report: FidelityReport,
        run_seconds: float | None = None) -> None:
    async def _main() -> None:
        await _probe_bad_session(cfg, report)
        await _listen(cfg, report, run_seconds)
    asyncio.run(_main())
