"""Jira slice orchestration: build config, run historical/live, emit the report."""
from __future__ import annotations

from ..config import JiraConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def run_historical(max_projects: int | None = None) -> FidelityReport:
    cfg = JiraConfig.from_env()
    report = FidelityReport("jira", cfg.base_url or "<unset>")
    report.note("Jira Cloud REST v3. /project/search uses classic startAt/total/isLast "
                "pagination; the new /search/jql uses token pagination (nextPageToken/"
                "isLast, no startAt/total) — the old /rest/api/3/search was removed in 2025. "
                "Validated against specs/jira.openapi.json (hand-authored from the docs).")
    historical.run_historical(report, cfg, max_projects=max_projects)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = JiraConfig.from_env()
    report = FidelityReport("jira", cfg.base_url or "<unset>")
    report.note("Jira dynamic webhooks: HMAC-SHA256 X-Hub-Signature (sha256=<hex>) over the "
                "raw body, no timestamp/replay window; envelope timestamp is integer epoch-ms. "
                "Verified per developer.atlassian.com/cloud/jira/platform/webhooks/.")
    server = WebhookServer("jira-events")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Jira webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
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
