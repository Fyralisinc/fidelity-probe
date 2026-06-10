"""Grafana HTTP: a service-account Bearer token + the annotations endpoint.

Grafana authenticates with ``Authorization: Bearer <token>`` (an org-scoped
service-account token, prefix ``glsa_``) and ``Accept: application/json``. The
ingestion read surface is:

    GET /api/annotations?from=&to=&limit=   (epoch-ms window, newest-first, bare array)
    GET /api/org                            (connectivity + credential probe)

Pagination has no cursor/Link — it is a backward time-window walk. 429s are
honored within a bounded retry budget (Grafana Cloud's gateway emits Retry-After;
the core API does not document it, so a missing Retry-After is tolerated).
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..config import GrafanaConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class GrafanaClient:
    def __init__(self, cfg: GrafanaConfig, report: FidelityReport):
        base, token = cfg.require_auth()
        self.base_url = base
        self.report = report
        self.session = requests.Session()
        self._headers = {"Authorization": f"Bearer {token}",
                         "Accept": "application/json"}

    def _get(self, path: str, params: dict, label: str):
        url = f"{self.base_url}{path}"
        resp = None
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
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, resp.headers, body

    def list_annotations(self, *, frm: int | None = None, to: int | None = None,
                         limit: int = 100, atype: str | None = None, label: str = "annotations"):
        params: dict[str, Any] = {"limit": limit}
        if frm is not None:
            params["from"] = frm
        if to is not None:
            params["to"] = to
        if atype is not None:
            params["type"] = atype
        return self._get("/api/annotations", params, label)

    def get_org(self):
        return self._get("/api/org", {}, "org")
