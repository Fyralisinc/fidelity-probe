"""Ashby HTTP: HTTP-Basic API-key auth + the RPC ``.list``/``.info`` endpoints.

Ashby is an RPC-style API — every read is an HTTP **POST** to ``/<category>.<verb>``
with a JSON body, even for list/read operations. Auth is the org API key presented
as the HTTP Basic **username** with an EMPTY password
(``Authorization: Basic base64("<key>:")``). Base URL is ``https://api.ashbyhq.com``
(no version path; the version rides the ``Accept: application/json; version=1``
header). The ingestion read surface is:

    POST /<category>.list   {cursor?, syncToken?, limit?}  -> {success, results:[…],
                                                               moreDataAvailable,
                                                               nextCursor?, syncToken?}
    POST /<category>.info   {id}                           -> {success, results:{…}}

Business errors come back as HTTP **200** with ``{success:false, errors:[…],
errorInfo:{code,…}}``; only auth (401/403) and rate-limit (429) are HTTP-level.
429s are honoured within a bounded retry budget (Retry-After is UNCONFIRMED in
Ashby's docs, so a missing one is tolerated).
"""
from __future__ import annotations

import base64
import time
from typing import Any

import requests

from ..config import AshbyConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class AshbyClient:
    def __init__(self, cfg: AshbyConfig, report: FidelityReport):
        base, key = cfg.require_auth()
        self.base_url = base
        self.report = report
        self.session = requests.Session()
        token = base64.b64encode(f"{key}:".encode()).decode("ascii")
        self._headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json; version=1",
            "Content-Type": "application/json",
        }

    def _rpc(self, method: str, body: dict, label: str):
        url = f"{self.base_url}/{method}"
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.post(url, headers=self._headers, json=body, timeout=30)
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
            data: Any = resp.json()
        except ValueError:
            data = resp.text
        return resp.status_code, resp.headers, data

    def list_entities(self, category: str, *, cursor: str | None = None,
                      sync_token: str | None = None, limit: int | None = None):
        body: dict[str, Any] = {}
        if cursor is not None:
            body["cursor"] = cursor
        if sync_token is not None:
            body["syncToken"] = sync_token
        if limit is not None:
            body["limit"] = limit
        return self._rpc(f"{category}.list", body, f"{category}.list")

    def get_entity(self, category: str, entity_id: str):
        return self._rpc(f"{category}.info", {"id": entity_id}, f"{category}.info")
