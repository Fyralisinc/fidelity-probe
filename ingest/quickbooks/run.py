"""QuickBooks slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import QuickBooksConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: QuickBooksConfig) -> FidelityReport:
    report = FidelityReport("quickbooks", cfg.base_url)
    report.note("QuickBooks Online Accounting API v3. The four transactional entities "
                "(Invoice/Bill/BillPayment/Payment) are read via the SQL `query` endpoint "
                "(QueryResponse.{Entity} envelope, 1-based STARTPOSITION/MAXRESULTS, "
                "minorversion). Intuit publishes no machine-readable schema, so objects are "
                "validated structurally against the documented contract.")
    return report


def run_historical() -> FidelityReport:
    cfg = QuickBooksConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = QuickBooksConfig.from_env()
    report = _new_report(cfg)
    report.note("QBO live = Intuit `eventNotifications` webhook: a THIN notification "
                "(realmId + entity name/id/operation/lastUpdated, no body) signed with "
                "`intuit-signature` = base64(HMAC-SHA256(rawBody, verifierToken)). The "
                "consumer re-queries the entity to fetch it. Verified per "
                "developer.intuit.com/app/developer/qbo/docs/develop/webhooks.")
    server = WebhookServer("quickbooks-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"QuickBooks webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
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
