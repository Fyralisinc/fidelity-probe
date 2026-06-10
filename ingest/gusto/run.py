"""Gusto slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import GustoConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: GustoConfig) -> FidelityReport:
    report = FidelityReport("gusto", cfg.base_url or "(unset)")
    report.note("Gusto payroll + HR platform. Reads are REST list endpoints that return a "
                "BARE JSON ARRAY at the top level (NO body envelope) with pagination "
                "metadata in RESPONSE HEADERS — X-Page / X-Total-Count / X-Total-Pages / "
                "X-Per-Page (page/per query params, per default 25/max 100); no Link header. "
                "GET /v1/companies/{uuid}/employees + /payrolls + the single company object + "
                "the single-payroll GET (adds totals + employee_compensations). Money is a "
                "decimal STRING in dollars ('80000.00') — NOT cents, NOT a number. Datetimes "
                "are ISO-8601 Z; date-only fields are YYYY-MM-DD. The payrolls list defaults "
                "to a 6-month window and rejects a >1-year span (422), so a full backfill "
                "walks <=1-year windows. Auth is OAuth Bearer (operator-mediated install), "
                "every request carries X-Gusto-API-Version. Gusto publishes an OpenAPI but no "
                "per-object JSON-Schema, so objects are validated structurally.")
    return report


def run_historical() -> FidelityReport:
    cfg = GustoConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = GustoConfig.from_env()
    report = _new_report(cfg)
    report.note("Gusto live = the X-Gusto-Signature webhook: a THIN event {uuid, event_type, "
                "resource_type, resource_uuid, entity_type, entity_uuid, timestamp} where "
                "timestamp is a numeric Unix EPOCH, signed X-Gusto-Signature = <lowercase hex "
                "HMAC-SHA256(verification_token, rawBody)> (no prefix, no timestamp in the "
                "signed bytes). Verified per docs.gusto.com/embedded-payroll/docs/webhooks; "
                "fetch-on-notify correlates resource_uuid against GET "
                "/v1/companies/{co}/payrolls/{uuid}. (The hex-vs-base64 encoding is the one "
                "INFERRED contract detail; defaulting to hex.)")
    server = WebhookServer("gusto-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Gusto webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
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
