"""Mercury slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import MercuryConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: MercuryConfig) -> FidelityReport:
    report = FidelityReport("mercury", cfg.base_url or "(unset)")
    report.note("Mercury business banking. Accounts are pulled via GET /accounts "
                "({accounts, page} UUID cursor); transactions via "
                "GET /account/{id}/transactions ({total, transactions}, OFFSET paged, "
                "newest-first, with a 30-DAY DEFAULT window — a full backfill passes an "
                "explicit wide `start`). Amounts are DOLLARS (signed: negative=debit). "
                "Mercury publishes no machine schema, so objects are validated "
                "structurally against the documented shape + enums.")
    return report


def run_historical() -> FidelityReport:
    cfg = MercuryConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = MercuryConfig.from_env()
    report = _new_report(cfg)
    report.note("Mercury live = the transaction webhook: a JSON-merge-patch event signed "
                "Mercury-Signature = t=<unix_seconds>,v1=<hex HMAC-SHA256 over "
                "'{t}.{rawBody}'> (bare hex, NO sha256= prefix, NOT base64). "
                "Verified per docs.mercury.com/reference/webhooks.")
    server = WebhookServer("mercury-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Mercury webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
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
