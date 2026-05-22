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
                        help="cap channels scanned (smoke testing)")
    args = parser.parse_args(argv)

    if args.provider == "slack":
        from .slack import run as slack_run
        report = (slack_run.run_historical(max_channels=args.max_channels)
                  if args.mode == "historical" else slack_run.run_live())
        return _finish(report)

    print(f"{args.provider} slice not built yet (Slack-first; paused for review).",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
