"""Grafana slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import GrafanaConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: GrafanaConfig) -> FidelityReport:
    report = FidelityReport("grafana", cfg.base_url or "(unset)")
    report.note("Grafana observability. The org-wide annotations stream is pulled via "
                "GET /api/annotations (a BARE JSON array, epoch-ms window, newest-first, "
                "no cursor/Link — a backward time-window walk). It carries both plain "
                "annotations and the auto-created alert-state-change annotations. Grafana "
                "publishes no machine schema, so objects are validated structurally "
                "(incl. the omitempty contract) against the documented shape.")
    return report


def run_historical() -> FidelityReport:
    cfg = GrafanaConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = GrafanaConfig.from_env()
    report = _new_report(cfg)
    report.note("Grafana live = the Alerting webhook (contact point): an Alertmanager-"
                "superset alert group signed X-Grafana-Alerting-Signature = bare lowercase "
                "hex HMAC-SHA256 over the raw body (NO sha256= prefix; Grafana 12.0+). "
                "Verified per grafana.com/docs/.../integrations/webhook-notifier.")
    server = WebhookServer("grafana-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Grafana webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
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
