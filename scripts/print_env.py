#!/usr/bin/env python3
"""Emit `export VAR=…` lines for every provider, read from the studio credentials panel.

Usage:  eval "$(python scripts/print_env.py)"   # load current creds into your shell

Fetches GET http://localhost:7000/api/credentials (override with STUDIO_URL), writes the
GitHub App private key to /tmp/github-app.pem, and prints the env the ingest client reads.
Only the base URLs + credentials differ from production; this is just a convenience so a
human doesn't paste rotating secrets by hand.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

STUDIO = os.environ.get("STUDIO_URL", "http://localhost:7000/api/credentials")
GH_KEY_PATH = "/tmp/github-app.pem"


def main() -> int:
    try:
        with urllib.request.urlopen(STUDIO, timeout=8) as resp:
            d = json.load(resp)["credentials"]
    except Exception as exc:  # noqa: BLE001
        print(f"# failed to read {STUDIO}: {exc}", file=sys.stderr)
        return 1
    b = d.get("base_urls", {})
    out: list[str] = []

    s = d.get("slack", {})
    out += [f'export SLACK_BASE_URL={b.get("slack","")}',
            f'export SLACK_BOT_TOKEN={s.get("bot_token","")}',
            f'export SLACK_SIGNING_SECRET={s.get("signing_secret","")}']

    gh = d.get("github", {})
    if gh.get("private_key"):
        with open(GH_KEY_PATH, "w", encoding="utf-8") as fh:
            fh.write(gh["private_key"])
    out += [f'export GITHUB_BASE_URL={b.get("github","")}',
            f'export GITHUB_APP_ID={gh.get("app_id","")}',
            f'export GITHUB_INSTALLATION_ID={gh.get("installation_id","")}',
            f'export GITHUB_PRIVATE_KEY_PATH={GH_KEY_PATH}']

    dc = d.get("discord", {})
    out += [f'export DISCORD_API_BASE_URL={b.get("discord_rest","")}',
            f'export DISCORD_GATEWAY_URL={b.get("discord_gateway","")}',
            f'export DISCORD_BOT_TOKEN={dc.get("bot_token","")}']

    g = d.get("gmail", {})
    out += [f'export GOOGLE_SERVICE_ACCOUNT_EMAIL={g.get("service_account_email","")}',
            f'export GOOGLE_DOMAIN={g.get("domain","")}',
            f'export GOOGLE_CUSTOMER_ID={g.get("customer_id","")}',
            f'export GMAIL_API_BASE_URL={b.get("gmail","")}',
            f'export GMAIL_TOKEN_URL={b.get("gmail_token","")}',
            f'export GMAIL_DIRECTORY_BASE_URL={b.get("gmail_directory","")}',
            f'export GMAIL_JWKS_URL={b.get("gmail_jwks","")}',
            f'export CALENDAR_API_BASE_URL={b.get("calendar","")}',
            f'export CALENDAR_TOKEN_URL={b.get("calendar_token","")}']

    n = d.get("notion", {})
    out += [f'export NOTION_API_BASE_URL={b.get("notion","")}',
            f'export NOTION_TOKEN={n.get("bot_token","")}']

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
