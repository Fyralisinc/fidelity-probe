"""Jira slice orchestration: build config, run historical, emit the report."""
from __future__ import annotations

from ..config import JiraConfig
from ..fidelity import FidelityReport
from . import historical


def run_historical(max_projects: int | None = None) -> FidelityReport:
    cfg = JiraConfig.from_env()
    report = FidelityReport("jira", cfg.base_url or "<unset>")
    report.note("Jira Cloud REST v3. /project/search uses classic startAt/total/isLast "
                "pagination; the new /search/jql uses token pagination (nextPageToken/"
                "isLast, no startAt/total) — the old /rest/api/3/search was removed in 2025. "
                "Validated against specs/jira.openapi.json (hand-authored from the docs).")
    historical.run_historical(report, cfg, max_projects=max_projects)
    return report
