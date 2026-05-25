#!/usr/bin/env python3
"""Offline self-check for the Gmail + Calendar (Google Workspace) slices.

A DEV HARNESS. One throwaway HTTP server fakes the whole Google surface these slices
use: the OAuth2 token endpoint (JWT-bearer grant), the Admin Directory users list, the
Gmail profile/messages list+get, and the Calendar calendarList/events — including the
`nextSyncToken` on the final events page, the incremental `syncToken` path, and the
expired-token `410 fullSyncRequired` path. Points the client at it via env and runs both
slices, asserting the DWD token exchange, directory enumeration, pagination, full fetch,
and the Calendar sync paths all work — no mock-specific code in the client.

Run:  python scripts/selfcheck_google.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOMAIN = "spammer-org.test"
USERS = [f"u{i}@{DOMAIN}" for i in range(3)]
VALID_SYNC = "SYNC_OK"


def directory_user(email):
    return {"kind": "admin#directory#user", "id": email.split("@")[0], "primaryEmail": email,
            "name": {"fullName": email}, "isAdmin": False, "suspended": False}


def message_stub(i):
    return {"id": f"msg-{i:05d}", "threadId": f"thr-{i:05d}"}


def message_full(mid):
    return {"id": mid, "threadId": "thr-1", "labelIds": ["INBOX"], "snippet": "hi",
            "historyId": "12345", "internalDate": "1700000000000", "sizeEstimate": 1024,
            "payload": {"mimeType": "text/plain", "headers": []}}


def event(i):
    return {"kind": "calendar#event", "id": f"ev-{i:05d}", "status": "confirmed",
            "summary": f"Event {i}", "start": {"dateTime": "2024-01-01T10:00:00Z"},
            "end": {"dateTime": "2024-01-01T11:00:00Z"}}


class FakeGoogle(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if urlsplit(self.path).path.endswith("/token"):
            return self._send(200, {"access_token": "ya29.selfcheck", "token_type": "Bearer",
                                    "expires_in": 3600, "scope": "readonly"})
        return self._send(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        split = urlsplit(self.path)
        path, q = split.path, {k: v[0] for k, v in parse_qs(split.query).items()}

        if path.endswith("/admin/directory/v1/users"):
            return self._send(200, {"kind": "admin#directory#users",
                                    "users": [directory_user(u) for u in USERS]})

        if path.endswith("/profile"):
            return self._send(200, {"emailAddress": USERS[0], "messagesTotal": 60,
                                    "threadsTotal": 40, "historyId": "9999"})

        if path.endswith("/messages"):
            tok = q.get("pageToken")
            if tok is None:
                return self._send(200, {"messages": [message_stub(i) for i in range(25)],
                                        "resultSizeEstimate": 60, "nextPageToken": "m2"})
            return self._send(200, {"messages": [message_stub(i) for i in range(25, 40)],
                                    "resultSizeEstimate": 60})
        if "/messages/" in path:
            return self._send(200, message_full(path.rsplit("/", 1)[-1]))

        if path.endswith("/calendarList"):
            return self._send(200, {"kind": "calendar#calendarList", "etag": "e",
                                    "items": [{"kind": "calendar#calendarListEntry",
                                               "id": USERS[0], "summary": "Primary",
                                               "primary": True}]})
        if "/events" in path:
            sync = q.get("syncToken")
            if sync is not None:
                if sync == VALID_SYNC:  # incremental: a small change set
                    return self._send(200, {"kind": "calendar#events", "etag": "e",
                                            "items": [event(999)], "nextSyncToken": VALID_SYNC})
                return self._send(410, {"error": {"errors": [{"domain": "calendar",
                    "reason": "fullSyncRequired", "message": "Sync token is no longer valid."}],
                    "code": 410, "message": "Sync token is no longer valid."}})
            tok = q.get("pageToken")
            if tok is None:
                return self._send(200, {"kind": "calendar#events", "etag": "e", "summary": "c",
                                        "timeZone": "UTC", "accessRole": "owner",
                                        "defaultReminders": [],
                                        "items": [event(i) for i in range(25)],
                                        "nextPageToken": "e2"})
            return self._send(200, {"kind": "calendar#events", "etag": "e", "summary": "c",
                                    "timeZone": "UTC", "accessRole": "owner",
                                    "defaultReminders": [], "items": [event(i) for i in range(25, 40)],
                                    "nextSyncToken": VALID_SYNC})
        return self._send(404, {"error": "not found"})

    def log_message(self, *_):
        pass


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), FakeGoogle)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    b = f"http://127.0.0.1:{port}"
    os.environ.update({
        "GOOGLE_SERVICE_ACCOUNT_EMAIL": f"sa@{DOMAIN}.iam.gserviceaccount.com",
        "GOOGLE_CUSTOMER_ID": "C0selfcheck", "GOOGLE_DOMAIN": DOMAIN,
        "GMAIL_API_BASE_URL": f"{b}/gmail/v1", "GMAIL_TOKEN_URL": f"{b}/token",
        "GMAIL_DIRECTORY_BASE_URL": f"{b}/admin/directory/v1",
        "CALENDAR_API_BASE_URL": f"{b}/calendar/v3", "CALENDAR_TOKEN_URL": f"{b}/token",
    })
    for k in ("GOOGLE_PRIVATE_KEY", "GOOGLE_PRIVATE_KEY_PATH"):
        os.environ.pop(k, None)  # force the ephemeral-key path

    from ingest.google import run as google_run
    failures = []

    print("== gmail ==")
    g = google_run.run_gmail()
    print(f"  counts={dict(g.object_counts)} pages={g.pages}")
    print(f"  schema={ {k:(v.passed,v.failed) for k,v in g.schema_checks.items()} }")
    if g.object_counts.get("user", 0) <= 0: failures.append("gmail: no users enumerated")
    if g.object_counts.get("message", 0) <= 0: failures.append("gmail: no messages fetched")
    if g.pages.get("gmail.messages.list", 0) < 2: failures.append("gmail: list pagination not exercised")
    if g.divergences: failures.append(f"gmail divergences: {[d.line() for d in g.divergences]}")

    print("== calendar ==")
    c = google_run.run_calendar()
    print(f"  counts={dict(c.object_counts)} pages={c.pages}")
    print(f"  protocol={[(p['check'],p['ok']) for p in c.protocol_checks]}")
    if c.object_counts.get("event", 0) <= 0: failures.append("calendar: no events")
    if c.pages.get("calendar.events.list", 0) < 2: failures.append("calendar: events pagination not exercised")
    checks = {p["check"]: p["ok"] for p in c.protocol_checks}
    if not any("incremental sync" in k and v for k, v in checks.items()):
        failures.append("calendar: incremental syncToken path not verified")
    if not any("expired" in k and v for k, v in checks.items()):
        failures.append("calendar: expired-syncToken 410 path not verified")
    if c.divergences: failures.append(f"calendar divergences: {[d.line() for d in c.divergences]}")

    print()
    if failures:
        print("SELF-CHECK FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SELF-CHECK PASSED ✅  (SA JWT-bearer token exchange, directory enumerate, Gmail "
          "list pagination + full fetch, Calendar events pagination + syncToken incremental "
          "+ expired-token 410)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
