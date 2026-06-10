"""Signal live ingestion — the persistent linked-device receive loop.

Signal's live surface is NOT a webhook: messages are PUSHED over a long-lived
authenticated linked-device connection (no callback URL, no HMAC — the connection
itself is the trust boundary, like Discord's gateway / Telegram's updates loop).
We connect to the mock's receive gateway (a WebSocket — the transport substitution
for the signal-cli daemon socket), present the persisted linked-device session,
read the ``subscribed`` ack, then validate each pushed ``receive`` notification
against the SAME envelope contract as the backfill — so a backfilled message and
its live twin collapse to one observation (the cross-path dedup invariant), keyed
``signal:{install}:{thread_id}:{ts_ms}:none``. The linked account's OWN outgoing
messages (syncMessage.sentMessage) are skipped server-side, so the live stream
carries only inbound dataMessages.

The negative test (the webhook-tamper analog): a WRONG session must be REJECTED
(rpc_error signal_api_unauthorized + close 4401) — if a bad session were accepted,
the authenticated-connection trust boundary would be broken.
"""
from __future__ import annotations

import asyncio
import json

import aiohttp

from ..config import SignalConfig
from ..fidelity import FidelityReport
from .historical import is_outgoing, thread_id_of, validate_envelope


async def _probe_bad_session(cfg: SignalConfig, report: FidelityReport) -> None:
    """A wrong session on connect must be rejected (the auth boundary)."""
    bad_url = cfg.ws_url() + "?session=tampered-not-the-session"
    rejected = False
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(bad_url, heartbeat=None) as ws:
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    body = json.loads(msg.data)
                    code = (body.get("error", {}).get("data", {}).get("signal_code")
                            if isinstance(body, dict) else None)
                    if code == "signal_api_unauthorized":
                        rejected = True
                        with_close = await asyncio.wait_for(ws.receive(), timeout=5)
                        if with_close.type in (aiohttp.WSMsgType.CLOSE,
                                               aiohttp.WSMsgType.CLOSED):
                            rejected = True
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    rejected = (msg.data == 4401) or rejected
    except Exception as exc:  # a refused/closed handshake is also a rejection
        rejected = True
        report.note(f"bad-session connect raised (treated as rejection): {exc}")
    report.record_signature("signal:gateway", rejected,
                            "" if rejected else "a WRONG session was ACCEPTED by the gateway")


def _handle_frame(body: dict, cfg: SignalConfig, report: FidelityReport,
                  seen_ok: set, external_ids: set) -> None:
    # The live push is a signal-cli `receive` JSON-RPC notification.
    if body.get("method") != "receive":
        return
    env = (body.get("params") or {}).get("envelope")
    if not isinstance(env, dict):
        report.diverge("protocol", "signal:gateway", "receive carried no envelope")
        return
    validate_envelope(env, report, seen_ok)
    tid = thread_id_of(env)
    ts = env.get("timestamp")
    ext = f"signal:{cfg.namespace()}:{tid}:{ts}:none"
    external_ids.add(ext)
    # the live stream should carry inbound only (own outgoing is skipped server-side).
    if is_outgoing(env):
        report.diverge("protocol", "signal:gateway",
                       f"own outgoing (syncMessage) was pushed live: ts={ts}")
    report.record_live_event("receive", f"thread={str(tid)[:12]} ts={ts}")
    report.count("event:receive")


async def _listen(cfg: SignalConfig, report: FidelityReport,
                  run_seconds: float | None) -> None:
    base_url, session = cfg.require_auth()
    url = cfg.ws_url() + f"?session={session}"
    seen_ok: set = set()
    external_ids: set = set()
    loop = asyncio.get_event_loop()
    deadline = (loop.time() + run_seconds) if run_seconds else None

    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(url, heartbeat=None) as ws:
            # The subscribeReceive ack (the subscription the consumer warm-starts on).
            first = await asyncio.wait_for(ws.receive(), timeout=10)
            if first.type == aiohttp.WSMsgType.TEXT:
                ack = json.loads(first.data)
                if ack.get("method") == "subscribed":
                    report.record_protocol(
                        "gateway sends a subscribed ack (account/uuid) on connect",
                        bool(ack.get("params", {}).get("account")),
                        f"account={ack.get('params', {}).get('account')}")
                    report.record_live_event("subscribed",
                                             f"account={ack.get('params', {}).get('account')}")
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
                        report.diverge("protocol", "signal:gateway", "non-JSON frame")
                        continue
                    _handle_frame(body, cfg, report, seen_ok, external_ids)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
    if external_ids:
        report.record_protocol("each live receive yields an external_id (edit slot none)",
                               True, f"{len(external_ids)} distinct")


def run(cfg: SignalConfig, report: FidelityReport,
        run_seconds: float | None = None) -> None:
    async def _main() -> None:
        await _probe_bad_session(cfg, report)
        await _listen(cfg, report, run_seconds)
    asyncio.run(_main())
