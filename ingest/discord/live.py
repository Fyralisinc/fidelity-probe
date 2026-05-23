"""Discord live ingestion — the real gateway: IDENTIFY → READY → dispatched events.

This is the official `discord.Client` connecting over the WebSocket gateway with the
library's own IDENTIFY/HELLO/heartbeat handshake; only the gateway base URL has been
redirected (see auth.redirect_transports). We enable raw socket events so we can both
record the protocol milestones (HELLO, READY, RESUMED) and *schema-validate* the
payloads of dispatched `MESSAGE_CREATE` events against Discord's official
`MessageResponse` — the same contract the REST message list is held to.

The client runs until stopped (Ctrl-C) or until an optional time budget elapses, then
the gateway is closed cleanly and the fidelity report is returned.
"""
from __future__ import annotations

import asyncio

import discord

from ..config import DiscordConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from .auth import TolerantClient

# Discord gateway opcodes (subset we narrate).
_OP_DISPATCH = 0
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11


class _GatewayListener(TolerantClient):
    def __init__(self, report: FidelityReport, sv: SpecValidator, **kwargs):
        super().__init__(**kwargs)
        self.set_report(report)
        self._report = report
        self._sv = sv
        self._hello_seen = False

    async def on_socket_raw_receive(self, msg):  # noqa: ANN001
        # Raw gateway frames (debug events enabled). We only need the JSON text frames.
        if not isinstance(msg, str):
            return
        try:
            import json
            payload = json.loads(msg)
        except (ValueError, TypeError):
            return
        op = payload.get("op")
        if op == _OP_HELLO and not self._hello_seen:
            self._hello_seen = True
            interval = (payload.get("d") or {}).get("heartbeat_interval")
            self._report.record_live_event("HELLO", f"heartbeat_interval={interval}ms")
        elif op == _OP_DISPATCH and payload.get("t") == "MESSAGE_CREATE":
            data = payload.get("d") or {}
            # Validate the live message payload against the official message schema.
            self._sv.validate_against_component(data, "MessageResponse", self._report)

    async def on_ready(self):
        ws = self.ws
        self._report.record_live_event(
            "READY", f"bot={self.user} guilds={len(self.guilds)} "
                     f"session={getattr(ws, 'session_id', None)}")
        self._report.auth.setdefault("gateway_session", getattr(ws, "session_id", None))

    async def on_resumed(self):
        self._report.record_live_event("RESUMED", "session resumed")

    async def on_message(self, message: discord.Message):
        self._report.record_live_event(
            "MESSAGE_CREATE",
            f"guild={message.guild.id if message.guild else None} "
            f"channel={message.channel.id} author={message.author} "
            f"content_len={len(message.content)}")
        self._report.count("event:MESSAGE_CREATE", 1)


def _build_intents() -> discord.Intents:
    # Request only what we consume (guilds + message events), not Intents.default()'s
    # broad mask which the gateway may reject (close 4013).
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True  # privileged; required to read message bodies
    return intents


async def run(cfg: DiscordConfig, sv: SpecValidator, report: FidelityReport,
              run_seconds: float | None = None) -> None:
    """Connect to the gateway and listen. Closes after run_seconds (if set) or on cancel."""
    token = cfg.require_bot_token()
    client = _GatewayListener(report, sv, intents=_build_intents(),
                              enable_debug_events=True)

    async def _stopper():
        await asyncio.sleep(run_seconds)
        report.note(f"live: time budget {run_seconds}s elapsed; closing gateway")
        await client.close()

    tasks = [asyncio.create_task(client.start(token))]
    if run_seconds is not None:
        tasks.append(asyncio.create_task(_stopper()))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        if not client.is_closed():
            await client.close()
