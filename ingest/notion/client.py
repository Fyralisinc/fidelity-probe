"""Notion HTTP: Bearer auth + pinned Notion-Version, with 429/Retry-After backoff.

Notion requires `Authorization: Bearer <integration token>` and a pinned
`Notion-Version` header on every request (https://developers.notion.com/reference).
Returns each response's (status, headers, body) so the slice can validate the body
shape and report exact wire deviations. 429s are recorded and Retry-After honored.
"""
from __future__ import annotations

import time

import requests

from ..config import NotionConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class NotionClient:
    def __init__(self, cfg: NotionConfig, report: FidelityReport):
        self.cfg = cfg
        self.report = report
        self.session = requests.Session()
        self._headers = {
            "Authorization": f"Bearer {cfg.require_token()}",
            "Notion-Version": cfg.version,
        }

    def _request(self, method: str, path: str, label: str,
                 json_body: dict | None = None, params: dict | None = None):
        url = f"{self.cfg.api_base}{path}"
        headers = dict(self._headers)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.request(method, url, headers=headers, json=json_body,
                                        params=params, timeout=30)
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
