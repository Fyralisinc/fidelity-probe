"""QuickBooks Online historical ingestion — query the four transactional entities.

For each of Invoice / Bill / BillPayment / Payment, page the `query` endpoint via
STARTPOSITION/MAXRESULTS and structurally validate every object against the
documented QBO contract (Intuit publishes no machine-readable schema, so — like
the GitHub/Slack live slices — we assert the fields a consumer actually depends
on: Id, SyncToken, MetaData.LastUpdatedTime, TotalAmt, and the entity-specific
refs/links). Also confirms the `QueryResponse.{Entity}` envelope + 1-based
STARTPOSITION pagination terminates.
"""
from __future__ import annotations

from typing import Any

from ..fidelity import FidelityReport
from .client import QuickBooksClient

_PAGE = 100
_MAX_PAGES = 1000  # safety bound

# entity -> the consumer-critical dotted paths that must be present + non-empty
_REQUIRED: dict[str, tuple[str, ...]] = {
    "Invoice":     ("Id", "SyncToken", "MetaData.LastUpdatedTime", "TxnDate",
                    "CustomerRef.value", "TotalAmt", "Line"),
    "Bill":        ("Id", "SyncToken", "MetaData.LastUpdatedTime", "TxnDate",
                    "VendorRef.value", "TotalAmt", "Line"),
    "BillPayment": ("Id", "SyncToken", "MetaData.LastUpdatedTime", "TxnDate",
                    "VendorRef.value", "TotalAmt", "PayType"),
    "Payment":     ("Id", "SyncToken", "MetaData.LastUpdatedTime", "TxnDate",
                    "CustomerRef.value", "TotalAmt"),
}


def _get(obj: Any, path: str) -> Any:
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _validate(entity: str, obj: dict, report: FidelityReport, seen_ok: set) -> None:
    problems = [p for p in _REQUIRED[entity]
                if _get(obj, p) in (None, "", [])]
    # linked-txn integrity for the settlement entities
    if entity == "BillPayment":
        lt = _get(obj, "Line.0.LinkedTxn") or (obj.get("Line") or [{}])[0].get("LinkedTxn")
        if not lt:
            problems.append("BillPayment has no Line[].LinkedTxn (the settled Bill)")
    if entity == "Payment":
        first = (obj.get("Line") or [{}])[0]
        if not first.get("LinkedTxn"):
            problems.append("Payment has no Line[].LinkedTxn (the applied Invoice)")
    check = f"{entity} object contract"
    if problems:
        report.record_protocol(check, False, f"id={obj.get('Id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def _page_entity(client: QuickBooksClient, entity: str, report: FidelityReport) -> None:
    seen_ok: set = set()
    start = 1
    pages = 0
    total = 0
    while pages < _MAX_PAGES:
        sql = f"SELECT * FROM {entity} STARTPOSITION {start} MAXRESULTS {_PAGE}"
        status, _, body = client.query(sql, f"query.{entity}")
        report.record_page(f"query.{entity}", str(start))
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", f"query.{entity}",
                           f"query {entity} -> {status}; {str(body)[:160]}")
            return
        qr = body.get("QueryResponse")
        if not isinstance(qr, dict):
            report.diverge("protocol", f"query.{entity}",
                           "response has no QueryResponse envelope")
            return
        items = qr.get(entity) or []
        if not isinstance(items, list):
            report.diverge("protocol", f"query.{entity}",
                           f"QueryResponse.{entity} is not an array")
            return
        if "time" not in body:
            report.record_protocol("QBO response carries top-level time", False,
                                   "no top-level `time` sibling of QueryResponse")
        for obj in items:
            report.count(entity.lower())
            _validate(entity, obj, report, seen_ok)
            total += 1
        pages += 1
        if len(items) < _PAGE:
            break
        start += len(items)
    report.record_protocol(f"{entity} pagination terminates", True, "")
    report.note(f"{entity}: {total} ingested over {pages} page(s)")


def run_historical(report: FidelityReport, cfg) -> None:
    client = QuickBooksClient(cfg, report)
    report.auth.update({"method": "OAuth Bearer (Authorization: Bearer …)",
                        "realm": client.realm})
    for entity in ("Invoice", "Bill", "BillPayment", "Payment"):
        _page_entity(client, entity, report)
