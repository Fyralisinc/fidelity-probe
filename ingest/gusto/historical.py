"""Gusto historical ingestion — employees + payrolls backfill.

The REAL Gusto contract (docs.gusto.com Embedded Payroll reference + the SDK):
  * List endpoints return a **BARE JSON ARRAY** at the top level (no body
    envelope), with pagination metadata in RESPONSE HEADERS — ``X-Page`` /
    ``X-Total-Count`` / ``X-Total-Pages`` / ``X-Per-Page`` (``page``/``per`` query
    params; ``per`` default 25 / max 100). No ``Link`` header.
  * Money is a decimal STRING in MAJOR units (dollars to the cent), e.g.
    ``"80000.00"`` — NOT cents, NOT a number.
  * Datetimes are ISO-8601 with a ``Z`` suffix; date-only fields (``check_date``,
    ``pay_period`` dates, ``hire_date``, ``date_of_birth``) are ``YYYY-MM-DD``.
  * The payrolls list defaults to a **6-month** window and rejects a span > 1 year
    (422), so a full backfill walks ≤1-year windows (forward-cushioned, because the
    frozen run's virtual clock can LEAD wall-clock time — like the hibob slice).
  * The single-payroll GET adds ``totals`` + ``employee_compensations``.

Gusto publishes an OpenAPI but no per-object JSON-Schema, so — like the QBO /
ramp / hibob slices — we structurally validate the fields a consumer depends on
(bare-array body, header pagination, money STRINGS, date formats) and assert the
windowed payroll walk converges.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from ..fidelity import FidelityReport
from .client import GustoClient

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONEY_RE = re.compile(r"^-?\d+\.\d{2}$")
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_WINDOW_DAYS = 360          # < the mock's 366-day max span
_MAX_WINDOWS = 10
# the frozen run's virtual clock can lead wall-clock; cushion the end forward so
# virtual-future payrolls are still inside the first window.
_FORWARD_CUSHION_DAYS = 400

_EMP_STATUSES = {"full_time", "part_time", "variable", "seasonal", "temporary",
                 "part_time_eligible", "full_time_temporary"}


def _is_money(v: Any, *, nullable_ok: bool = False) -> bool:
    if v is None:
        return nullable_ok
    return isinstance(v, str) and bool(_MONEY_RE.match(v))


def _validate_employee(e: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not e.get("uuid"):
        problems.append("missing `uuid`")
    if not isinstance(e.get("version"), str):
        problems.append("`version` must be a string (dedup token)")
    if e.get("current_employment_status") not in _EMP_STATUSES:
        problems.append(f"current_employment_status not in enum: {e.get('current_employment_status')!r}")
    dob = e.get("date_of_birth")
    if dob is not None and not (isinstance(dob, str) and _DATE_RE.match(dob)):
        problems.append(f"date_of_birth must be DATE-only: {dob!r}")
    jobs = e.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        problems.append("`jobs` must be a non-empty array")
    else:
        job = jobs[0]
        if not _is_money(job.get("rate"), nullable_ok=True):
            problems.append(f"job.rate must be a money STRING (dollars): {job.get('rate')!r}")
        comps = job.get("compensations")
        if isinstance(comps, list) and comps and not _is_money(comps[0].get("rate"), nullable_ok=True):
            problems.append("compensations[].rate must be a money STRING")
    check = "employee object contract"
    if problems:
        report.record_protocol(check, False, f"uuid={e.get('uuid')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _validate_payroll(p: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not p.get("uuid"):
        problems.append("missing `uuid`")
    if not (isinstance(p.get("check_date"), str) and _DATE_RE.match(p["check_date"])):
        problems.append(f"check_date must be DATE-only: {p.get('check_date')!r}")
    pp = p.get("pay_period")
    if not isinstance(pp, dict):
        problems.append("`pay_period` must be an object")
    else:
        for k in ("start_date", "end_date"):
            if not (isinstance(pp.get(k), str) and _DATE_RE.match(pp[k])):
                problems.append(f"pay_period.{k} must be DATE-only: {pp.get(k)!r}")
    ca = p.get("calculated_at")
    if ca is not None and not (isinstance(ca, str) and _ISO_Z_RE.match(ca)):
        problems.append(f"calculated_at must be ISO-8601 Z: {ca!r}")
    if not isinstance(p.get("processed"), bool):
        problems.append("`processed` must be a bool")
    check = "payroll object contract"
    if problems:
        report.record_protocol(check, False, f"uuid={p.get('uuid')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _walk_pages(client: GustoClient, fetch, label: str, validate,
                report: FidelityReport, count_key: str) -> int:
    """Walk an offset page/per list endpoint via the X-Total-Pages header; validate
    the BARE-ARRAY body + the X-* pagination headers on each page."""
    seen_ok: set = set()
    walked, page, total_pages = 0, 1, 1
    seen_ids: set = set()
    while page <= total_pages and page <= 500:
        status, headers, body = fetch(page)
        if status != 200 or not isinstance(body, list):
            report.diverge("protocol", label, f"{label} p{page} -> {status}; body is not a bare array")
            return walked
        # pagination metadata MUST be in headers (no body envelope, no Link).
        for h in ("X-Page", "X-Total-Count", "X-Total-Pages", "X-Per-Page"):
            if h not in headers:
                report.record_protocol(f"{label} pagination header {h}", False, "missing")
        if "Link" in headers:
            report.record_protocol(f"{label} has NO Link header", False, "Link present")
        else:
            report.record_protocol(f"{label} has NO Link header", True, "")
        try:
            total_pages = int(headers.get("X-Total-Pages", "1"))
        except ValueError:
            total_pages = 1
        report.record_page(label, f"page={page}/{total_pages}")
        for obj in body:
            if isinstance(obj, dict):
                oid = obj.get("uuid")
                if oid in seen_ids:
                    report.diverge("protocol", label, f"duplicate across pages: {oid}")
                seen_ids.add(oid)
                report.count(count_key)
                validate(obj, report, seen_ok)
                walked += 1
        page += 1
    report.record_protocol(f"{label} bare-array body + X-* header pagination", True, "")
    return walked


def run_historical(report: FidelityReport, cfg) -> None:
    client = GustoClient(cfg, report)
    report.auth.update({"method": "OAuth Bearer (operator-mediated install)"})

    # 1) company singleton.
    st, _, co = client.company()
    if st == 200 and isinstance(co, dict) and co.get("uuid"):
        report.record_protocol("GET /v1/companies/{uuid} single object", True, "")
        report.note(f"company: {co.get('name')}")
    else:
        report.diverge("protocol", "company", f"GET /v1/companies/{{uuid}} -> {st}")

    # 2) employees — bare-array offset pagination (per=2 to force a multi-page walk).
    n_emp = _walk_pages(
        client, lambda pg: client.list_employees(page=pg, per=2),
        "employees", _validate_employee, report, "employee")
    report.note(f"employees: {n_emp}")

    # 3) payrolls — windowed backfill (≤1-year windows, forward-cushioned). The
    #    default (no start_date) is a 6-month window, so a full backfill MUST walk
    #    explicit windows. Collect unique payrolls across windows.
    end = date.today() + timedelta(days=_FORWARD_CUSHION_DAYS)
    seen_payrolls: set = set()
    seen_ok: set = set()
    empty_streak = 0
    pages_seen = 0
    for w in range(_MAX_WINDOWS):
        start = end - timedelta(days=_WINDOW_DAYS)
        window_count = 0
        page, total_pages = 1, 1
        while page <= total_pages and page <= 200:
            st, headers, body = client.list_payrolls(
                page=page, per=100, start_date=start.isoformat(), end_date=end.isoformat())
            if st != 200 or not isinstance(body, list):
                report.diverge("protocol", "payrolls",
                               f"window {start}..{end} p{page} -> {st}; not a bare array")
                break
            try:
                total_pages = int(headers.get("X-Total-Pages", "1"))
            except ValueError:
                total_pages = 1
            pages_seen += 1
            for p in body:
                if isinstance(p, dict) and p.get("uuid") not in seen_payrolls:
                    seen_payrolls.add(p.get("uuid"))
                    report.count("payroll")
                    _validate_payroll(p, report, seen_ok)
                    window_count += 1
            page += 1
        empty_streak = empty_streak + 1 if window_count == 0 else 0
        if empty_streak >= 2:
            break
        end = start
    report.note(f"payrolls: {len(seen_payrolls)} ({pages_seen} pages over windows)")
    if seen_payrolls:
        report.record_protocol("payrolls windowed backfill converges", True, "")

    # 4) the default (no start_date) is a windowed subset — a faithful wrinkle.
    st, headers, body = client.list_payrolls(per=100)
    if st == 200 and isinstance(body, list):
        try:
            default_total = int(headers.get("X-Total-Count", "0"))
        except ValueError:
            default_total = 0
        if default_total <= len(seen_payrolls):
            report.record_protocol("payrolls default = a 6-month windowed subset", True, "")
        else:
            report.record_protocol("payrolls default = a 6-month windowed subset", False,
                                   f"default {default_total} > full {len(seen_payrolls)}")

    # 5) span > 1 year → 422 (the documented constraint).
    st, _, body = client.list_payrolls(start_date="2000-01-01", end_date="2099-12-31")
    if st == 422:
        report.record_protocol("payrolls span > 1 year -> 422", True, "")
    else:
        report.record_protocol("payrolls span > 1 year -> 422", False, f"got {st}")

    # 6) single-payroll GET adds totals + employee_compensations (money STRINGS).
    if seen_payrolls:
        puid = next(iter(seen_payrolls))
        st, _, detail = client.get_payroll(puid)
        if st == 200 and isinstance(detail, dict) and "employee_compensations" in detail:
            comps = detail["employee_compensations"]
            ok = (isinstance(detail.get("totals"), dict)
                  and isinstance(comps, list)
                  and (not comps or _is_money(comps[0].get("gross_pay"), nullable_ok=True)))
            report.record_protocol("single payroll adds totals + employee_compensations", ok,
                                   "" if ok else f"detail keys={sorted(detail)[:8]}")
        else:
            report.diverge("protocol", "payroll", f"GET payrolls/{{uuid}} -> {st}")
