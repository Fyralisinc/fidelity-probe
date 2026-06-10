"""Fireflies GraphQL HTTP: a single ``POST /graphql`` with a Bearer token.

Fireflies' real API is GraphQL (docs.fireflies.ai). Every read is a POST to
``{base_url}/graphql`` with ``Authorization: Bearer <api_token>`` and a
``{query, variables}`` JSON body; the response is ``{data, errors}``. The reads
this slice issues:

    user { user_id email name … }            (no id => the API-key owner — the real
                                              "verify my token"; there is NO workspace id)
    transcripts(skip, limit, fromDate, toDate) { … }   (newest-first [Transcript])
    transcript(id: String!) { … }            (single hydrate)

Pagination is ``skip``/``limit`` (limit MAX 50); a short page (< limit) is EOF.
``fromDate``/``toDate`` are ISO-8601 ``DateTime`` strings. Errors surface as
``errors[].extensions.code`` (auth_failed / object_not_found / too_many_requests /
invalid_arguments / …) with the documented per-code HTTP status; 429
(``too_many_requests``) is retried within a bounded budget (the retry hint is a
GraphQL ``extensions.metadata.retryAfter`` timestamp, NOT a ``Retry-After`` header).
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..config import FirefliesConfig
from ..fidelity import FidelityReport

_MAX_RETRY = 4
_BACKOFF = 1.0
_MAX_LIMIT = 50

# A field set covering the Transcript surface a consumer depends on.
_TRANSCRIPT_FIELDS = """
  id
  title
  date
  dateString
  duration
  transcript_url
  audio_url
  video_url
  meeting_link
  host_email
  organizer_email
  participants
  fireflies_users
  calendar_id
  client_reference_id
  meeting_attendees { displayName email phoneNumber name location }
  speakers { id name }
  meeting_info { fred_joined silent_meeting summary_status }
  summary { overview short_summary keywords action_items meeting_type topics_discussed }
  sentences { index speaker_name speaker_id text start_time end_time }
"""


class FirefliesClient:
    def __init__(self, cfg: FirefliesConfig, report: FidelityReport):
        self.base_url = cfg.require_auth()
        self.report = report
        self.session = requests.Session()
        self._headers = {"Authorization": f"Bearer {cfg.api_token}",
                         "Accept": "application/json",
                         "Content-Type": "application/json"}

    def _post(self, query: str, variables: dict | None, label: str):
        """POST a GraphQL query; return (http_status, body, errors_or_None)."""
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.post(f"{self.base_url}/graphql", headers=self._headers,
                                     json=payload, timeout=30)
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
            self.report.diverge("protocol", label, f"non-JSON GraphQL response -> {resp.status_code}")
            return resp.status_code, None, [{"message": "non-JSON"}]
        errors = body.get("errors") if isinstance(body, dict) else None
        return resp.status_code, body, errors

    # ---- queries -----------------------------------------------------------

    def verify_user(self):
        """``user`` (no id) → the API-key owner (Fireflies' real token-verify)."""
        q = "query { user { user_id email name is_admin num_transcripts integrations } }"
        return self._post(q, None, "user")

    def list_transcripts(self, *, limit: int = _MAX_LIMIT, skip: int = 0,
                         from_date: str | None = None, to_date: str | None = None):
        var_decls = ["$limit: Int!", "$skip: Int!"]
        args = ["limit: $limit", "skip: $skip"]
        variables: dict[str, Any] = {"limit": limit, "skip": skip}
        if from_date is not None:
            var_decls.append("$fromDate: DateTime")
            args.append("fromDate: $fromDate")
            variables["fromDate"] = from_date
        if to_date is not None:
            var_decls.append("$toDate: DateTime")
            args.append("toDate: $toDate")
            variables["toDate"] = to_date
        q = (f"query({', '.join(var_decls)}) {{ "
             f"transcripts({', '.join(args)}) {{{_TRANSCRIPT_FIELDS}}} }}")
        return self._post(q, variables, "transcripts")

    def get_transcript(self, transcript_id: str):
        q = ("query($id: String!) { transcript(id: $id) {"
             + _TRANSCRIPT_FIELDS + "} }")
        return self._post(q, {"id": transcript_id}, "transcript")
