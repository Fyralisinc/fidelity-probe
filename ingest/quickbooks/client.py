"""QuickBooks Online HTTP: OAuth Bearer + the SQL `query` endpoint.

QBO authenticates with `Authorization: Bearer <access_token>` and reads entities
through a single SQL-like endpoint:

    GET /v3/company/{realmId}/query?query=<SQL>&minorversion=<N>

with `Accept: application/json` to force JSON (the API can emit XML otherwise).
Pagination is `STARTPOSITION n MAXRESULTS m` inside the query string (1-based).
429 ThrottleExceeded is honored within a bounded retry budget.
"""
from __future__ import annotations

import time
from urllib.parse import quote

import requests

from ..config import QuickBooksConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class QuickBooksClient:
    def __init__(self, cfg: QuickBooksConfig, report: FidelityReport):
        realm, token = cfg.require_auth()
        self.base_url = cfg.base_url
        self.realm = realm
        self.minorversion = cfg.minorversion
        self.report = report
        self.session = requests.Session()
        self._headers = {"Authorization": f"Bearer {token}",
                         "Accept": "application/json"}

    def query(self, sql: str, label: str):
        url = f"{self.base_url}/v3/company/{self.realm}/query"
        params = {"query": sql, "minorversion": self.minorversion}
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.get(url, headers=self._headers, params=params, timeout=30)
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                try:
                    ra_f = float(ra) if ra is not None else None
                except (TypeError, ValueError):
                    ra_f = None
                self.report.record_rate_limit(label, 429, ra_f, honored=ra_f is not None)
                if attempt < _MAX_RETRY:
                    time.sleep(min(ra_f or 1.0, _MAX_SLEEP))
                    continue
            break
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, resp.headers, body
