"""Deel slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import DeelConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: DeelConfig) -> FidelityReport:
    report = FidelityReport("deel", cfg.base_url or "(unset)")
    report.note("Deel global payroll / contractor payments. Contracts are pulled via "
                "GET /contracts ({data, page:{cursor, total_rows}}, CURSOR-only via "
                "after_cursor); the payment stream is GET /invoices ({data, page:{offset, "
                "total_rows, items_per_page, cursor}}, HYBRID limit/offset/cursor — each "
                "invoice carries contract_id, and a no-status probe returns ONLY paid so a "
                "full backfill passes status=all). Money is a decimal STRING in major units "
                "(NOT cents, NOT a number); timestamps RFC3339 ms+Z. Deel publishes docs but "
                "no per-object JSON-Schema, so objects are validated structurally against the "
                "documented shape + enums.")
    return report


def run_historical() -> FidelityReport:
    cfg = DeelConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = DeelConfig.from_env()
    report = _new_report(cfg)
    report.note("Deel live = the x-deel-signature webhook: a nested "
                "{data:{meta:{event_type, organization_id}, resource:[…]}, timestamp} "
                "envelope signed x-deel-signature = bare-hex HMAC-SHA256 over "
                "'POST'+rawBody (method string prepended; NO sha256= prefix, NOT base64, NO "
                "timestamp), with companion x-deel-hmac-label/x-deel-webhook-version headers. "
                "Verified per developer.deel.com webhook docs.")
    server = WebhookServer("deel-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Deel webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
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
