"""Gmail / Calendar slice orchestration: build config, run, emit the report."""
from __future__ import annotations

from ..config import GoogleConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import calendar as calendar_slice
from . import drive as drive_slice
from . import gmail as gmail_slice
from . import gmail_live


def _new_report(provider: str, base_url: str) -> FidelityReport:
    report = FidelityReport(provider, base_url)
    report.note("Google APIs are described by discovery documents (not OpenAPI); response "
                "bodies are validated against the discovery schemas. Discovery does not "
                "declare response-required fields, so validation catches type/shape "
                "deviations rather than missing fields — wire-level checks (status, "
                "pagination, syncToken, rate-limit) carry the rest.")
    return report


def run_gmail(max_users: int | None = None) -> FidelityReport:
    cfg = GoogleConfig.from_env()
    report = _new_report("gmail", cfg.gmail_base)
    gmail_slice.run_historical(cfg, report, max_users=max_users)
    return report


def run_gmail_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = GoogleConfig.from_env()
    report = _new_report("gmail", cfg.gmail_base)
    report.note("Gmail live = Cloud Pub/Sub push (NOT a content webhook): an OIDC-JWT-"
                "authenticated envelope carrying only {emailAddress, historyId}; the "
                "consumer fetches the message via history.list → messages.get. Verified "
                "per developers.google.com/workspace/gmail/api/guides/push.")
    server = WebhookServer("gmail-pubsub")
    gmail_live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Gmail Pub/Sub push listener on http://{wcfg.host}:{wcfg.port}{gmail_live.ENDPOINT}")
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


def run_calendar(max_users: int | None = None) -> FidelityReport:
    cfg = GoogleConfig.from_env()
    report = _new_report("calendar", cfg.calendar_base)
    calendar_slice.run_historical(cfg, report, max_users=max_users)
    return report


def run_drive(max_users: int | None = None) -> FidelityReport:
    cfg = GoogleConfig.from_env()
    report = _new_report("drive", cfg.drive_base)
    drive_slice.run_historical(cfg, report, max_users=max_users)
    return report
