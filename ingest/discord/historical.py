"""Discord historical ingestion — guilds → channels → messages.

A Discord bot discovers its guilds over the gateway (the READY + GUILD_CREATE events),
not via REST — so guild IDs are handed in here by the run orchestrator after the
gateway is ready. From each guild ID the production read path is pure REST:
  GET /guilds/{id}                 (guild detail)
  GET /guilds/{id}/channels        (all channels)
  GET /channels/{id}/messages      (newest-first, paginated by snowflake `before`)

We drive the official `HTTPClient`'s own REST methods (the same ones the high-level
client calls), so the wire shape — Bot auth, rate-limit handling, request format —
is the library's. Each returned array is validated against Discord's official
OpenAPI 3.1 response schema, and the snowflake cursor chain is recorded for the
fidelity report. Discord's rate limiter (429 + Retry-After / X-RateLimit-*) is
handled inside HTTPClient; ingestion continues past per-channel permission errors.
"""
from __future__ import annotations

from discord.errors import HTTPException, NotFound

from ..fidelity import FidelityReport
from ..schemas import SpecValidator

_MSG_PAGE = 100
# Channel types that hold a message timeline: text (0), announcement (5), and threads.
_TEXT_CHANNEL_TYPES = {0, 5, 10, 11, 12}


async def probe_rest_guild_list(http, report: FidelityReport) -> None:
    """Document whether the REST bot-guild-list endpoint behaves as Discord's does.

    Real Discord lists a Bot token's guilds at GET /users/@me/guilds; bots normally
    enumerate guilds via the gateway, so this is recorded as a divergence (not fatal):
    discovery falls back to the gateway READY/GUILD_CREATE events."""
    try:
        await http.get_guilds(200)
    except NotFound:
        report.record_protocol(
            "GET /users/@me/guilds", False,
            "404 Not Found; real Discord lists a Bot token's guilds here. Guild discovery "
            "fell back to the gateway READY/GUILD_CREATE events.")
    except HTTPException as e:
        report.note(f"get_guilds probe: {e.status} {e.text}")


async def fetch_guild(http, guild_id: str, sv: SpecValidator,
                      report: FidelityReport) -> dict | None:
    try:
        guild = await http.get_guild(guild_id)
    except HTTPException as e:
        report.note(f"get_guild({guild_id}): {e.status} {e.text}")
        return None
    sv.validate_response(guild, "/guilds/{guild_id}", report, label="guilds.get")
    return guild


async def list_channels(http, guild_id: str, sv: SpecValidator,
                        report: FidelityReport) -> list[dict]:
    try:
        channels = await http.get_all_guild_channels(guild_id)
    except HTTPException as e:
        report.note(f"get_all_guild_channels({guild_id}): {e.status} {e.text}")
        return []
    report.record_page("guilds.channels", None)
    sv.validate_response(channels, "/guilds/{guild_id}/channels", report,
                         label="guilds.channels")
    report.count("channel", len(channels))
    return channels


async def fetch_messages(http, channel_id: str, sv: SpecValidator,
                         report: FidelityReport) -> int:
    """Paginate a channel's full history backwards via the `before` snowflake cursor."""
    total = 0
    before: str | None = None
    try:
        while True:
            batch = await http.logs_from(channel_id, _MSG_PAGE, before=before)
            report.record_page("channels.messages", before)
            sv.validate_response(batch, "/channels/{channel_id}/messages", report,
                                 label="channels.messages")
            total += len(batch)
            if len(batch) < _MSG_PAGE:
                break
            before = batch[-1]["id"]  # logs_from returns newest-first; walk older
    except HTTPException as e:
        # 403 (no access) / 50001 etc. — record and continue, as a real client would.
        report.note(f"logs_from({channel_id}): {e.status} {e.text}")
    report.count("message", total)
    return total


async def run_historical(http, guild_ids: list[str], report: FidelityReport,
                         sv: SpecValidator, max_guilds: int | None = None) -> None:
    await probe_rest_guild_list(http, report)
    report.count("guild", len(guild_ids))
    if max_guilds is not None:
        guild_ids = guild_ids[:max_guilds]
    for gid in guild_ids:
        await fetch_guild(http, gid, sv, report)
        for ch in await list_channels(http, gid, sv, report):
            if ch.get("type") in _TEXT_CHANNEL_TYPES:
                await fetch_messages(http, ch["id"], sv, report)
