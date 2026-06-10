"""Miro slice orchestration: build config, run historical, emit report.

Miro is POLL-ONLY — its experimental webhooks were discontinued on 2025-12-05, so
there is no live slice (incremental ingestion is re-walking /items and dedup'ing on
the versioned external_id).
"""
from __future__ import annotations

from ..config import MiroConfig
from ..fidelity import FidelityReport
from . import historical


def _new_report(cfg: MiroConfig) -> FidelityReport:
    report = FidelityReport("miro", cfg.base_url or "(unset)")
    report.note("Miro collaborative whiteboard (REST API v2). The read surface splits "
                "into TWO paginators: GET /v2/boards is OFFSET-paginated "
                "({data,total,size,offset,limit,links,type}) while GET /v2/boards/{id}/items "
                "is CURSOR-paginated ({data,total,size,cursor,limit,links} — NO top-level "
                "type; `cursor` ABSENT on the last page). The single signal is the board "
                "ITEM (sticky_note/shape/text/card/frame); items have NO version field "
                "(only createdAt/modifiedAt ms-Z), so the versioned dedup key "
                "miro:{org}:item:{id}:{version} versions on modifiedAt. Board users carry "
                "`name`; item createdBy/modifiedBy are {id,type} with NO name. Auth is an "
                "org-app Bearer (scope boards:read). Miro is credit-rate-limited (429 + "
                "X-RateLimit-* headers, NO Retry-After) and POLL-ONLY (webhooks discontinued "
                "2025-12-05). Pinned against Miro's published OpenAPI spec; validated "
                "structurally here.")
    return report


def run_historical() -> FidelityReport:
    cfg = MiroConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report
