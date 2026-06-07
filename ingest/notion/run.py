"""Notion slice orchestration: build config, run historical or live, emit the report."""
from __future__ import annotations

from ..config import NotionConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def run_historical() -> FidelityReport:
    cfg = NotionConfig.from_env()
    report = FidelityReport("notion", cfg.api_base)
    report.note("Notion publishes no official OpenAPI; responses are validated against "
                "the documented contracts hand-authored in specs/notion.openapi.json "
                f"(Notion-Version {cfg.version}).")
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = NotionConfig.from_env()
    report = FidelityReport("notion", cfg.api_base)
    report.note("Live thin-webhook slice: verify X-Notion-Signature (HMAC-SHA256 over the "
                "raw body, keyed by the verification token), contract-check the thin envelope, "
                "and fetch the page back via GET /v1/pages/{id} to validate the Page object.")
    server = WebhookServer("notion-events")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Notion webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
    if run_seconds is not None:
        print(f"Running for {run_seconds}s, then writing the fidelity report.")
        server.serve_for(wcfg, run_seconds)
    else:
        server.run(wcfg)
    return report
