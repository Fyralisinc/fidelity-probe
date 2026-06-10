"""Ramp HTTP: an OAuth client-credentials token + the ``/developer/v1`` endpoints.

Ramp authenticates with an OAuth 2.0 access token sent as ``Authorization: Bearer
<ramp_business_tok_…>``. The token is minted by the client-credentials grant:

    POST /developer/v1/token   (HTTP Basic base64(client_id:client_secret) + body
                                {grant_type:"client_credentials", scope:"…"})
    -> {access_token, token_type:"Bearer", expires_in, scope}

(If a pre-minted RAMP_ACCESS_TOKEN is supplied the exchange is skipped.) Base URL
is ``https://api.ramp.com``; the ``/developer/v1`` segment is part of each path.
The ingestion read surface (the REAL contract — pinned from docs.ramp.com OpenAPI):

    GET /developer/v1/transactions        ({data, page:{next}} KEYSET page)
    GET /developer/v1/transactions/{id}   (single Transaction, BARE object)
    GET /developer/v1/reimbursements      ({data, page:{next}} KEYSET page)
    GET /developer/v1/cards               ({data, page:{next}} KEYSET page)
    GET /developer/v1/users               ({data, page:{next}} KEYSET page)

Pagination is KEYSET: ``page.next`` is a FULL URL embedding ``start=<last entity
id>`` — the client just GETs it. ``page_size`` default 20 / max 100. 429s are
honoured within a bounded retry budget; Ramp documents NO Retry-After header
(only client-side backoff), so a fixed backoff is used.
"""
from __future__ import annotations

import base64
import time
from typing import Any

import requests

from ..config import RampConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_BACKOFF = 1.0


class RampClient:
    def __init__(self, cfg: RampConfig, report: FidelityReport):
        base = cfg.require_auth()
        self.base_url = base
        self.report = report
        self.session = requests.Session()
        token = cfg.access_token or self._mint_token(cfg)
        self._headers = {"Authorization": f"Bearer {token}",
                         "Accept": "application/json"}

    def _mint_token(self, cfg: RampConfig) -> str:
        """Exchange client-credentials for a Bearer access token."""
        basic = base64.b64encode(
            f"{cfg.client_id}:{cfg.client_secret}".encode()).decode()
        resp = self.session.post(
            f"{self.base_url}/developer/v1/token",
            headers={"Authorization": f"Basic {basic}",
                     "Accept": "application/json"},
            json={"grant_type": "client_credentials",
                  "scope": "transactions:read reimbursements:read cards:read users:read"},
            timeout=30)
        if resp.status_code != 200:
            self.report.diverge("auth", "token",
                                f"POST /developer/v1/token -> {resp.status_code}")
            return "ramp_business_tok_unminted"
        body = resp.json()
        if body.get("token_type") != "Bearer":
            self.report.record_protocol("token_type is Bearer", False,
                                        f"token_type={body.get('token_type')!r}")
        else:
            self.report.record_protocol("token_type is Bearer", True, "")
        return body.get("access_token") or "ramp_business_tok_unminted"

    def _get(self, url: str, params: dict | None, label: str):
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.get(url, headers=self._headers, params=params, timeout=30)
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                self.report.record_rate_limit(label, 429, None, honored=ra is not None)
                if attempt < _MAX_RETRY:
                    time.sleep(_BACKOFF)
                    continue
            break
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, resp.headers, body

    # ---- list endpoints (keyset) -------------------------------------------

    def list_transactions(self, *, page_size: int = 100, **filters):
        params: dict[str, Any] = {"page_size": page_size, **filters}
        return self._get(f"{self.base_url}/developer/v1/transactions", params,
                         "transactions")

    def list_reimbursements(self, *, page_size: int = 100, **filters):
        params: dict[str, Any] = {"page_size": page_size, **filters}
        return self._get(f"{self.base_url}/developer/v1/reimbursements", params,
                         "reimbursements")

    def list_cards(self, *, page_size: int = 100):
        return self._get(f"{self.base_url}/developer/v1/cards",
                         {"page_size": page_size}, "cards")

    def list_users(self, *, page_size: int = 100):
        return self._get(f"{self.base_url}/developer/v1/users",
                         {"page_size": page_size}, "users")

    def follow(self, url: str, label: str):
        """GET a full ``page.next`` URL verbatim (keyset continuation)."""
        return self._get(url, None, label)

    def get_transaction(self, transaction_id: str):
        return self._get(f"{self.base_url}/developer/v1/transactions/{transaction_id}",
                         None, "transaction")
