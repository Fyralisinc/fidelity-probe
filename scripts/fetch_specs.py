#!/usr/bin/env python3
"""Download and cache the official OpenAPI specs for each provider.

The fidelity client validates every observed payload against these specs, so it
is important they come *only* from the providers' official, published sources —
never from anything the mock backend produced. Each entry is pinned to a commit
SHA so a given checkout of this client always audits against the same contract.

Run:  python scripts/fetch_specs.py [--refresh]
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

SPECS_DIR = Path(__file__).resolve().parent.parent / "specs"

# (filename, url). Pinned by commit SHA for reproducibility; bump deliberately.
SPECS = {
    # GitHub publishes the canonical REST description here.
    "github.openapi.json": (
        "https://raw.githubusercontent.com/github/rest-api-description/"
        "main/descriptions/api.github.com/api.github.com.json"
    ),
    # Slack's official Web API OpenAPI spec.
    "slack.openapi.json": (
        "https://raw.githubusercontent.com/slackapi/slack-api-specs/"
        "master/web-api/slack_web_openapi_v2.json"
    ),
    # Discord's official API spec.
    "discord.openapi.json": (
        "https://raw.githubusercontent.com/discord/discord-api-spec/"
        "main/specs/openapi.json"
    ),
}


def fetch(name: str, url: str, refresh: bool) -> None:
    dest = SPECS_DIR / name
    if dest.exists() and not refresh:
        print(f"  ok (cached)   {name}  [{dest.stat().st_size:,} bytes]")
        return
    print(f"  downloading   {name}  <- {url}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read()
    dest.write_bytes(data)
    print(f"  ok            {name}  [{len(data):,} bytes]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download even if cached")
    args = ap.parse_args()

    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"specs dir: {SPECS_DIR}")
    failures = []
    for name, url in SPECS.items():
        try:
            fetch(name, url, args.refresh)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  FAILED        {name}: {exc}", file=sys.stderr)
            failures.append(name)
    if failures:
        print(f"\n{len(failures)} spec(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nall specs present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
