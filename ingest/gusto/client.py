"""Gusto HTTP: the OAuth Bearer + the ``/v1`` read endpoints.

Gusto authenticates with an OAuth 2.0 access token sent as ``Authorization:
Bearer <token>`` (operator-mediated install — the operator pastes the token from
their own Gusto OAuth app; there is no client-credentials grant). Each request
also carries the date-based version header ``X-Gusto-API-Version: YYYY-MM-DD``.
Base URL is ``https://api.gusto.com``; the ``/v1`` segment is part of each path.

The ingestion read surface (the REAL contract — pinned from docs.gusto.com's
Embedded Payroll reference + the official SDK):

    POST /oauth/token                                    (refresh, if creds given)
    GET  /v1/companies/{company_uuid}                     (single Company)
    GET  /v1/companies/{company_uuid}/employees           (BARE ARRAY + X-* headers)
    GET  /v1/companies/{company_uuid}/payrolls            (BARE ARRAY + X-* headers)
    GET  /v1/companies/{company_uuid}/payrolls/{uuid}      (single Payroll + comps)

**Pagination returns a BARE JSON ARRAY at the top level** (no body envelope) with
metadata in RESPONSE HEADERS: ``X-Page`` / ``X-Total-Count`` / ``X-Total-Pages`` /
``X-Per-Page`` (``page``/``per`` query params, ``per`` default 25 / max 100); no
``Link`` header. 429s are honoured within a bounded retry budget — Gusto DOES
document ``Retry-After`` (a real contrast with hibob/ramp), so the slice respects it.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..config import GustoConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_API_VERSION = "2024-04-01"


class GustoClient:
    def __init__(self, cfg: GustoConfig, report: FidelityReport):
        cfg.require_auth()
        self.base_url = cfg.base_url
        self.company_uuid = cfg.company_uuid
        self.report = report
        self.session = requests.Session()
        token = cfg.access_token or self._refresh_token(cfg)
        self._headers = {"Authorization": f"Bearer {token}",
                         "X-Gusto-API-Version": _API_VERSION,
                         "Accept": "application/json"}

    def _refresh_token(self, cfg: GustoConfig) -> str:
        """Exchange a refresh_token for a Bearer access token (if no token preset)."""
        resp = self.session.post(
            f"{self.base_url}/oauth/token",
            json={"grant_type": "refresh_token", "refresh_token": cfg.refresh_token,
                  "client_id": cfg.client_id, "client_secret": cfg.client_secret},
            headers={"Accept": "application/json"}, timeout=30)
        if resp.status_code != 200:
            self.report.diverge("auth", "token",
                                f"POST /oauth/token -> {resp.status_code}")
            return "gusto_unminted"
        body = resp.json()
        if body.get("token_type") != "Bearer":
            self.report.record_protocol("token_type is Bearer", False,
                                        f"token_type={body.get('token_type')!r}")
        else:
            self.report.record_protocol("token_type is Bearer", True, "")
        return body.get("access_token") or "gusto_unminted"

    def _get(self, path: str, params: dict | None, label: str):
        url = f"{self.base_url}{path}"
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.get(url, headers=self._headers, params=params, timeout=30)
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                self.report.record_rate_limit(label, 429, float(ra) if ra else None,
                                              honored=ra is not None)
                if attempt < _MAX_RETRY:
                    time.sleep(0.2)
                    continue
            break
        # echo-check the API-version header
        if resp is not None and resp.headers.get("X-Gusto-API-Version") != _API_VERSION:
            self.report.record_protocol("X-Gusto-API-Version echoed", False,
                                        f"got {resp.headers.get('X-Gusto-API-Version')!r}")
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, resp.headers, body

    # ---- endpoints ----------------------------------------------------------

    def company(self):
        return self._get(f"/v1/companies/{self.company_uuid}", None, "company")

    def list_employees(self, *, page: int = 1, per: int = 25, **filters):
        params = {"page": page, "per": per, **filters}
        return self._get(f"/v1/companies/{self.company_uuid}/employees", params, "employees")

    def list_payrolls(self, *, page: int = 1, per: int = 100, **filters):
        params = {"page": page, "per": per, **filters}
        return self._get(f"/v1/companies/{self.company_uuid}/payrolls", params, "payrolls")

    def get_payroll(self, payroll_uuid: str):
        return self._get(f"/v1/companies/{self.company_uuid}/payrolls/{payroll_uuid}",
                         None, "payroll")
