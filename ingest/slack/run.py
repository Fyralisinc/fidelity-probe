"""Slack slice orchestration: wire auth + historical/live and emit the report."""
from __future__ import annotations

from ..config import SlackConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import auth, historical, live


def _new_report(cfg: SlackConfig) -> FidelityReport:
    report = FidelityReport("slack", cfg.base_url)
    report.note("Slack Web API spec is Swagger 2.0 and incomplete: response objects are "
                "marked additionalProperties:false but the real API returns more fields, "
                "and some response schemas fail their own examples. Extra fields are "
                "recorded as 'undocumented'; schemas whose own example fails are not "
                "counted as divergences.")
    return report


def run_historical(max_channels: int | None = None) -> FidelityReport:
    cfg = SlackConfig.from_env()
    report = _new_report(cfg)
    token = auth.acquire_token(cfg, report)
    client = auth.make_web_client(token, cfg, report)
    historical.run_historical(client, report, max_channels=max_channels)
    return report


def run_live() -> FidelityReport:
    cfg = SlackConfig.from_env()
    report = _new_report(cfg)
    server = WebhookServer("slack-events")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Slack Events API listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
    print("Ctrl-C to stop and write the fidelity report.")
    try:
        server.run(wcfg)
    except KeyboardInterrupt:
        pass
    return report
