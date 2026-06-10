"""Grafana historical ingestion — the org-wide annotations backfill.

Grafana's annotations endpoint is a **bare JSON array**, newest-first, with NO
opaque cursor or Link header: pagination is a **backward time-window walk** — each
page is fetched newest-first, then the next page's upper bound (`to`) is set to
``min(time seen) − 1ms``; a page shorter than ``limit`` is the last page.

The single stream carries BOTH plain annotations (manual notes / deploy markers)
and the auto-created alert-state-change annotations (which carry
``alertId``/``newState``/``prevState``). Grafana publishes no machine-readable
schema for this endpoint, so — like the QBO/GitHub slices — we structurally
validate the fields a consumer actually depends on, and assert the documented
``omitempty`` behavior (a plain annotation must NOT carry ``alertId``; an alert
annotation must NOT carry user identity).
"""
from __future__ import annotations

from typing import Any

from ..fidelity import FidelityReport
from .client import GrafanaClient

_PAGE = 100
_MAX_PAGES = 1000  # safety bound


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _validate(anno: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not _is_int(anno.get("id")):
        problems.append("missing/!int `id`")
    # epoch-MILLISECONDS integers (NOT seconds, NOT RFC3339 strings)
    for k in ("time", "timeEnd"):
        v = anno.get(k)
        if not _is_int(v):
            problems.append(f"`{k}` must be an epoch-ms integer")
        elif v and v < 1_000_000_000_000:
            problems.append(f"`{k}`={v} looks like epoch-SECONDS, not milliseconds")
    if "tags" in anno and not isinstance(anno["tags"], list):
        problems.append("`tags` present but not an array")

    is_alert = bool(anno.get("alertId"))
    if is_alert:
        # alert-state-change annotation contract
        if not _is_int(anno.get("alertId")) or anno["alertId"] <= 0:
            problems.append("alert annotation: `alertId` must be a positive int")
        if not anno.get("newState"):
            problems.append("alert annotation: missing `newState`")
        # omitempty: a machine alert annotation must not carry user identity
        for forbidden in ("userId", "login", "email"):
            if forbidden in anno:
                problems.append(f"alert annotation carries `{forbidden}` "
                                "(machine annotations omit user identity)")
    else:
        # plain annotation contract — omitempty means NO alert keys at all
        for forbidden in ("alertId", "newState", "prevState", "alertName"):
            if forbidden in anno:
                problems.append(f"plain annotation carries `{forbidden}` "
                                "(omitempty: a zero alertId must be dropped, not sent)")

    check = "annotation object contract"
    if problems:
        report.record_protocol(check, False, f"id={anno.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def run_historical(report: FidelityReport, cfg) -> None:
    client = GrafanaClient(cfg, report)
    report.auth.update({"method": "Service-account Bearer (Authorization: Bearer glsa_…)"})

    # 1) connectivity / credential probe
    status, _, org = client.get_org()
    if status == 200 and isinstance(org, dict) and "id" in org and "name" in org:
        report.record_protocol("GET /api/org probe", True, "")
        report.note(f"org id={org['id']} name={org['name']!r}")
    else:
        report.record_protocol("GET /api/org probe", False, f"/api/org -> {status}; {str(org)[:160]}")

    # 2) backward time-window walk over /api/annotations
    seen_ok: set = set()
    seen_ids: set = set()
    to: int | None = None
    pages = 0
    total = 0
    alert_n = 0
    high_water: int | None = None
    order_ok = True
    while pages < _MAX_PAGES:
        status, _, body = client.list_annotations(to=to, limit=_PAGE)
        report.record_page("annotations", str(to) if to is not None else "head")
        if status != 200:
            report.diverge("protocol", "annotations", f"GET /api/annotations -> {status}; {str(body)[:160]}")
            return
        if not isinstance(body, list):
            report.diverge("protocol", "annotations",
                           "GET /api/annotations must return a BARE JSON array, got "
                           f"{type(body).__name__}")
            return
        pages += 1
        if not body:
            break
        page_times: list[int] = []
        prev_time: int | None = None
        for anno in body:
            if not isinstance(anno, dict):
                report.diverge("protocol", "annotations", "annotation element is not an object")
                continue
            report.count("annotation")
            _validate(anno, report, seen_ok)
            aid, t = anno.get("id"), anno.get("time")
            if anno.get("alertId"):
                alert_n += 1
            if isinstance(t, int):
                page_times.append(t)
                high_water = t if high_water is None else max(high_water, t)
                # newest-first ordering check within the page
                if prev_time is not None and t > prev_time and order_ok:
                    order_ok = False
                    report.record_protocol("annotations newest-first ordering", False,
                                           f"id={aid} time {t} > previous {prev_time}")
                prev_time = t
            # external_id dedup is versioned by time: grafana:{instance}:annotation:{id}:{time}
            key = (aid, t)
            if key in seen_ids:
                report.record_protocol("annotation ids unique within walk", False,
                                       f"duplicate (id,time)={key} across pages")
            seen_ids.add(key)
            total += 1
        if len(body) < _PAGE:
            break  # short page = EOF
        if not page_times:
            break
        to = min(page_times) - 1  # backward walk: next upper bound

    if order_ok:
        report.record_protocol("annotations newest-first ordering", True, "")
    report.record_protocol("annotations pagination terminates (short page = EOF)", True, "")
    report.note(f"annotations: {total} over {pages} page(s); {alert_n} alert-state-change, "
                f"{total - alert_n} plain; high_water_time_ms={high_water}")
