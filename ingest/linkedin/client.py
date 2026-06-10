"""LinkedIn HTTP: the versioned ``/rest/`` Community-Management read surface.

LinkedIn authenticates with an OAuth 2.0 access token sent as ``Authorization:
Bearer <token>``. Every versioned ``/rest/`` call additionally requires two protocol
headers (pinned from learn.microsoft.com/linkedin):

    Linkedin-Version: YYYYMM            (required; latest is not applied by default)
    X-Restli-Protocol-Version: 2.0.0    (Rest.li 2.0 param encoding)

Base URL is ``https://api.linkedin.com``; the ``/rest`` segment is part of each path.
The organization read surface (the REAL contract):

    GET /rest/organizations/{id}                          (org lookup / connectivity)
    GET /rest/posts?q=author&author={orgURN}              (OFFSET start/count finder)
    GET /rest/organizationalEntityShareStatistics?q=organizationalEntity&organizationalEntity={orgURN}
    GET /rest/organizationalEntityFollowerStatistics?q=organizationalEntity&organizationalEntity={orgURN}

Collections are Rest.li FINDER envelopes ``{elements:[…], paging:{start,count,
links:[…]}}``. The posts finder pages by OFFSET (``start``/``count``, default 10 /
max 100); the EOF signal is a page with FEWER elements than ``count``. The two stats
finders return a single lifetime ``elements`` row (no pagination). LinkedIn
rate-limits with a bare 429 (classic ``{message,serviceErrorCode,status}`` body) and
documents **NO Retry-After / NO X-RateLimit-*** headers — so the client backs off on
a fixed budget rather than a server-advertised delay.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..config import LinkedinConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 3
_BACKOFF = 1.0


class LinkedinClient:
    def __init__(self, cfg: LinkedinConfig, report: FidelityReport):
        base, org_urn = cfg.require_auth()
        self.base_url = base
        self.organization_urn = org_urn
        self.report = report
        self.session = requests.Session()
        self._headers = {
            "Authorization": f"Bearer {cfg.access_token}",
            "Linkedin-Version": cfg.version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict | None, label: str):
        url = f"{self.base_url}{path}" if path.startswith("/") else path
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.get(url, headers=self._headers, params=params, timeout=30)
            if resp.status_code == 429:
                # LinkedIn documents NO Retry-After — back off on a fixed budget.
                retry_after = resp.headers.get("Retry-After")
                self.report.record_rate_limit(label, 429, retry_after,
                                              honored=retry_after is None)
                if attempt < _MAX_RETRY:
                    time.sleep(_BACKOFF)
                    continue
            break
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, resp.headers, body

    # ---- the read surface ---------------------------------------------------

    def get_organization(self, org_id: str):
        return self._get(f"/rest/organizations/{org_id}", None, "organization")

    def list_posts(self, *, start: int = 0, count: int = 10,
                   sort_by: str = "LAST_MODIFIED"):
        return self._get("/rest/posts",
                         {"q": "author", "author": self.organization_urn,
                          "start": start, "count": count, "sortBy": sort_by},
                         "posts")

    def share_statistics(self):
        return self._get("/rest/organizationalEntityShareStatistics",
                         {"q": "organizationalEntity",
                          "organizationalEntity": self.organization_urn},
                         "shareStatistics")

    def follower_statistics(self):
        return self._get("/rest/organizationalEntityFollowerStatistics",
                         {"q": "organizationalEntity",
                          "organizationalEntity": self.organization_urn},
                         "followerStatistics")
