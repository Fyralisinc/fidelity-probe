"""Discord auth + transport redirect.

discord.py has no `base_url` constructor parameter: the REST base
(`https://discord.com/api/v10`) and the gateway host (`wss://gateway.discord.gg/`)
are baked into the library as class attributes. Pointing the official client at a
wire-compatible mock is therefore a real client-side integration problem, solved
here by redirecting both transports at their single source of truth — exactly the
"only the base URL differs" contract this whole client is built around:

  * REST    -> `discord.http.Route.BASE`
  * Gateway -> `discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY`

We do *not* fork or branch behaviour: every request shape, the Bot token auth
(`Authorization: Bot <token>`), pagination, and the gateway IDENTIFY handshake are
the library's own. Only those two base URLs move.

Auth itself is the standard bot login: `HTTPClient.static_login(token)` performs
`GET /users/@me` with the Bot token, which both validates the token and yields the
bot's own user object (recorded + schema-checked).
"""
from __future__ import annotations

import asyncio

import discord
import yarl
from discord import http as discord_http
from discord.gateway import DiscordWebSocket
from discord.http import HTTPClient, Route

from ..config import DiscordConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator

# Remember the originals so the redirect is reversible (selfcheck / repeated runs).
_ORIG_ROUTE_BASE = Route.BASE
_ORIG_GATEWAY = DiscordWebSocket.DEFAULT_GATEWAY
_ORIG_JSON_OR_TEXT = discord_http.json_or_text

# The report the content-type accommodation should flag a deviation onto.
_active_report: FidelityReport | None = None


def _flag_content_type(ctype: str) -> None:
    """Record (once) that responses don't use Discord's documented bare Content-Type."""
    report = _active_report
    if report is None or getattr(report, "_ct_flagged", False):
        return
    report._ct_flagged = True  # type: ignore[attr-defined]
    report.record_protocol(
        "Content-Type (documented `application/json`)", False,
        f"responses use `{ctype}` instead of the bare `application/json` that real "
        f"Discord returns; discord.py's json_or_text does an EXACT match, so the stock "
        f"client returns every body unparsed as text and cannot build its models. "
        f"Accommodated here (charset-tolerant parse) so the audit can continue.")


async def _lenient_json_or_text(response):  # noqa: ANN001
    """charset-tolerant drop-in for discord.http.json_or_text.

    discord.py only parses JSON when Content-Type is *exactly* `application/json`. Most
    HTTP clients match on the media type and ignore parameters like `; charset=utf-8`;
    we do the same so a non-compliant Content-Type doesn't silently turn every response
    into a string — recording the deviation rather than papering over it."""
    text = await response.text(encoding="utf-8")
    ctype = response.headers.get("content-type", "")
    if ctype.split(";")[0].strip().lower() == "application/json":
        if ctype.strip().lower() != "application/json":
            _flag_content_type(ctype)
        return discord.utils._from_json(text)
    return text


def redirect_transports(cfg: DiscordConfig, report: FidelityReport | None = None) -> None:
    """Move discord.py's REST + gateway bases to the configured target, and install a
    charset-tolerant JSON decoder. The library's behaviour is otherwise untouched."""
    global _active_report
    _active_report = report
    Route.BASE = cfg.api_base
    discord_http.json_or_text = _lenient_json_or_text
    if cfg.gateway_url:
        # from_client() rewrites the query (?v=&encoding=&compress=), so a bare ws base
        # is what's wanted here; we strip any query the operator supplied.
        base = yarl.URL(cfg.gateway_url).with_query(None)
        DiscordWebSocket.DEFAULT_GATEWAY = base
    if report is not None:
        report.note(f"redirected discord.py transports: REST -> {Route.BASE}, "
                    f"gateway -> {DiscordWebSocket.DEFAULT_GATEWAY}")


def restore_transports() -> None:
    global _active_report
    Route.BASE = _ORIG_ROUTE_BASE
    DiscordWebSocket.DEFAULT_GATEWAY = _ORIG_GATEWAY
    discord_http.json_or_text = _ORIG_JSON_OR_TEXT
    _active_report = None


class TolerantClient(discord.Client):
    """discord.Client whose login() tolerates a missing GET /oauth2/applications/@me.

    discord.py's `login()` unconditionally fetches the application object before it will
    connect the gateway. Real Discord implements that endpoint; a target that 404s it
    would otherwise make the official client unusable. We replicate `login()` faithfully
    but record the deviation and continue, so the gateway can still be exercised. Attach
    a FidelityReport via `set_report()` to capture the finding.
    """

    def set_report(self, report: FidelityReport) -> None:
        self._fidelity_report = report

    async def login(self, token: str) -> None:  # mirrors discord.Client.login, tolerantly
        from discord.client import _loop as _CLIENT_LOOP
        from discord.user import ClientUser

        if self.loop is _CLIENT_LOOP:
            await self._async_setup_hook()
        token = token.strip()
        data = await self.http.static_login(token)
        self._connection.user = ClientUser(state=self._connection, data=data)
        try:
            self._application = await self.application_info()
            if self._connection.application_id is None:
                self._connection.application_id = self._application.id
            if not self._connection.application_flags:
                self._connection.application_flags = self._application.flags
        except discord.NotFound:
            self._application = None
            report = getattr(self, "_fidelity_report", None)
            if report is not None:
                report.record_protocol(
                    "GET /oauth2/applications/@me", False,
                    "404 Not Found; discord.py's login() requires this endpoint and real "
                    "Discord implements it. Tolerated so the gateway can still connect.")
        await self.setup_hook()


async def login(cfg: DiscordConfig, sv: SpecValidator,
                report: FidelityReport) -> tuple[HTTPClient, dict]:
    """Build the official HTTPClient, perform the Bot-token login, validate the bot user."""
    token = cfg.require_bot_token()
    http = HTTPClient(loop=asyncio.get_event_loop())
    user = await http.static_login(token)
    user_data = dict(user) if not isinstance(user, dict) else user
    sv.validate_response(user_data, "/users/@me", report, label="users.@me")
    report.auth.update({
        "method": "Bot token (Authorization: Bot …)",
        "bot_user": f"{user_data.get('username')}#{user_data.get('discriminator')}",
        "bot_id": user_data.get("id"),
    })
    return http, user_data
