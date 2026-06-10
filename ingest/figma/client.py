"""Figma HTTP: an ``X-Figma-Token`` access token + the ``/v1/`` read endpoints.

Figma authenticates a personal/plan access token via the ``X-Figma-Token`` header
(OAuth ``Authorization: Bearer`` is also accepted on read endpoints). Base URL is
``https://api.figma.com``. The design ingestion read surface (the REAL contract —
pinned from developers.figma.com + the official OpenAPI spec figma/rest-api-spec):

    GET /v1/me                          (auth probe — the only User carrying email)
    GET /v1/teams/{team_id}/projects    ({name, projects:[{id,name}]})
    GET /v1/projects/{project_id}/files ({name, files:[{key,name,thumbnail_url,last_modified}]})
    GET /v1/files/{key}/versions        ({versions:[…], pagination:{prev_page,next_page}};
                                         CURSOR page_size(def30/max50)+before/after)
    GET /v1/files/{key}/comments        ({comments:[…]} — NO pagination)

There is NO ``GET /v1/files`` list and NO ``/v1/files/{key}/events`` stream — a real
backfill enumerates files then MERGES ``/versions`` + ``/comments`` into one event
stream. Figma rate-limits with HTTP 429 + a ``Retry-After`` (seconds) header, which
the client honours within a bounded retry budget.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

import requests

from ..config import FigmaConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0


class FigmaClient:
    def __init__(self, cfg: FigmaConfig, report: FidelityReport):
        base, team_id, token = cfg.require_auth()
        self.base_url = base
        self.team_id = team_id
        self.report = report
        self.session = requests.Session()
        # X-Figma-Token is the personal/plan access-token header.
        self._headers = {"X-Figma-Token": token, "Accept": "application/json"}

    def _request(self, method: str, url: str, label: str, *, params: dict | None = None):
        if url.startswith("/"):
            url = f"{self.base_url}{url}"
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.request(method, url, headers=self._headers,
                                        params=params, timeout=30)
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                honored = ra is not None
                self.report.record_rate_limit(label, 429, ra, honored=honored)
                if attempt < _MAX_RETRY:
                    try:
                        sleep_s = min(float(ra), _MAX_SLEEP) if ra else 1.0
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

    # ---- auth probe --------------------------------------------------------

    def get_me(self):
        return self._request("GET", "/v1/me", "me")

    # ---- enumeration -------------------------------------------------------

    def team_projects(self, team_id: str | None = None):
        tid = team_id or self.team_id
        return self._request("GET", f"/v1/teams/{tid}/projects", "projects")

    def project_files(self, project_id: str):
        return self._request("GET", f"/v1/projects/{project_id}/files", "files")

    # ---- file metadata -----------------------------------------------------

    def file_meta(self, file_key: str):
        return self._request("GET", f"/v1/files/{file_key}/meta", "meta")

    # ---- versions (cursor) -------------------------------------------------

    def file_versions(self, file_key: str, *, page_size: int | None = None,
                      before: int | None = None, after: int | None = None):
        params: dict[str, Any] = {}
        if page_size is not None:
            params["page_size"] = page_size
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        return self._request("GET", f"/v1/files/{file_key}/versions", "versions",
                             params=params or None)

    def follow(self, url: str, label: str):
        """Follow a full ``pagination.next_page``/``prev_page`` URL verbatim."""
        return self._request("GET", url, label)

    # ---- comments (no pagination) -----------------------------------------

    def file_comments(self, file_key: str):
        return self._request("GET", f"/v1/files/{file_key}/comments", "comments")
