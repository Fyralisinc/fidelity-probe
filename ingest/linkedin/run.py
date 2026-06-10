"""LinkedIn slice orchestration: build config, run historical, emit report.

LinkedIn org data is POLL-ONLY — it has NO webhook / push of any kind, so there is no
live slice (incremental ingestion is re-walking the org-page collections and dedup'ing
on the entity-kind-discriminated external_id).
"""
from __future__ import annotations

from ..config import LinkedinConfig
from ..fidelity import FidelityReport
from . import historical


def _new_report(cfg: LinkedinConfig) -> FidelityReport:
    report = FidelityReport("linkedin", cfg.base_url or "(unset)")
    report.note("LinkedIn organization marketing / Community-Management API (REST /rest/, "
                "Rest.li 2.0 conventions). Reads are FINDER collections scoped by an "
                "organization URN — GET /rest/posts?q=author (OFFSET start/count, default "
                "10/max 100; envelope {elements, paging:{start,count,links}}; EOF = a page "
                "with fewer elements than count) + organizationalEntityShareStatistics + "
                "organizationalEntityFollowerStatistics (q=organizationalEntity, single "
                "lifetime elements row). A post id is a urn:li:share|ugcPost:{n} URN; "
                "createdAt/lastModifiedAt/publishedAt are epoch-MILLIS integers (NOT ISO). "
                "totalShareStatistics has exactly seven counters + engagement; follower "
                "stats are facet arrays with NO lifetime total. Every versioned call needs "
                "Linkedin-Version:YYYYMM + X-Restli-Protocol-Version:2.0.0 + Bearer; the "
                "error envelope is the classic {message,serviceErrorCode,status}; 429 has "
                "NO Retry-After / NO X-RateLimit-* (a documented absence). LinkedIn org "
                "data is POLL-ONLY (no webhook). Validated structurally against the "
                "documented shape + enums.")
    return report


def run_historical() -> FidelityReport:
    cfg = LinkedinConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report
