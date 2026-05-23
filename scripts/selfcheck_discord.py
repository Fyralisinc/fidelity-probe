#!/usr/bin/env python3
"""Offline self-check for the Discord slice.

A DEV HARNESS, not part of the shipping client. discord.py is the hardest slice to
point at a mock because it has no base_url knob and it speaks a stateful WebSocket
gateway, so this harness stands up BOTH transports on one aiohttp server:

  * a REST API (GET /users/@me, /users/@me/guilds, /guilds/{id}, /guilds/{id}/channels,
    /channels/{id}/messages with real snowflake `before` pagination), and
  * a gateway WebSocket that performs the real handshake: HELLO (op 10) → accept the
    client's IDENTIFY (op 2) → READY (op 0/READY) → a dispatched MESSAGE_CREATE, plus
    heartbeat ACKs.

It then points the client at both via DISCORD_API_BASE_URL + DISCORD_GATEWAY_URL and
runs the historical and live pipelines. This proves the transport redirection,
Bot-token login, snowflake pagination, gateway IDENTIFY/READY handshake, live event
capture, schema-validation execution and reporting all work end to end — with no
mock-specific code in the client.

Because Discord's official spec ships no response examples to borrow (unlike Slack /
GitHub), the fixtures here are hand-rolled and not guaranteed spec-perfect, so this
harness asserts the *pipeline mechanics*, not zero divergences.

Run:  python scripts/selfcheck_discord.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "selfcheck.bot.token"
MSG_PAGE = 100  # must match historical._MSG_PAGE to force a 2-page chain


# ---- fixtures (plausible, parseable by discord.py; not guaranteed spec-perfect) ----

def make_user(uid: str = "111111111111111111", name: str = "fidelity-bot") -> dict:
    return {
        "id": uid, "username": name, "discriminator": "0001", "avatar": None,
        "global_name": name, "bot": True, "public_flags": 0, "flags": 0,
        "mfa_enabled": True, "locale": "en-US", "verified": True, "email": None,
    }


def make_guild_partial(gid: str, name: str) -> dict:
    return {"id": gid, "name": name, "icon": None, "banner": None, "owner": True,
            "permissions": "0", "features": []}


def make_channel(cid: str, name: str) -> dict:
    return {"id": cid, "type": 0, "guild_id": "222222222222222222", "name": name,
            "position": 0, "nsfw": False, "parent_id": None, "topic": None,
            "last_message_id": None, "rate_limit_per_user": 0, "permission_overwrites": []}


def make_message(mid: int, channel_id: str = "333333333333333333") -> dict:
    return {
        "id": str(mid), "channel_id": channel_id, "author": make_user(),
        "content": f"message {mid}", "timestamp": "2024-01-01T00:00:00+00:00",
        "edited_timestamp": None, "tts": False, "mention_everyone": False,
        "mentions": [], "mention_roles": [], "attachments": [], "embeds": [],
        "reactions": [], "pinned": False, "type": 0, "flags": 0, "components": [],
    }


GUILD_ID = "222222222222222222"
CHANNEL_ID = "333333333333333333"

READY_D = {
    "v": 10, "user": make_user(),
    "guilds": [{"id": GUILD_ID, "unavailable": True}],  # populated by GUILD_CREATE below
    "session_id": "selfcheck-session",
    "resume_gateway_url": "ws://127.0.0.1/gateway", "application": {"id": "999", "flags": 0},
    "relationships": [], "private_channels": [], "presences": [], "geo_ordered_rtc_regions": [],
}

GUILD_CREATE_D = {
    "id": GUILD_ID, "name": "Fidelity Guild", "owner_id": make_user()["id"],
    "channels": [], "roles": [], "emojis": [], "members": [], "member_count": 1,
    "large": False, "unavailable": False, "joined_at": "2024-01-01T00:00:00+00:00",
}


# ---- fake server (REST + gateway) -------------------------------------------------

def _json(data) -> web.Response:
    # Real Discord returns exactly `application/json`; discord.py's json_or_text does an
    # *exact* content-type match, so we must not let aiohttp append `; charset=utf-8`.
    return web.Response(body=json.dumps(data).encode("utf-8"),
                        headers={"Content-Type": "application/json"})


def build_app() -> web.Application:
    app = web.Application()

    async def me(_):
        u = make_user()
        u.update({"premium_type": 0, "accent_color": None, "banner": None,
                  "avatar_decoration_data": None})
        return _json(u)

    async def my_guilds(request):
        after = request.query.get("after")
        # single page of guilds (guild pagination not the focus; message pagination is)
        return _json([] if after else [make_guild_partial("222222222222222222", "Fidelity Guild")])

    async def guild(request):
        return _json({"id": request.match_info["gid"], "name": "Fidelity Guild",
                      "icon": None, "owner_id": make_user()["id"], "roles": [],
                      "emojis": [], "features": [], "approximate_member_count": 3})

    async def guild_channels(request):
        return _json([make_channel("333333333333333333", "general")])

    async def messages(request):
        before = request.query.get("before")
        limit = int(request.query.get("limit", MSG_PAGE))
        if before is None:
            ids = list(range(1000, 1000 - limit, -1))  # a full page, newest-first
        elif before == "901":  # second page: a short page ends the chain
            ids = list(range(900, 870, -1))
        else:
            ids = []
        return _json([make_message(i) for i in ids])

    async def gateway_bot(_):
        return _json({"url": "ws://127.0.0.1/gateway", "shards": 1,
                      "session_start_limit": {"total": 1000, "remaining": 999,
                                              "reset_after": 0, "max_concurrency": 1}})

    async def app_info(_):
        # discord.py's Client.login() fetches GET /oauth2/applications/@me before connecting.
        return _json({
            "id": "999", "name": "Fidelity App", "icon": None, "description": "",
            "bot_public": True, "bot_require_code_grant": False, "owner": make_user(),
            "summary": "", "verify_key": "0" * 64, "flags": 0, "team": None,
        })

    async def gateway_ws(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 45000}})  # HELLO
        seq = 0
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            op = data.get("op")
            if op == 2:  # IDENTIFY -> READY, GUILD_CREATE (guild discovery), then a message
                seq += 1
                await ws.send_json({"op": 0, "s": seq, "t": "READY", "d": READY_D})
                seq += 1
                await ws.send_json({"op": 0, "s": seq, "t": "GUILD_CREATE", "d": GUILD_CREATE_D})
                seq += 1
                await ws.send_json({"op": 0, "s": seq, "t": "MESSAGE_CREATE",
                                    "d": make_message(2000)})
            elif op == 1:  # HEARTBEAT -> ACK
                await ws.send_json({"op": 11})
        return ws

    app.router.add_get("/api/v10/users/@me", me)
    app.router.add_get("/api/v10/users/@me/guilds", my_guilds)
    app.router.add_get("/api/v10/guilds/{gid}", guild)
    app.router.add_get("/api/v10/guilds/{gid}/channels", guild_channels)
    app.router.add_get("/api/v10/channels/{cid}/messages", messages)
    app.router.add_get("/api/v10/gateway/bot", gateway_bot)
    app.router.add_get("/api/v10/oauth2/applications/@me", app_info)
    app.router.add_get("/gateway", gateway_ws)
    return app


def start_server() -> int:
    """Run the aiohttp app in a background thread; return the bound port."""
    port_holder: dict = {}
    ready = threading.Event()

    def _serve():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(build_app())
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        port_holder["port"] = site._server.sockets[0].getsockname()[1]
        ready.set()
        loop.run_forever()

    threading.Thread(target=_serve, daemon=True).start()
    ready.wait(timeout=10)
    return port_holder["port"]


def main() -> int:
    import os
    port = start_server()
    os.environ["DISCORD_API_BASE_URL"] = f"http://127.0.0.1:{port}/api/v10"
    os.environ["DISCORD_GATEWAY_URL"] = f"ws://127.0.0.1:{port}/gateway"
    os.environ["DISCORD_BOT_TOKEN"] = TOKEN

    from ingest.discord import run as dc_run

    failures: list[str] = []

    print("== historical ==")
    report = dc_run.run_historical()
    oc = report.object_counts
    print(f"  auth={report.auth.get('method')} bot={report.auth.get('bot_user')}")
    print(f"  counts={dict(oc)}")
    print(f"  pages={report.pages}")
    print(f"  schema_checks={ {k:(v.passed,v.failed) for k,v in report.schema_checks.items()} }")
    print(f"  divergences={len(report.divergences)} (informational for Discord selfcheck)")

    for obj in ("guild", "channel", "message"):
        if oc.get(obj, 0) <= 0:
            failures.append(f"no {obj} ingested")
    if report.pages.get("channels.messages", 0) < 2:
        failures.append(f"message pagination not exercised (pages={report.pages.get('channels.messages')})")
    if "READY" not in [e["kind"] for e in report.live_events]:
        failures.append("gateway READY not observed during historical guild discovery")
    if "guilds.get" not in report.schema_checks:
        failures.append("guild schema validation did not run")
    if "channels.messages" not in report.schema_checks:
        failures.append("message schema validation did not run")

    print("== live (gateway handshake) ==")
    live_report = dc_run.run_live(run_seconds=3.0)
    kinds = [e["kind"] for e in live_report.live_events]
    print(f"  live_events={kinds}")
    print(f"  schema_checks={ {k:(v.passed,v.failed) for k,v in live_report.schema_checks.items()} }")
    if "HELLO" not in kinds:
        failures.append("gateway HELLO not observed")
    if "READY" not in kinds:
        failures.append("gateway READY not observed (IDENTIFY handshake failed)")
    if "MESSAGE_CREATE" not in kinds:
        failures.append("no MESSAGE_CREATE event captured")
    if "MessageResponse" not in live_report.schema_checks:
        failures.append("live message payload was not schema-validated")

    print()
    if failures:
        print("SELF-CHECK FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SELF-CHECK PASSED ✅  (REST+gateway redirect, Bot login, snowflake pagination, "
          "IDENTIFY/READY handshake, live MESSAGE_CREATE capture + schema validation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
