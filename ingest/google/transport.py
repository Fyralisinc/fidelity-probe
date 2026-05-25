"""Shared HTTP for the Google REST reads: bearer auth + 429/Retry-After backoff.

Returns each response's (status, headers, parsed-or-text body) so slices can validate
the body against the official discovery schema and report exact wire deviations
(URL/method/status/headers/shape). Rate-limit responses (429, or 403 with a
rate-limit reason) are recorded and Retry-After is honored.
"""
from __future__ import annotations

import time

import requests

from ..fidelity import FidelityReport

_MAX_RETRY = 4
_MAX_SLEEP = 5.0  # keep audit runs bounded


def _retry_after(resp: requests.Response) -> float | None:
    ra = resp.headers.get("Retry-After")
    if ra is None:
        return None
    try:
        return float(ra)
    except (TypeError, ValueError):
        return None


def _is_rate_limit(resp: requests.Response) -> bool:
    if resp.status_code == 429:
        return True
    if resp.status_code == 403:
        try:
            reasons = {e.get("reason") for e in resp.json().get("error", {}).get("errors", [])}
        except (ValueError, AttributeError):
            reasons = set()
        return bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded"})
    return False


def _body(resp: requests.Response):
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" in ctype:
        try:
            return resp.json()
        except ValueError:
            return resp.text
    return resp.text


def get(session: requests.Session, url: str, token: str, label: str,
        report: FidelityReport, params: dict | None = None):
    """GET with bearer auth, honoring 429/Retry-After. Returns (status, headers, body)."""
    for attempt in range(_MAX_RETRY + 1):
        resp = session.get(url, headers={"Authorization": f"Bearer {token}"},
                           params=params, timeout=30)
        if _is_rate_limit(resp):
            ra = _retry_after(resp)
            report.record_rate_limit(label, resp.status_code, ra, honored=ra is not None)
            if attempt < _MAX_RETRY and ra is not None:
                time.sleep(min(ra, _MAX_SLEEP))
                continue
        return resp.status_code, resp.headers, _body(resp)
    return resp.status_code, resp.headers, _body(resp)
