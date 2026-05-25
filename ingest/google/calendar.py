"""Google Calendar historical ingestion + incremental sync.

Per user (per-user bearer via domain-wide delegation): list calendars
(calendarList.list), then for each calendar page the full event list
(events.list, paginated by pageToken) — the final page carries a `nextSyncToken`.
We then exercise the two sync paths the integration model calls out:

  * incremental sync: re-request events with `syncToken=<captured>` (expects 200 with
    only changes since the snapshot), and
  * expired token: request with a deliberately invalid `syncToken` — Google answers
    `410 Gone` with reason `fullSyncRequired`, signalling a full resync is needed.

Responses are validated against the official Calendar discovery schemas (CalendarList,
Events). 410 on the bad token is recorded as a passing protocol check (the documented
contract); anything else is a divergence.
"""
from __future__ import annotations

from urllib.parse import quote

import requests

from ..config import CALENDAR_SCOPE, GoogleConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from . import auth, directory, transport

_EVENT_PAGE = 25
_BAD_SYNC_TOKEN = "CPjSt-----INVALID-----SYNC-----TOKEN"


def _page_events(cfg: GoogleConfig, token: str, cal_id: str, sv: SpecValidator,
                 report: FidelityReport, session: requests.Session) -> str | None:
    """Page a calendar's full event list; return the final nextSyncToken (if any)."""
    quoted = quote(cal_id, safe="")
    url = f"{cfg.calendar_base}/calendars/{quoted}/events"
    page_token: str | None = None
    sync_token: str | None = None
    while True:
        params = {"maxResults": _EVENT_PAGE}
        if page_token:
            params["pageToken"] = page_token
        status, _, body = transport.get(session, url, token, "calendar.events.list", report, params)
        report.record_page("calendar.events.list", page_token)
        if status != 200:
            report.diverge("protocol", "calendar.events.list",
                           f"GET {url} -> {status}; body={str(body)[:160]}")
            return None
        sv.validate_against_component(body, "Events", report)
        report.count("event", len(body.get("items") or []))
        sync_token = body.get("nextSyncToken") or sync_token
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    return sync_token


def _verify_sync_paths(cfg: GoogleConfig, token: str, cal_id: str, sync_token: str | None,
                       sv: SpecValidator, report: FidelityReport,
                       session: requests.Session) -> None:
    quoted = quote(cal_id, safe="")
    url = f"{cfg.calendar_base}/calendars/{quoted}/events"

    if sync_token:
        status, _, body = transport.get(session, url, token, "calendar.events.sync", report,
                                        {"syncToken": sync_token})
        ok = status == 200
        if ok:
            sv.validate_against_component(body, "Events", report)
        report.record_protocol(
            "Calendar incremental sync (syncToken)", ok,
            "" if ok else f"syncToken request returned {status}, expected 200")
    else:
        report.note("calendar: no nextSyncToken returned on the final events page; "
                    "incremental sync path not exercised for this calendar")

    # Expired/invalid syncToken must yield 410 fullSyncRequired.
    status, _, body = transport.get(session, url, token, "calendar.events.expired", report,
                                    {"syncToken": _BAD_SYNC_TOKEN})
    reason = ""
    if isinstance(body, dict):
        errs = body.get("error", {}).get("errors", [])
        reason = errs[0].get("reason", "") if errs else ""
    ok = status == 410 and reason == "fullSyncRequired"
    report.record_protocol(
        "Calendar expired-syncToken → 410 fullSyncRequired", ok,
        "" if ok else f"invalid syncToken returned {status} reason={reason!r}, "
                      f"expected 410/fullSyncRequired")


def _ingest_user(cfg: GoogleConfig, key_pem: str, user: str, sv: SpecValidator,
                 report: FidelityReport, session: requests.Session,
                 max_calendars: int | None) -> None:
    token = auth.fetch_token(cfg, key_pem, cfg.calendar_token_url, user, CALENDAR_SCOPE,
                             report, session=session)
    status, _, cl = transport.get(session, f"{cfg.calendar_base}/users/me/calendarList",
                                  token, "calendar.calendarList", report)
    if status != 200:
        report.diverge("protocol", "calendar.calendarList",
                       f"calendarList -> {status}; body={str(cl)[:160]}")
        return
    sv.validate_against_component(cl, "CalendarList", report)
    cals = cl.get("items") or []
    if max_calendars is not None:
        cals = cals[:max_calendars]
    report.count("calendar", len(cals))
    for cal in cals:
        cal_id = cal.get("id")
        if not cal_id:
            continue
        sync_token = _page_events(cfg, token, cal_id, sv, report, session)
        _verify_sync_paths(cfg, token, cal_id, sync_token, sv, report, session)


def run_historical(cfg: GoogleConfig, report: FidelityReport,
                   max_users: int | None = None,
                   max_calendars: int | None = None) -> None:
    sv_dir = SpecValidator("admin_directory")
    sv_cal = SpecValidator("calendar")
    key_pem = auth.resolve_key(cfg, report)
    session = requests.Session()
    users = directory.list_users(cfg, key_pem, sv_dir, report, session)
    emails = [u["primaryEmail"] for u in users if u.get("primaryEmail")]
    if max_users is not None:
        emails = emails[:max_users]
    for email in emails:
        _ingest_user(cfg, key_pem, email, sv_cal, report, session, max_calendars)
