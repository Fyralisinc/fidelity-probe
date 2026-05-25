"""Gmail historical ingestion — enumerate mailboxes → list messages → fetch full.

Per mailbox (a per-user bearer minted via domain-wide delegation, userId = the user's
email): GET the profile, then page the message list (users.messages.list, paginated by
pageToken) and fetch each message in full (users.messages.get?format=full). Every
response is validated against the official Gmail *discovery* schema (Profile,
ListMessagesResponse, Message). To keep an audit run bounded we cap mailboxes and
full-message fetches per mailbox (still genuinely exercising list pagination + full
fetch); raise the caps via the CLI for a deeper backfill.
"""
from __future__ import annotations

import requests

from ..config import GMAIL_SCOPE, GoogleConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from . import auth, directory, transport

_LIST_PAGE = 25        # small enough to exercise list pagination against the cap
_FULL_CAP = 50         # full-message fetches per mailbox (default audit bound)


def _ingest_mailbox(cfg: GoogleConfig, key_pem: str, user: str, sv: SpecValidator,
                    report: FidelityReport, session: requests.Session,
                    full_cap: int) -> None:
    token = auth.fetch_token(cfg, key_pem, cfg.gmail_token_url, user, GMAIL_SCOPE,
                             report, session=session)
    base = f"{cfg.gmail_base}/users/{user}"

    status, _, prof = transport.get(session, f"{base}/profile", token, "gmail.profile", report)
    if status == 200:
        sv.validate_against_component(prof, "Profile", report)

    fetched = 0
    page_token: str | None = None
    while fetched < full_cap:
        params = {"maxResults": _LIST_PAGE}
        if page_token:
            params["pageToken"] = page_token
        status, _, body = transport.get(session, f"{base}/messages", token,
                                        "gmail.messages.list", report, params)
        report.record_page("gmail.messages.list", page_token)
        if status != 200:
            report.diverge("protocol", "gmail.messages.list",
                           f"GET {base}/messages -> {status}; body={str(body)[:160]}")
            break
        sv.validate_against_component(body, "ListMessagesResponse", report)
        for stub in (body.get("messages") or []):
            if fetched >= full_cap:
                break
            st2, _, full = transport.get(session, f"{base}/messages/{stub['id']}", token,
                                         "gmail.messages.get", report, {"format": "full"})
            if st2 == 200:
                sv.validate_against_component(full, "Message", report)
                report.count("message")
                fetched += 1
            else:
                report.diverge("protocol", "gmail.messages.get",
                               f"GET message {stub['id']} -> {st2}")
        page_token = body.get("nextPageToken")
        if not page_token:
            break


def run_historical(cfg: GoogleConfig, report: FidelityReport,
                   max_users: int | None = None,
                   full_cap: int = _FULL_CAP) -> None:
    sv_dir = SpecValidator("admin_directory")
    sv_gmail = SpecValidator("gmail")
    key_pem = auth.resolve_key(cfg, report)
    session = requests.Session()
    users = directory.list_users(cfg, key_pem, sv_dir, report, session)
    emails = [u["primaryEmail"] for u in users if u.get("primaryEmail")]
    if max_users is not None:
        emails = emails[:max_users]
    for email in emails:
        _ingest_mailbox(cfg, key_pem, email, sv_gmail, report, session, full_cap)
    report.count("mailbox", len(emails))
