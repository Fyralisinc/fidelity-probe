"""Ramp slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import RampConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: RampConfig) -> FidelityReport:
    report = FidelityReport("ramp", cfg.base_url or "(unset)")
    report.note("Ramp corporate cards + bill-pay + reimbursements. Reads are REST list "
                "endpoints with KEYSET pagination — GET /developer/v1/transactions, "
                "/reimbursements, /cards, /users all return {data:[…], page:{next}}, where "
                "page.next is a FULL URL embedding start=<last entity id> (null at EOF), "
                "page_size default 20/max 100. Money is DUAL: the top-level `amount` is a "
                "NUMBER in dollars, nested CurrencyAmount fields are integer cents. "
                "Transactions key `currency_code`; reimbursements key `currency`. Timestamps "
                "are ISO-8601 with a +00:00 offset; reimbursement transaction_date is "
                "DATE-only. Auth is OAuth client-credentials -> Bearer ramp_business_tok_. "
                "Ramp publishes an OpenAPI but no per-object JSON-Schema, so objects are "
                "validated structurally against the documented shape + enums.")
    return report


def run_historical() -> FidelityReport:
    cfg = RampConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = RampConfig.from_env()
    report = _new_report(cfg)
    report.note("Ramp live = the X-Ramp-Signature transaction webhook: a THIN event "
                "{id, type, created_at, business_id, object:{id}} signed X-Ramp-Signature = "
                "<bare lowercase hex HMAC-SHA256(secret, rawBody)> (no prefix, not base64, "
                "no timestamp). Verified per docs.ramp.com/developer-api/v1/guides/webhooks; "
                "fetch-on-notify correlates object.id against GET /developer/v1/transactions/{id}.")
    server = WebhookServer("ramp-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Ramp webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
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
