"""CLI: `python -m ingest <provider> <mode>`

Examples:
  python -m ingest slack historical      # full paginated pull + schema audit
  python -m ingest slack live            # run the Events API webhook listener

Exit code is non-zero if any divergence from the official spec/protocol was observed.
"""
from __future__ import annotations

import argparse
import sys

from .fidelity import FidelityReport


def _finish(report: FidelityReport) -> int:
    json_path, md_path = report.write()
    print("\n" + report.to_markdown())
    print(f"\nwrote {json_path} and {md_path}")
    if not report.ok:
        print(f"\n{len(report.divergences)} divergence(s) observed -> exit 1", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest")
    parser.add_argument("provider", choices=["slack", "github", "discord"])
    parser.add_argument("mode", choices=["historical", "live"])
    parser.add_argument("--max-channels", type=int, default=None,
                        help="cap channels scanned (Slack smoke testing)")
    parser.add_argument("--max-repos", type=int, default=None,
                        help="cap repositories scanned (GitHub smoke testing)")
    parser.add_argument("--max-guilds", type=int, default=None,
                        help="cap guilds scanned (Discord smoke testing)")
    parser.add_argument("--seconds", type=float, default=None,
                        help="time budget for a live listener before it stops on its own")
    args = parser.parse_args(argv)

    if args.provider == "slack":
        from .slack import run as slack_run
        report = (slack_run.run_historical(max_channels=args.max_channels)
                  if args.mode == "historical" else slack_run.run_live())
        return _finish(report)

    if args.provider == "github":
        from .github import run as github_run
        report = (github_run.run_historical(max_repos=args.max_repos)
                  if args.mode == "historical" else github_run.run_live())
        return _finish(report)

    if args.provider == "discord":
        from .discord import run as discord_run
        report = (discord_run.run_historical(max_guilds=args.max_guilds)
                  if args.mode == "historical"
                  else discord_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    print(f"unknown provider {args.provider!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
