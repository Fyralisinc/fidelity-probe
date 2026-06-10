"""Fireflies slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import FirefliesConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: FirefliesConfig) -> FidelityReport:
    report = FidelityReport("fireflies", cfg.base_url or "(unset)")
    report.note("Fireflies AI meeting-notetaker. The API is GraphQL — a single "
                "POST /graphql exposing transcripts(skip, limit≤50, fromDate, toDate) "
                "→ a plain [Transcript] array (newest-first; NO total/pageInfo, short "
                "page = EOF), transcript(id: String!) → one Transcript, and user (no "
                "id) → the API-key owner (Fireflies' real token-verify; there is NO "
                "first-class workspace id). A Transcript's `date` is a Float epoch-"
                "MILLISECONDS; `dateString` is the separate ISO-8601 ...Z string; "
                "`duration` is a Number in MINUTES; there is NO version/updatedAt field "
                "(the dedup content-version is derived from `date`). Auth is a single "
                "long-lived Bearer API token. Fireflies publishes no per-object JSON-"
                "Schema, so objects are validated structurally against the documented "
                "shape.")
    return report


def run_historical() -> FidelityReport:
    cfg = FirefliesConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = FirefliesConfig.from_env()
    report = _new_report(cfg)
    report.note("Fireflies live = the x-hub-signature transcript webhook: a THIN V2 "
                "event {event, timestamp, meeting_id, client_reference_id} signed "
                "x-hub-signature = sha256=<hex HMAC-SHA256(secret, rawBody)> (the legacy "
                "x-hub-signature header name but a SHA-256 digest with the sha256= prefix, "
                "over the body alone — no timestamp). Verified per "
                "docs.fireflies.ai/graphql-api/webhooks-v2; fetch-on-notify correlates "
                "meeting_id against the GraphQL transcript(id:) query.")
    server = WebhookServer("fireflies-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Fireflies webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
    if run_seconds is not None:
        print(f"Running for {run_seconds}s, then writing the fidelity report.")
        server.serve_for(wcfg, run_seconds)
        return report
    print("Ctrl-C to stop and write the fidelity report.")
    try:
        server.run(wcfg)
    except KeyboardInterrupt:
        pass
    return report
