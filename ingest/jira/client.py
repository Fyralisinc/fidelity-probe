"""Jira Cloud HTTP: HTTP Basic auth + 429/Retry-After backoff.

Atlassian Cloud authenticates with HTTP Basic where the username is the account email
and the password is an API token: `Authorization: Basic base64(email:api_token)`
(https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/). The
site base URL is per-install (https://<site>.atlassian.net) — there is no global host.
Returns each response's (status, headers, body) so the slice can report exact wire
deviations; 429s are recorded and Retry-After honored.
"""
from __future__ import annotations

import base64
import time

import requests

from ..config import JiraConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class JiraClient:
    def __init__(self, cfg: JiraConfig, report: FidelityReport):
        base, email, token = cfg.require_auth()
        self.base_url = base
        self.report = report
        self.session = requests.Session()
        creds = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._headers = {"Authorization": f"Basic {creds}", "Accept": "application/json"}

    def _request(self, method: str, path: str, label: str,
                 params: dict | None = None, json_body: dict | None = None):
        url = f"{self.base_url}{path}"
        headers = dict(self._headers)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.request(method, url, headers=headers, params=params,
                                        json=json_body, timeout=30)
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                try:
                    ra_f = float(ra) if ra is not None else None
                except (TypeError, ValueError):
                    ra_f = None
                self.report.record_rate_limit(label, 429, ra_f, honored=ra_f is not None)
                if attempt < _MAX_RETRY and ra_f is not None:
                    time.sleep(min(ra_f, _MAX_SLEEP))
                    continue
            break
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, resp.headers, body

    def get(self, path: str, label: str, params: dict | None = None):
        return self._request("GET", path, label, params=params)

    def post(self, path: str, label: str, json_body: dict):
        return self._request("POST", path, label, json_body=json_body)
