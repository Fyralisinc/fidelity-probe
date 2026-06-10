"""HiBob slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import HibobConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: HibobConfig) -> FidelityReport:
    report = FidelityReport("hibob", cfg.base_url or "(unset)")
    report.note("HiBob ('Bob') HR platform. People are read via POST /v1/people/search "
                "({employees:[…]} — returns ALL, NO pagination; filters root.id/root.email "
                "equals, showInactive gate). Time-off is GET /v1/timeoff/requests/changes "
                "(a BARE ARRAY of change snapshots windowed by since/to, ~6-month cap — a "
                "full backfill walks ≤6-month windows). Payroll history is "
                "GET /v1/bulk/people/salaries ({results, response_metadata:{next_cursor}}, "
                "CURSOR pagination; base:{value, currency} is a NUMBER in major units, NOT "
                "cents/string). Timestamps are ISO-8601 µs with NO Z; work.* dates DD/MM/YYYY. "
                "HiBob publishes docs but no per-object JSON-Schema, so objects are validated "
                "structurally against the documented shape + enums.")
    return report


def run_historical() -> FidelityReport:
    cfg = HibobConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = HibobConfig.from_env()
    report = _new_report(cfg)
    report.note("HiBob live = the Bob-Signature webhook: a Webhooks-v2 metadata-only "
                "{companyId, type, triggeredBy, triggeredAt, version, data} envelope signed "
                "Bob-Signature = base64(HMAC-SHA512(secret, rawBody)) (no prefix, no "
                "timestamp). data carries IDs only, so the slice fetch-on-notify re-fetches "
                "the record (employee → people/search by id; timeoff → changes feed by "
                "requestId). Verified per apidocs.hibob.com webhook docs.")
    server = WebhookServer("hibob-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"HiBob webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
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
