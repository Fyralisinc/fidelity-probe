"""Carta slice orchestration: build config, run historical, emit report.

Carta is POLL-ONLY — it has NO webhook / push of any kind, so there is no live
slice (incremental ingestion is re-walking the cap-table collections and dedup'ing
on the versioned external_id).
"""
from __future__ import annotations

from ..config import CartaConfig
from ..fidelity import FidelityReport
from . import historical


def _new_report(cfg: CartaConfig) -> FidelityReport:
    report = FidelityReport("carta", cfg.base_url or "(unset)")
    report.note("Carta cap-table / equity-management (REST API v1alpha1, Google-AIP "
                "conventions). Reads are collections under /v1alpha1/issuers/{id}/… — "
                "stakeholders, shareClasses, optionGrants, convertibleNotes (SAFEs) — with "
                "AIP-158 token pagination: pageSize (default 25) + opaque pageToken, the "
                "response wraps the list under its PLURAL key alongside nextPageToken, which "
                "is ABSENT on the last page (EOF). Single GETs wrap under a SINGULAR key "
                "({issuer:{…}}). MONEY + every decimal/quantity is a PROTOBUF WRAPPER whose "
                "value is a decimal STRING (Money = {currencyCode:{value},amount:{value}}; "
                "decimal = {value:'…'}) — NOT a number, NOT cents. IDs are mixed "
                "(numeric-string issuer-suite ids vs UUID securityId); datetimes are "
                "RFC3339-µs-Z, dates YYYY-MM-DD; there is NO SyncToken. Auth is OAuth "
                "client-credentials -> Bearer (no refresh_token; re-mint ~1h). 429 carries "
                "RateLimit-*/X-RateLimit-*-Second/-Minute headers and NO Retry-After. Carta "
                "is POLL-ONLY (no webhook). Validated structurally against the documented "
                "shape + enums.")
    return report


def run_historical() -> FidelityReport:
    cfg = CartaConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report
