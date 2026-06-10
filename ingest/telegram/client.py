"""A minimal Telethon-shaped MTProto client over the method-contract shim.

The real Telegram user API is MTProto (encrypted binary); a real consumer uses
Telethon. We cannot run Telethon against a method shim (it speaks the binary wire
+ does a DH handshake), so this is a small async-free client that reproduces the
exact surface Telethon exposes for the calls a backfill makes:

    get_history(peer, offset_id, min_id, limit) -> (messages, next_offset_id, is_last)
    get_dialogs(limit)                          -> [dialog descriptor]
    get_me()                                    -> the self user

``get_history`` mirrors Telethon's contract precisely (verified vs
core.telegram.org/api/offsets + the Telethon get_messages docstring): the server
returns one backward page (newest-first, ≤limit, older than ``offset_id`` with 0 =
newest, strictly above the ``min_id`` floor); the CLIENT computes the next page's
cursor = the MIN id of the page and ``is_last`` = a short page. Each message is a
TL ``message`` (``date``/``edit_date`` EPOCH SECONDS; ``from_id`` a Peer or NULL).

The persisted ``StringSession`` credential is presented on each read (the
transport substitution — on the real wire it authenticates the connection). A
missing/wrong session → 401 AUTH_KEY_UNREGISTERED; a bad peer → PEER_ID_INVALID;
``FLOOD_WAIT_X`` (RPC error 420) carries the server-chosen wait in seconds.
"""
from __future__ import annotations

import re
import time
from typing import Any

import requests

from ..config import TelegramConfig
from ..fidelity import FidelityReport

_PAGE = 100  # messages.getHistory caps a single page at 100 (Telethon _MAX_CHUNK_SIZE)
_MAX_RETRY = 3
_FLOOD_RE = re.compile(r"FLOOD_WAIT_(\d+)")


class TelegramError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message


class TelegramClient:
    def __init__(self, cfg: TelegramConfig, report: FidelityReport):
        self.base_url, session = cfg.require_auth()
        self.report = report
        self.session = requests.Session()
        self._headers = {"Authorization": f"Session {session}",
                         "Accept": "application/json",
                         "Content-Type": "application/json"}

    def _post(self, method: str, body: dict | None, label: str) -> tuple[int, Any]:
        """POST a method call; honour FLOOD_WAIT (420 + server seconds)."""
        resp = None
        for attempt in range(_MAX_RETRY + 1):
            resp = self.session.post(f"{self.base_url}/{method}", headers=self._headers,
                                     json=body or {}, timeout=30)
            if resp.status_code == 420:
                m = _FLOOD_RE.search((resp.json() or {}).get("error_message", ""))
                secs = int(m.group(1)) if m else None
                # The wait is server-chosen (not a client bucket) — honour it.
                self.report.record_rate_limit(label, 420, secs, honored=secs is not None)
                if attempt < _MAX_RETRY and secs is not None:
                    time.sleep(min(secs, 1))  # bounded for the audit
                    continue
            break
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, None

    # ---- queries -----------------------------------------------------------

    def get_me(self) -> tuple[int, Any]:
        return self._post("users.getFullUser", {}, "users.getFullUser")

    def get_dialogs(self, *, limit: int = 200) -> tuple[int, Any]:
        return self._post("messages.getDialogs", {"limit": limit}, "messages.getDialogs")

    def get_history(self, *, peer: dict, offset_id: int = 0, min_id: int = 0,
                    limit: int = _PAGE):
        """One backward page. Returns (status, messages, next_offset_id, is_last).

        ``next_offset_id`` is the MIN id of the page (the cursor for the next, older
        page); ``is_last`` is True on a short page (< limit) — the canonical
        Telethon backward walk."""
        limit = min(_PAGE, max(1, limit))
        st, body = self._post(
            "messages.getHistory",
            {"peer": peer, "offset_id": offset_id, "min_id": min_id, "limit": limit},
            "messages.getHistory")
        if st != 200 or not isinstance(body, dict):
            return st, None, None, True
        messages = body.get("messages") or []
        ids = [m["id"] for m in messages
               if isinstance(m.get("id"), int) and m["id"] > 0]
        next_offset_id = min(ids) if ids else None
        is_last = len(messages) < limit or next_offset_id is None
        return st, messages, next_offset_id, is_last
