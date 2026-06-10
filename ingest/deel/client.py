"""Deel HTTP: a Bearer token + the ``/rest/v2/`` contracts/invoices endpoints.

Deel authenticates with a long-lived org/personal API token sent as
``Authorization: Bearer <token>``. Base URL is ``https://api.letsdeel.com/rest/v2``
(the ``/rest/v2`` segment is part of the base). The ingestion read surface (the
REAL contract — pinned from developer.deel.com):

    GET /contracts              ({data, page:{cursor, total_rows}}; CURSOR-only via after_cursor)
    GET /contracts/{id}         (single Contract, wrapped {data:{…}})
    GET /invoices               ({data, page:{offset, total_rows, items_per_page, cursor}};
                                 HYBRID limit+offset+cursor; status=all to see every status,
                                 issued_from_date/issued_to_date date window)

Deel sends ``X-Version: <YYYY-MM-DD>`` (date-based API versioning). 429s are honoured
within a bounded retry budget (Deel documents 429 + Retry-After at ~5 rps).
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..config import DeelConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0
_API_VERSION = "2026-01-01"


class DeelClient:
    def __init__(self, cfg: DeelConfig, report: FidelityReport):
        base, token = cfg.require_auth()
        self.base_url = base
        self.report = report
        self.session = requests.Session()
        self._headers = {"Authorization": f"Bearer {token}",
                         "Accept": "application/json",
                         "X-Version": _API_VERSION}

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

    # ---- contracts ---------------------------------------------------------

    def list_contracts(self, *, after_cursor: str | None = None, limit: int = 50):
        params: dict[str, Any] = {"limit": limit}
        if after_cursor is not None:
            params["after_cursor"] = after_cursor
        return self._get("/contracts", params, "contracts")

    def get_contract(self, contract_id: str):
        return self._get(f"/contracts/{contract_id}", {}, "contract")

    # ---- invoices ----------------------------------------------------------

    def list_invoices(self, *, cursor: str | None = None, offset: int | None = None,
                      limit: int = 100, status: str | None = None,
                      issued_from_date: str | None = None,
                      issued_to_date: str | None = None):
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        elif offset is not None:
            params["offset"] = offset
        if status is not None:
            params["status"] = status
        if issued_from_date is not None:
            params["issued_from_date"] = issued_from_date
        if issued_to_date is not None:
            params["issued_to_date"] = issued_to_date
        return self._get("/invoices", params, "invoices")
