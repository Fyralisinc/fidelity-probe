"""Mercury HTTP: an API-token Bearer + the accounts/transactions endpoints.

Mercury authenticates with the org API token, sent as ``Authorization: Bearer
<token>`` (the token carries a literal ``secret-token:`` prefix; Basic auth with
the token as the username is also accepted, but Bearer is the simplest). Base URL
is ``https://api.mercury.com/api/v1``. The ingestion read surface is:

    GET /accounts                                  (UUID-cursor page; {accounts, page})
    GET /account/{id}                              (single account, bare object)
    GET /account/{id}/transactions                 (offset page; {total, transactions})
    GET /account/{id}/transaction/{id}             (single txn, bare — fetch-on-notify)

Transactions default to the **last 30 days** unless ``start`` is given, so a full
backfill passes an explicit wide ``start``. 429s are honored within a bounded
retry budget (Mercury documents no rate-limit contract, so a missing Retry-After
is tolerated).
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..config import MercuryConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class MercuryClient:
    def __init__(self, cfg: MercuryConfig, report: FidelityReport):
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

    def list_accounts(self, *, limit: int = 500, start_after: str | None = None,
                      order: str | None = None):
        params: dict[str, Any] = {"limit": limit}
        if start_after is not None:
            params["start_after"] = start_after
        if order is not None:
            params["order"] = order
        return self._get("/accounts", params, "accounts")

    def get_account(self, account_id: str):
        return self._get(f"/account/{account_id}", {}, "account")

    def list_transactions(self, account_id: str, *, limit: int = 500, offset: int = 0,
                          order: str | None = None, start: str | None = None,
                          end: str | None = None, status: str | None = None):
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if order is not None:
            params["order"] = order
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        if status is not None:
            params["status"] = status
        return self._get(f"/account/{account_id}/transactions", params,
                         f"transactions.{account_id[:8]}")

    def get_transaction(self, account_id: str, transaction_id: str):
        return self._get(f"/account/{account_id}/transaction/{transaction_id}", {},
                         f"transaction.{account_id[:8]}")
