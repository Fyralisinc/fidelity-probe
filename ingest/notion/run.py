"""Notion slice orchestration: build config, run historical, emit the report."""
from __future__ import annotations

from ..config import NotionConfig
from ..fidelity import FidelityReport
from . import historical


def run_historical() -> FidelityReport:
    cfg = NotionConfig.from_env()
    report = FidelityReport("notion", cfg.api_base)
    report.note("Notion publishes no official OpenAPI; responses are validated against "
                "the documented contracts hand-authored in specs/notion.openapi.json "
                f"(Notion-Version {cfg.version}).")
    historical.run_historical(report, cfg)
    return report
