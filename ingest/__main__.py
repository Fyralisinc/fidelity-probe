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
    parser.add_argument("provider",
                        choices=["slack", "github", "discord", "gmail", "calendar", "notion",
                                 "drive", "jira", "quickbooks", "grafana", "mercury", "ashby",
                                 "brex", "deel"])
    parser.add_argument("mode", choices=["historical", "live"])
    parser.add_argument("--max-channels", type=int, default=None,
                        help="cap channels scanned (Slack smoke testing)")
    parser.add_argument("--max-repos", type=int, default=None,
                        help="cap repositories scanned (GitHub smoke testing)")
    parser.add_argument("--max-guilds", type=int, default=None,
                        help="cap guilds scanned (Discord smoke testing)")
    parser.add_argument("--max-users", type=int, default=None,
                        help="cap mailboxes/users scanned (Gmail/Calendar/Drive smoke testing)")
    parser.add_argument("--max-projects", type=int, default=None,
                        help="cap projects scanned (Jira smoke testing)")
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
                  if args.mode == "historical" else github_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    if args.provider == "discord":
        from .discord import run as discord_run
        report = (discord_run.run_historical(max_guilds=args.max_guilds)
                  if args.mode == "historical"
                  else discord_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    if args.provider == "notion":
        from .notion import run as notion_run
        report = (notion_run.run_historical() if args.mode == "historical"
                  else notion_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    if args.provider == "jira":
        from .jira import run as jira_run
        report = (jira_run.run_historical(max_projects=args.max_projects)
                  if args.mode == "historical" else jira_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    if args.provider == "quickbooks":
        from .quickbooks import run as qb_run
        report = (qb_run.run_historical() if args.mode == "historical"
                  else qb_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    if args.provider == "grafana":
        from .grafana import run as grafana_run
        report = (grafana_run.run_historical() if args.mode == "historical"
                  else grafana_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    if args.provider == "mercury":
        from .mercury import run as mercury_run
        report = (mercury_run.run_historical() if args.mode == "historical"
                  else mercury_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    if args.provider == "ashby":
        from .ashby import run as ashby_run
        report = (ashby_run.run_historical() if args.mode == "historical"
                  else ashby_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    if args.provider == "brex":
        from .brex import run as brex_run
        report = (brex_run.run_historical() if args.mode == "historical"
                  else brex_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    if args.provider == "deel":
        from .deel import run as deel_run
        report = (deel_run.run_historical() if args.mode == "historical"
                  else deel_run.run_live(run_seconds=args.seconds))
        return _finish(report)

    if args.provider == "gmail":
        from .google import run as google_run
        report = (google_run.run_gmail(max_users=args.max_users)
                  if args.mode == "historical"
                  else google_run.run_gmail_live(run_seconds=args.seconds))
        return _finish(report)

    # Calendar / Drive: read/backfill only — live push/webhook delivery isn't wired.
    if args.provider in ("calendar", "drive"):
        if args.mode != "historical":
            print(f"{args.provider} has no live mode yet (push/webhook delivery is not "
                  f"wired); run `historical`.", file=sys.stderr)
            return 2
        from .google import run as google_run
        runner = {"calendar": google_run.run_calendar, "drive": google_run.run_drive}[args.provider]
        return _finish(runner(max_users=args.max_users))

    print(f"unknown provider {args.provider!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
