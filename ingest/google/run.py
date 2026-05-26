"""Gmail / Calendar slice orchestration: build config, run, emit the report."""
from __future__ import annotations

from ..config import GoogleConfig
from ..fidelity import FidelityReport
from . import calendar as calendar_slice
from . import drive as drive_slice
from . import gmail as gmail_slice


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
