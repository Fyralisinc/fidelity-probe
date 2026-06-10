"""HiBob HTTP: a service-user Basic credential + the ``/v1/`` read endpoints.

HiBob authenticates with a **service user** credential presented as HTTP Basic,
``Authorization: Basic base64("{service_user_id}:{token}")``
(apidocs.hibob.com/reference/authorization). Base URL is ``https://api.hibob.com``.
The HR ingestion read surface (the REAL contract — pinned from apidocs.hibob.com):

    POST /v1/people/search             ({employees:[…]}; returns ALL — NO pagination)
    GET  /v1/timeoff/requests/changes  (BARE ARRAY; since/to date window, ≤6 months)
    GET  /v1/bulk/people/salaries       ({results, response_metadata:{next_cursor}};
                                         CURSOR pagination, limit default 50 / max 200)

HiBob rate-limits per minute (``/v1/people/search`` = 50/min) and signals the
window via ``X-RateLimit-Limit/Remaining/Reset`` (Reset = Unix epoch) — it does
NOT document ``Retry-After``. 429s are honoured within a bounded retry budget.
"""
from __future__ import annotations

import base64
import time
from typing import Any

import requests

from ..config import HibobConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class HibobClient:
    def __init__(self, cfg: HibobConfig, report: FidelityReport):
        base, sid, token = cfg.require_auth()
        self.base_url = base
        self.report = report
        self.session = requests.Session()
        cred = base64.b64encode(f"{sid}:{token}".encode()).decode()
        self._headers = {"Authorization": f"Basic {cred}",
                         "Accept": "application/json"}

    def _request(self, method: str, path: str, label: str, *,
                 params: dict | None = None, json_body: dict | None = None):
        url = f"{self.base_url}{path}"
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.request(method, url, headers=self._headers,
                                        params=params, json=json_body, timeout=30)
            if resp.status_code == 429:
                # HiBob has no Retry-After; the documented backoff signal is the
                # X-RateLimit-Reset epoch. Treat its presence as an honoured signal.
                reset = resp.headers.get("X-RateLimit-Reset")
                honored = reset is not None
                self.report.record_rate_limit(label, 429, None, honored=honored)
                if attempt < _MAX_RETRY:
                    sleep_s = 1.0
                    try:
                        if reset is not None:
                            sleep_s = max(0.0, min(float(reset) - time.time(), _MAX_SLEEP))
                    except (TypeError, ValueError):
                        sleep_s = 1.0
                    time.sleep(min(sleep_s or 1.0, _MAX_SLEEP))
                    continue
            break
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, resp.headers, body

    # ---- people ------------------------------------------------------------

    def people_search(self, *, filters: list | None = None,
                      show_inactive: bool = False, fields: list | None = None):
        body: dict[str, Any] = {"showInactive": show_inactive}
        if filters is not None:
            body["filters"] = filters
        if fields is not None:
            body["fields"] = fields
        return self._request("POST", "/v1/people/search", "people", json_body=body)

    # ---- time-off ----------------------------------------------------------

    def timeoff_changes(self, *, since: str, to: str | None = None,
                        include_pending: bool = False):
        params: dict[str, Any] = {"since": since,
                                  "includePending": str(include_pending).lower()}
        if to is not None:
            params["to"] = to
        return self._request("GET", "/v1/timeoff/requests/changes", "timeoff",
                             params=params)

    # ---- salaries (payroll history) ---------------------------------------

    def bulk_salaries(self, *, cursor: str | None = None, limit: int = 50,
                      employee_ids: str | None = None):
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if employee_ids is not None:
            params["employeeIds"] = employee_ids
        return self._request("GET", "/v1/bulk/people/salaries", "salaries", params=params)
