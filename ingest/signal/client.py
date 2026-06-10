"""A minimal signal-cli-shaped linked-device client over the method-contract shim.

Signal has NO official server API; the only sound integration is signal-cli in
JSON-RPC daemon mode. signal-cli is FORWARD-ONLY (its `receive`/`subscribeReceive`
drain the server's transient queue; the complete command list has NO history
fetch), so the backward-paged backfill the ingestion contract needs is served over
a method shim. This client reproduces the high-level SignalClient calls a backfill
makes, parsing the REAL signal-cli envelope shapes the wire carries:

    get_history(thread, offset_ts, min_ts, limit) -> (envelopes, next_offset_ts, is_last)
    iter_threads()                                -> [thread descriptor]
    has_history_since(thread, min_ts)             -> (has_more, newest_ts)
    me()                                          -> the linked-account identity

``get_history`` pages BACKWARD on an ``offset_ts`` cursor: the server returns one
page (newest-first by ``timestamp``, <=limit, older than ``offset_ts`` with 0 =
newest, strictly above the ``min_ts`` floor); the CLIENT computes the next page's
cursor = the MIN ts of the page and ``is_last`` = a short page. A message id IS its
``timestamp`` in MILLISECONDS (Signal has no separate integer id).

The persisted linked-device session credential is presented on each read (the
transport substitution — on the real wire a signal-cli daemon holds the linked
device). A missing/wrong session → 401 signal_api_unauthorized; an unknown thread →
signal_api_error; a rate-limit → 429 signal_api_rate_limited + server retry_after.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

from ..config import SignalConfig
from ..fidelity import FidelityReport

_PAGE = 100  # get_history caps a single backward page at 100 (SIGNAL_BACKFILL_PAGE_SIZE)
_MAX_RETRY = 3


def _signal_code(body: Any) -> Optional[str]:
    """Pull the Fyralis SignalApiError code from a JSON-RPC error envelope."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("data"), dict):
            return err["data"].get("signal_code")
    return None


class SignalError(Exception):
    def __init__(self, http_code: int, signal_code: Optional[str], message: str) -> None:
        super().__init__(f"{http_code} {signal_code or '?'}: {message}")
        self.http_code = http_code
        self.signal_code = signal_code
        self.message = message


class SignalClient:
    def __init__(self, cfg: SignalConfig, report: FidelityReport):
        self.base_url, session = cfg.require_auth()
        self.report = report
        self.session = requests.Session()
        self._headers = {"Authorization": f"Session {session}",
                         "Accept": "application/json",
                         "Content-Type": "application/json"}

    def _post(self, method: str, body: dict | None, label: str) -> tuple[int, Any]:
        """POST a method call; honour a server rate-limit (429 + retry_after)."""
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.post(f"{self.base_url}/v1/{method}",
                                     headers=self._headers, json=body or {}, timeout=30)
            if resp.status_code == 429:
                try:
                    secs = (resp.json().get("error", {}).get("data", {})
                            .get("retry_after"))
                except ValueError:
                    secs = None
                # The wait is server-chosen (signal has no client-side bucket) — honour it.
                self.report.record_rate_limit(label, 429, secs, honored=secs is not None)
                if attempt < _MAX_RETRY and secs is not None:
                    time.sleep(min(secs, 1))  # bounded for the audit
                    continue
            break
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, None

    # ---- queries -----------------------------------------------------------

    def me(self) -> tuple[int, Any]:
        return self._post("me", {}, "me")

    def iter_threads(self) -> tuple[int, Any]:
        return self._post("iter_threads", {}, "iter_threads")

    def has_history_since(self, *, thread: dict, min_ts: int) -> tuple[int, Any]:
        return self._post("has_history_since",
                          {"thread": thread, "min_ts": min_ts}, "has_history_since")

    def get_history(self, *, thread: dict, offset_ts: int = 0, min_ts: int = 0,
                    limit: int = _PAGE):
        """One backward page. Returns (status, envelopes, next_offset_ts, is_last).

        ``next_offset_ts`` is the MIN timestamp of the page (the cursor for the next,
        older page); ``is_last`` is True on a short page (< limit) — the canonical
        backward walk."""
        limit = min(_PAGE, max(1, limit))
        st, body = self._post(
            "get_history",
            {"thread": thread, "offset_ts": offset_ts, "min_ts": min_ts, "limit": limit},
            "get_history")
        if st != 200 or not isinstance(body, dict):
            return st, None, None, True
        messages = body.get("messages") or []
        tss = [_envelope_ts(m) for m in messages]
        tss = [t for t in tss if isinstance(t, int) and t > 0]
        next_offset_ts = min(tss) if tss else None
        is_last = len(messages) < limit or next_offset_ts is None
        return st, messages, next_offset_ts, is_last


def _envelope_ts(env: Any) -> Optional[int]:
    """The message id = the envelope ``timestamp`` (epoch MILLISECONDS)."""
    if isinstance(env, dict) and isinstance(env.get("timestamp"), int):
        return env["timestamp"]
    return None
