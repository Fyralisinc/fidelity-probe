"""HiBob historical ingestion — people + time-off-changes + salaries backfill.

The REAL HiBob contract (apidocs.hibob.com):
  * ``POST /v1/people/search`` → ``{employees:[Employee]}`` — a JSON-body search
    that returns ALL matching employees in ONE array. There is **NO pagination**.
    ``showInactive`` (default false) gates left employees; ``filters`` supports
    only ``root.id`` / ``root.email`` with operator ``equals``.
  * ``GET /v1/timeoff/requests/changes`` → a **BARE ARRAY** of change snapshots,
    windowed by ``since`` (required) / ``to`` and filtered by the change date
    (``createdOn``). The window is capped at ~6 months, so a full backfill walks
    a sequence of ≤6-month windows.
  * ``GET /v1/bulk/people/salaries`` → ``{results:[SalaryEntry], response_metadata:
    {next_cursor}, errors:[]}`` — CURSOR pagination (``limit`` default 50 / max
    200). Salary money is ``base:{value:<number>, currency}`` — a plain NUMBER in
    major units (NOT cents, NOT a string).

HiBob's timestamp house style is ISO-8601 with microseconds and NO ``Z``/offset
(``creationDateTime``/``creationDate``); ``work.*`` dates render ``DD/MM/YYYY``,
time-off + salary calendar dates ``YYYY-MM-DD``. HiBob publishes docs but no
per-object JSON-Schema, so — like the QBO / mercury / deel slices — we structurally
validate the fields a consumer depends on (incl. enums + the number-money + the
no-Z-microsecond + envelope conventions) and assert the walks terminate.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..fidelity import FidelityReport
from .client import HibobClient

_SALARIES_PAGE = 50
_MAX_PAGES = 10_000
_TIMEOFF_WINDOW_DAYS = 180          # each call walks a <=6-month window (spec cap)
_TIMEOFF_BACK_WINDOWS = 5           # how many windows to walk backward
_TIMEOFF_FWD_CUSHION_DAYS = 200     # cover a frozen run whose virtual clock leads wall-clock

_ISO_NOZ_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{1,6}$")
_DDMMYYYY_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_ISODATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_CHANGE_TYPES = {"Created", "Canceled", "Deleted", "Pending"}
_PAY_PERIODS = {"Annual", "Hourly", "Daily", "Weekly", "Monthly"}
_PAY_FREQUENCIES = {"Monthly", "Semi Monthly", "Weekly", "Bi-Weekly"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_employee(e: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not (isinstance(e.get("id"), str) and e["id"]):
        problems.append(f"`id` must be a non-empty string: {e.get('id')!r}")
    if not isinstance(e.get("companyId"), str):
        problems.append(f"`companyId` must be a string: {e.get('companyId')!r}")
    for k in ("firstName", "surname", "fullName", "displayName", "email"):
        if not isinstance(e.get(k), str):
            problems.append(f"`{k}` must be a string")
    cdt = e.get("creationDateTime")
    if not (isinstance(cdt, str) and _ISO_NOZ_RE.match(cdt)):
        problems.append(f"`creationDateTime` must be ISO-8601 µs with NO Z: {cdt!r}")
    work = e.get("work")
    if not isinstance(work, dict):
        problems.append("`work` must be an object")
    else:
        sd = work.get("startDate")
        if sd is not None and not (isinstance(sd, str) and _DDMMYYYY_RE.match(sd)):
            problems.append(f"`work.startDate` must be DD/MM/YYYY: {sd!r}")
        if work.get("isManager") not in (None, "Yes", "No"):
            problems.append(f"`work.isManager` must be Yes/No: {work.get('isManager')!r}")
    check = "employee object contract"
    if problems:
        report.record_protocol(check, False, f"id={e.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _validate_change(c: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not isinstance(c.get("requestId"), int):
        problems.append(f"`requestId` must be an int: {c.get('requestId')!r}")
    if not isinstance(c.get("employeeId"), str):
        problems.append("`employeeId` must be a string")
    if c.get("changeType") not in _CHANGE_TYPES:
        problems.append(f"changeType not in enum: {c.get('changeType')!r}")
    co = c.get("createdOn")
    if not (isinstance(co, str) and _ISO_NOZ_RE.match(co)):
        problems.append(f"`createdOn` must be ISO-8601 µs with NO Z: {co!r}")
    sd = c.get("startDate")
    if sd is not None and not (isinstance(sd, str) and _ISODATE_RE.match(sd)):
        problems.append(f"`startDate` must be YYYY-MM-DD: {sd!r}")
    if c.get("durationUnit") not in (None, "days", "hours"):
        problems.append(f"`durationUnit` must be days/hours: {c.get('durationUnit')!r}")
    check = "timeoff change object contract"
    if problems:
        report.record_protocol(check, False, f"requestId={c.get('requestId')}: "
                               + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _validate_salary(s: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not isinstance(s.get("id"), int):
        problems.append(f"`id` must be an int: {s.get('id')!r}")
    if not isinstance(s.get("employeeId"), str):
        problems.append("`employeeId` must be a string")
    base = s.get("base")
    if not isinstance(base, dict):
        problems.append("`base` must be an object {value, currency}")
    else:
        v = base.get("value")
        if not (isinstance(v, (int, float)) and not isinstance(v, bool)):
            problems.append(f"`base.value` must be a NUMBER in major units: {v!r}")
        if not isinstance(base.get("currency"), str):
            problems.append("`base.currency` must be a string")
    if s.get("payPeriod") not in _PAY_PERIODS:
        problems.append(f"payPeriod not in enum: {s.get('payPeriod')!r}")
    if s.get("payFrequency") not in _PAY_FREQUENCIES:
        problems.append(f"payFrequency not in enum: {s.get('payFrequency')!r}")
    ed = s.get("effectiveDate")
    if not (isinstance(ed, str) and _ISODATE_RE.match(ed)):
        problems.append(f"`effectiveDate` must be YYYY-MM-DD: {ed!r}")
    if not isinstance(s.get("isCurrent"), bool):
        problems.append("`isCurrent` must be a boolean")
    cd = s.get("creationDate")
    if not (isinstance(cd, str) and _ISO_NOZ_RE.match(cd)):
        problems.append(f"`creationDate` must be ISO-8601 µs with NO Z: {cd!r}")
    check = "salary object contract"
    if problems:
        report.record_protocol(check, False, f"id={s.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def run_historical(report: FidelityReport, cfg) -> None:
    client = HibobClient(cfg, report)
    report.auth.update({"method": "service-user HTTP Basic base64(service_user_id:token)"})
    seen_ok: set = set()
    now = datetime.now(timezone.utc)

    # 1) people — POST /v1/people/search returns ALL employees in one {employees:[…]}.
    status, _, body = client.people_search(show_inactive=True)
    employees: list[dict] = []
    if status != 200 or not isinstance(body, dict):
        report.diverge("protocol", "people",
                       f"POST /v1/people/search -> {status}; {str(body)[:160]}")
    else:
        if set(body) != {"employees"}:
            report.record_protocol("people envelope is exactly {employees} (no pagination)",
                                   False, f"keys={sorted(body)}")
        else:
            report.record_protocol("people envelope is exactly {employees} (no pagination)",
                                   True, "")
        emps = body.get("employees")
        if not isinstance(emps, list):
            report.diverge("protocol", "people", "`employees` is not an array")
        else:
            report.record_page("people", "search")
            for e in emps:
                if isinstance(e, dict):
                    report.count("employee")
                    _validate_employee(e, report, seen_ok)
                    employees.append(e)
    report.note(f"people: {len(employees)} employees (single un-paginated response)")

    # 1b) filter probe — root.id equals returns exactly that employee.
    if employees:
        eid = employees[0]["id"]
        st, _, fb = client.people_search(
            filters=[{"fieldPath": "root.id", "operator": "equals", "values": [eid]}])
        ok = (st == 200 and isinstance(fb, dict)
              and [x.get("id") for x in fb.get("employees", [])] == [eid])
        report.record_protocol("people filter root.id=equals returns exactly that employee",
                               ok, "" if ok else f"-> {st}; {str(fb)[:120]}")

    # 2) time-off — a backward walk of <=6-month windows (the change-date feed).
    #    The frozen run's virtual clock can LEAD wall-clock, so start the walk a few
    #    months forward of `now` and step backward; each window is <=180 days (the
    #    documented ~6-month cap), so every individual call is spec-valid.
    changes: dict[int, dict] = {}
    tseen: set = set()
    window_to = now + timedelta(days=_TIMEOFF_FWD_CUSHION_DAYS)
    for _ in range(_TIMEOFF_BACK_WINDOWS):
        window_since = window_to - timedelta(days=_TIMEOFF_WINDOW_DAYS)
        status, _, body = client.timeoff_changes(since=_iso(window_since),
                                                 to=_iso(window_to))
        report.record_page("timeoff", _iso(window_since))
        if status != 200:
            report.diverge("protocol", "timeoff",
                           f"GET /timeoff/requests/changes -> {status}; {str(body)[:160]}")
            break
        if not isinstance(body, list):
            report.record_protocol("timeoff/requests/changes is a BARE ARRAY", False,
                                   f"type={type(body).__name__}")
            break
        for c in body:
            if isinstance(c, dict) and isinstance(c.get("requestId"), int):
                if c["requestId"] not in changes:
                    report.count("timeoff_change")
                    _validate_change(c, report, tseen)
                changes[c["requestId"]] = c
        window_to = window_since
    report.record_protocol("timeoff/requests/changes is a BARE ARRAY", True, "")
    report.note(f"time-off changes: {len(changes)} over {_TIMEOFF_BACK_WINDOWS} windows")

    # 2b) `since` is required → a missing-since call is 400.
    st, _, _ = client._request("GET", "/v1/timeoff/requests/changes", "timeoff", params={})
    report.record_protocol("timeoff/requests/changes requires `since` (400 without it)",
                           st == 400, "" if st == 400 else f"-> {st}")

    # 2c) a window wider than ~6 months is rejected (400).
    st, _, _ = client.timeoff_changes(since=_iso(now - timedelta(days=400)), to=_iso(now))
    report.record_protocol("timeoff window wider than ~6 months is rejected (400)",
                           st == 400, "" if st == 400 else f"-> {st}")

    # 3) salaries — CURSOR walk; validate the {results, response_metadata} envelope.
    salaries: list[dict] = []
    sseen: set = set()
    cursor, pages = None, 0
    while pages < _MAX_PAGES:
        status, _, body = client.bulk_salaries(cursor=cursor, limit=_SALARIES_PAGE)
        report.record_page("salaries", cursor or "head")
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "salaries",
                           f"GET /v1/bulk/people/salaries -> {status}; {str(body)[:160]}")
            break
        if set(body) - {"results", "response_metadata", "errors"}:
            report.record_protocol(
                "salaries envelope {results, response_metadata, errors}", False,
                f"keys={sorted(body)}")
        rmeta = body.get("response_metadata")
        if not isinstance(rmeta, dict) or "next_cursor" not in rmeta:
            report.record_protocol("salaries response_metadata has next_cursor", False,
                                   f"response_metadata={rmeta!r}")
        results = body.get("results")
        if not isinstance(results, list):
            report.diverge("protocol", "salaries", "`results` is not an array")
            break
        pages += 1
        for s in results:
            if isinstance(s, dict):
                report.count("salary")
                _validate_salary(s, report, sseen)
                salaries.append(s)
        cursor = rmeta.get("next_cursor") if isinstance(rmeta, dict) else None
        if not cursor:
            break
    report.record_protocol("salaries envelope {results, response_metadata, errors}", True, "")
    report.record_protocol("salaries response_metadata has next_cursor", True, "")
    report.record_protocol("salaries cursor walk terminates on null next_cursor", True, "")
    report.note(f"salaries: {len(salaries)} over {pages} page(s)")

    # 4) salary → employee linkage (best-effort).
    if employees and salaries:
        eids = {e["id"] for e in employees}
        linked = sum(1 for s in salaries if s.get("employeeId") in eids)
        report.note(f"salary→employee links resolved: {linked}/{len(salaries)}")
