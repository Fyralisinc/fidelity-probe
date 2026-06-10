"""Brex slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import BrexConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: BrexConfig) -> FidelityReport:
    report = FidelityReport("brex", cfg.base_url or "(unset)")
    report.note("Brex corporate cards + cash management. Accounts are pulled via "
                "GET /v2/accounts/cash ({next_cursor, items} CURSOR page) + "
                "GET /v2/accounts/card (a BARE ARRAY, no pagination); transactions via "
                "GET /v2/transactions/cash/{id} and /v2/transactions/card/primary "
                "({next_cursor, items}, opaque-cursor paged, posted_at_start filter). "
                "Money is a {amount:<int CENTS, signed>, currency} OBJECT (NOT dollars); "
                "transaction dates are DATE-only YYYY-MM-DD. Brex publishes an OpenAPI but "
                "no per-object JSON-Schema, so objects are validated structurally against "
                "the documented shape + enums.")
    return report


def run_historical() -> FidelityReport:
    cfg = BrexConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = BrexConfig.from_env()
    report = _new_report(cfg)
    report.note("Brex live = the Svix-signed transfer webhook: a thin "
                "{event_type, transfer_id, payment_type, return_for_id, company_id} event "
                "signed Webhook-Signature = v1,<base64 HMAC-SHA256 over "
                "'{Webhook-Id}.{Webhook-Timestamp}.{rawBody}'> (key = base64-decode of the "
                "whsec_ secret; timestamp in a SEPARATE header). Verified per "
                "developer.brex.com/docs/webhooks (Svix scheme).")
    server = WebhookServer("brex-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Brex webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
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
