"""Discord slice orchestration: redirect transports, run historical/live, emit report."""
from __future__ import annotations

import asyncio

import discord

from ..config import DiscordConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from . import auth, historical, live


def _new_report(cfg: DiscordConfig) -> FidelityReport:
    report = FidelityReport("discord", cfg.api_base)
    report.note("discord.py has no base_url parameter; the REST base "
                "(discord.http.Route.BASE) and gateway base "
                "(DiscordWebSocket.DEFAULT_GATEWAY) are redirected to the target — the "
                "library's auth, pagination and gateway handshake are otherwise unchanged.")
    report.note("Discord's official spec is OpenAPI 3.1 and REST-only; gateway envelopes "
                "aren't modelled, so live MESSAGE_CREATE payloads are validated against the "
                "REST `MessageResponse` schema and other gateway frames are recorded only.")
    return report


def _historical_intents() -> discord.Intents:
    # Exactly the intents we use — guilds (to discover guilds over the gateway) plus the
    # message intents — rather than Intents.default()'s broad mask, which the gateway can
    # reject (close 4013). This is the standard "request only what you need" posture.
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    return intents


class _HistoryClient(auth.TolerantClient):
    """Connects the gateway only to discover the bot's guilds (the canonical bot path),
    then drives the REST historical pull from those IDs and disconnects."""

    def __init__(self, report: FidelityReport, sv: SpecValidator,
                 max_guilds: int | None, **kwargs):
        super().__init__(**kwargs)
        self.set_report(report)
        self._report = report
        self._sv = sv
        self._max_guilds = max_guilds
        self._done = False

    async def on_ready(self):
        if self._done:  # guard against reconnect re-entry
            return
        self._done = True
        try:
            self._report.record_live_event(
                "READY", f"bot={self.user} guilds={len(self.guilds)} "
                         f"session={getattr(self.ws, 'session_id', None)}")
            self._report.auth.update({
                "method": "Bot token (Authorization: Bot …)",
                "bot_user": str(self.user),
                "bot_id": str(self.user.id) if self.user else None,
            })
            guild_ids = [str(g.id) for g in self.guilds]
            await historical.run_historical(self.http, guild_ids, self._report,
                                            self._sv, max_guilds=self._max_guilds)
        finally:
            await self.close()


async def _drive_historical(cfg: DiscordConfig, report: FidelityReport,
                            max_guilds: int | None) -> None:
    sv = SpecValidator("discord")
    auth.redirect_transports(cfg, report)
    client = _HistoryClient(report, sv, max_guilds, intents=_historical_intents())
    try:
        await client.start(cfg.require_bot_token())
    finally:
        if not client.is_closed():
            await client.close()


def run_historical(max_guilds: int | None = None) -> FidelityReport:
    cfg = DiscordConfig.from_env()
    report = _new_report(cfg)
    try:
        asyncio.run(_drive_historical(cfg, report, max_guilds))
    finally:
        auth.restore_transports()
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = DiscordConfig.from_env()
    report = _new_report(cfg)
    sv = SpecValidator("discord")
    auth.redirect_transports(cfg, report)
    print(f"Discord gateway listener connecting to {cfg.gateway_url or 'the default gateway'} ...")
    print("Ctrl-C to stop and write the fidelity report.")
    try:
        asyncio.run(live.run(cfg, sv, report, run_seconds=run_seconds))
    except KeyboardInterrupt:
        pass
    finally:
        auth.restore_transports()
    return report
