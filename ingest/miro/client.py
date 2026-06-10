"""Miro HTTP: an org-app Bearer token + the ``/v2/`` read endpoints.

Miro authenticates a single long-lived org-level app access token via
``Authorization: Bearer <token>`` (scope ``boards:read``). Base URL is
``https://api.miro.com/v2`` (the ``/v2`` segment is part of the base). The
whiteboard ingestion read surface (the REAL contract — pinned from Miro's
published OpenAPI spec, the source Miro generates all its SDK clients from):

    GET /v2/boards                  (OFFSET pagination — limit 1-50/def 20, offset;
                                     {data,total,size,offset,limit,links,type})
    GET /v2/boards/{board_id}       (single board — + links{self,related})
    GET /v2/boards/{board_id}/items (CURSOR pagination — limit 10-50/def 10, opaque
                                     cursor; {data,total,size,cursor,limit,links};
                                     `cursor` ABSENT on the last page)

These are TWO DIFFERENT paginators (boards = offset, items = cursor) — a Brex-clone
that treats both as one paginator is wrong against real Miro. Miro rate-limits with
CREDIT-based HTTP 429 + ``X-RateLimit-Limit/Remaining/Reset`` headers and **NO
``Retry-After``** — the client backs off on ``X-RateLimit-Reset`` (an epoch second),
not Retry-After.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..config import MiroConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class MiroClient:
    def __init__(self, cfg: MiroConfig, report: FidelityReport):
        base, org_id, token = cfg.require_auth()
        self.base_url = base
        self.org_id = org_id
        self.report = report
        self.session = requests.Session()
        self._headers = {"Authorization": f"Bearer {token}",
                         "Accept": "application/json"}

    def _request(self, method: str, url: str, label: str, *, params: dict | None = None):
        if url.startswith("/"):
            url = f"{self.base_url}{url}"
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.request(method, url, headers=self._headers,
                                        params=params, timeout=30)
            if resp.status_code == 429:
                # Miro is credit-based: the documented backoff signal is
                # X-RateLimit-Reset (epoch seconds), NOT Retry-After.
                reset = resp.headers.get("X-RateLimit-Reset")
                honored = reset is not None
                self.report.record_rate_limit(label, 429, reset, honored=honored)
                if attempt < _MAX_RETRY and honored:
                    try:
                        sleep_s = max(0.0, float(reset) - time.time())
                    except (TypeError, ValueError):
                        sleep_s = 1.0
                    time.sleep(min(sleep_s or 1.0, _MAX_SLEEP))
                    continue
            break
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, resp.headers, body

    # ---- boards (offset) ---------------------------------------------------

    def list_boards(self, *, limit: int | None = None, offset: int | None = None):
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self._request("GET", "/boards", "boards", params=params or None)

    def get_board(self, board_id: str):
        return self._request("GET", f"/boards/{board_id}", "board")

    # ---- items (cursor) ----------------------------------------------------

    def list_items(self, board_id: str, *, limit: int | None = None,
                   cursor: str | None = None, item_type: str | None = None):
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        if item_type is not None:
            params["type"] = item_type
        return self._request("GET", f"/boards/{board_id}/items", "items",
                             params=params or None)

    def follow(self, url: str, label: str):
        """Follow a full ``links.next`` URL verbatim (it carries limit + cursor/offset)."""
        return self._request("GET", url, label)
