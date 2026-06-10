"""Carta HTTP: an OAuth client-credentials token + the ``/v1alpha1`` issuer suite.

Carta authenticates with an OAuth 2.0 access token sent as ``Authorization: Bearer
<token>``. The token is minted by the client-credentials grant against Carta's IdP
path (here served on the same mock host):

    POST /o/access_token/   (HTTP Basic base64(client_id:client_secret) + body
                             {grant_type:"CLIENT_CREDENTIALS", scope:"…"})
    -> {access_token, token_type:"Bearer", expires_in, scope}   (NO refresh_token)

(If a pre-minted CARTA_ACCESS_TOKEN is supplied the exchange is skipped.) Base URL
is ``https://api.carta.com``; the ``/v1alpha1`` segment is part of each path. The
cap-table read surface (the REAL contract — pinned from docs.carta.com):

    GET /v1alpha1/issuers                          ({issuers:[…]})
    GET /v1alpha1/issuers/{id}                     ({issuer:{…}} — singular wrap)
    GET /v1alpha1/issuers/{id}/stakeholders        (AIP page {stakeholders, nextPageToken})
    GET /v1alpha1/issuers/{id}/shareClasses        (AIP page {shareClasses, nextPageToken})
    GET /v1alpha1/issuers/{id}/optionGrants        (AIP page {optionGrants, nextPageToken})
    GET /v1alpha1/issuers/{id}/convertibleNotes    (AIP page {convertibleNotes, nextPageToken})

Pagination is Google AIP-158 token style: ``pageSize`` + opaque ``pageToken``; the
response carries ``nextPageToken`` until the last page (then ABSENT). Carta rate-
limits with 429 + ``RateLimit-*`` / ``X-RateLimit-*-Second``/``-Minute`` headers and
**NO Retry-After** — the client backs off on ``RateLimit-Reset`` (seconds).
"""
from __future__ import annotations

import base64
import time
from typing import Any

import requests

from ..config import CartaConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class CartaClient:
    def __init__(self, cfg: CartaConfig, report: FidelityReport):
        base, issuer_id = cfg.require_auth()
        self.base_url = base
        self.issuer_id = issuer_id
        self.report = report
        self.session = requests.Session()
        token = cfg.access_token or self._mint_token(cfg)
        self._headers = {"Authorization": f"Bearer {token}",
                         "Accept": "application/json"}

    def _mint_token(self, cfg: CartaConfig) -> str:
        """Exchange client-credentials for a Bearer access token (no refresh_token)."""
        basic = base64.b64encode(
            f"{cfg.client_id}:{cfg.client_secret}".encode()).decode()
        resp = self.session.post(
            f"{self.base_url}/o/access_token/",
            headers={"Authorization": f"Basic {basic}",
                     "Accept": "application/json"},
            json={"grant_type": "CLIENT_CREDENTIALS",
                  "scope": ("read_issuer_info read_issuer_stakeholders "
                            "read_issuer_shareclasses read_issuer_securities")},
            timeout=30)
        if resp.status_code != 200:
            self.report.diverge("auth", "token",
                                f"POST /o/access_token/ -> {resp.status_code}")
            return "carta_unminted"
        body = resp.json()
        if body.get("token_type") != "Bearer":
            self.report.record_protocol("token_type is Bearer", False,
                                        f"token_type={body.get('token_type')!r}")
        else:
            self.report.record_protocol("token_type is Bearer", True, "")
        # client-credentials issues NO refresh_token (you re-mint).
        if "refresh_token" in body:
            self.report.record_protocol(
                "client-credentials token has NO refresh_token", False,
                "refresh_token present on a client-credentials response")
        else:
            self.report.record_protocol(
                "client-credentials token has NO refresh_token", True, "")
        return body.get("access_token") or "carta_unminted"

    def _get(self, url: str, params: dict | None, label: str):
        if url.startswith("/"):
            url = f"{self.base_url}{url}"
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.get(url, headers=self._headers, params=params, timeout=30)
            if resp.status_code == 429:
                # Carta is rate-limited with RateLimit-Reset (seconds), NOT Retry-After.
                reset = resp.headers.get("RateLimit-Reset")
                honored = reset is not None
                self.report.record_rate_limit(label, 429, reset, honored=honored)
                if attempt < _MAX_RETRY and honored:
                    try:
                        sleep_s = min(float(reset), _MAX_SLEEP)
                    except (TypeError, ValueError):
                        sleep_s = 1.0
                    time.sleep(max(0.0, sleep_s) or 1.0)
                    continue
            break
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, resp.headers, body

    # ---- issuer + cap-table collections ------------------------------------

    def list_issuers(self):
        return self._get("/v1alpha1/issuers", None, "issuers")

    def get_issuer(self, issuer_id: str | None = None):
        iid = issuer_id or self.issuer_id
        return self._get(f"/v1alpha1/issuers/{iid}", None, "issuer")

    def list_collection(self, collection: str, *, page_size: int | None = None,
                        page_token: str | None = None, **filters):
        params: dict[str, Any] = dict(filters)
        if page_size is not None:
            params["pageSize"] = page_size
        if page_token is not None:
            params["pageToken"] = page_token
        return self._get(f"/v1alpha1/issuers/{self.issuer_id}/{collection}",
                         params or None, collection)
