"""Admin Directory API — enumerate the Workspace's users (mailbox owners).

GET /admin/directory/v1/users?customer=<id> with a directory-scoped token minted for
an admin subject (domain-wide delegation). Paginated by `pageToken`; validated against
the discovery `Users` schema. Returns the list of primaryEmail addresses to ingest.
"""
from __future__ import annotations

import requests

from ..config import DIRECTORY_SCOPE, GoogleConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from . import auth, transport

_PAGE = 200


def list_users(cfg: GoogleConfig, key_pem: str, sv: SpecValidator,
               report: FidelityReport, session: requests.Session) -> list[dict]:
    sa, customer, domain = cfg.require_identity()
    token = auth.fetch_token(cfg, key_pem, cfg.directory_token_url,
                             cfg.admin_subject, DIRECTORY_SCOPE, report, session=session)
    report.auth.update({
        "method": "service account + domain-wide delegation (JWT-bearer)",
        "service_account": sa, "customer_id": customer, "domain": domain,
        "admin_subject": cfg.admin_subject,
    })
    users: list[dict] = []
    page_token: str | None = None
    url = f"{cfg.directory_base}/users"
    while True:
        params = {"customer": customer, "maxResults": _PAGE}
        if page_token:
            params["pageToken"] = page_token
        status, headers, body = transport.get(session, url, token, "directory.users", report, params)
        report.record_page("directory.users", page_token)
        if status != 200:
            report.diverge("protocol", "directory.users",
                           f"GET {url} -> {status}; body={str(body)[:200]}")
            break
        # Google discovery docs key schemas by name (no OpenAPI paths); validate the
        # body against the method's documented response schema component.
        sv.validate_against_component(body, "Users", report)
        users.extend(body.get("users", []) if isinstance(body, dict) else [])
        page_token = body.get("nextPageToken") if isinstance(body, dict) else None
        if not page_token:
            break
    report.count("user", len(users))
    return users
