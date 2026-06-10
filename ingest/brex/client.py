"""Brex HTTP: a Bearer token + the ``/v2/`` accounts/transactions endpoints.

Brex authenticates with a user/OAuth token sent as ``Authorization: Bearer
<token>`` (user tokens carry a ``bxt_`` prefix). Base URL is
``https://api.brex.com``; the ``/v2`` segment is part of each path. The ingestion
read surface (the REAL contract — pinned from developer.brex.com's OpenAPI):

    GET /v2/accounts/cash                  (CURSOR page {next_cursor, items})
    GET /v2/accounts/cash/{id}             (single CashAccount, bare object)
    GET /v2/accounts/cash/primary          (single primary CashAccount)
    GET /v2/accounts/card                  (BARE ARRAY of CardAccount, no pagination)
    GET /v2/transactions/cash/{id}         (CURSOR page; posted_at_start filter)
    GET /v2/transactions/card/primary      (CURSOR page; posted_at_start filter)

Pagination is an OPAQUE ``cursor`` (+ ``limit``, default 100/max 1000); the
response carries ``next_cursor`` (null/absent == last page). 429s are honoured
within a bounded retry budget (Brex documents 429 but no Retry-After guarantee,
so a missing one is tolerated).
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..config import BrexConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class BrexClient:
    def __init__(self, cfg: BrexConfig, report: FidelityReport):
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

    # ---- accounts ----------------------------------------------------------

    def list_cash_accounts(self, *, cursor: str | None = None, limit: int = 100):
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return self._get("/v2/accounts/cash", params, "accounts.cash")

    def get_cash_account(self, account_id: str):
        return self._get(f"/v2/accounts/cash/{account_id}", {}, "account.cash")

    def get_primary_cash_account(self):
        return self._get("/v2/accounts/cash/primary", {}, "account.cash.primary")

    def list_card_accounts(self):
        return self._get("/v2/accounts/card", {}, "accounts.card")

    # ---- transactions ------------------------------------------------------

    def list_cash_transactions(self, account_id: str, *, cursor: str | None = None,
                               limit: int = 100, posted_at_start: str | None = None):
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if posted_at_start is not None:
            params["posted_at_start"] = posted_at_start
        return self._get(f"/v2/transactions/cash/{account_id}", params,
                         f"transactions.cash.{account_id[:8]}")

    def list_primary_card_transactions(self, *, cursor: str | None = None,
                                       limit: int = 100, posted_at_start: str | None = None):
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if posted_at_start is not None:
            params["posted_at_start"] = posted_at_start
        return self._get("/v2/transactions/card/primary", params, "transactions.card.primary")
