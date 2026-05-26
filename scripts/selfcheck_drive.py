#!/usr/bin/env python3
"""Offline self-check for the Google Drive slice.

A DEV HARNESS. One throwaway server fakes the token endpoint, the Admin Directory, and
the Drive v3 surface the slice uses: drives.list, files.list (paginated, incl. a
Google-native doc + a trashed file), files/{id}/comments (requires fields), revisions,
files/{id}/export (raw bytes), changes.getStartPageToken, and changes.list (terminating
with a newStartPageToken). Asserts shared-drive + My-Drive backfill, comment/revision
fetch, export, and the incremental changes feed — no mock-specific code in the client.

Run:  python scripts/selfcheck_drive.py
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
USERS = [f"u{i}@{DOMAIN}" for i in range(2)]


def f_file(i, mime="application/pdf", trashed=False):
    return {"kind": "drive#file", "id": f"file-{i:04d}", "name": f"doc {i}",
            "mimeType": mime, "trashed": trashed, "modifiedTime": "2024-01-01T00:00:00.000Z"}


class FakeDrive(BaseHTTPRequestHandler):
    def _send(self, code, payload, *, raw_text=None):
        if raw_text is not None:
            body = raw_text.encode()
            ctype = "text/plain"
        else:
            body = json.dumps(payload).encode()
            ctype = "application/json; charset=UTF-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if urlsplit(self.path).path.endswith("/token"):
            return self._send(200, {"access_token": "ya29.selfcheck", "token_type": "Bearer",
                                    "expires_in": 3600, "scope": "drive.readonly"})
        return self._send(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        split = urlsplit(self.path)
        path, q = split.path, {k: v[0] for k, v in parse_qs(split.query).items()}

        if path.endswith("/admin/directory/v1/users"):
            return self._send(200, {"kind": "admin#directory#users",
                                    "users": [{"kind": "admin#directory#user", "id": u.split("@")[0],
                                               "primaryEmail": u} for u in USERS]})
        if path.endswith("/drive/v3/drives"):
            return self._send(200, {"kind": "drive#driveList",
                                    "drives": [{"kind": "drive#drive", "id": "sd-1",
                                                "name": "Shared Drive 1"}]})
        if path.endswith("/drive/v3/files"):
            tok = q.get("pageToken")
            if tok is None:
                return self._send(200, {"kind": "drive#fileList", "incompleteSearch": False,
                                        "nextPageToken": "f2",
                                        "files": [f_file(1, "application/vnd.google-apps.document"),
                                                  f_file(2, "application/pdf", trashed=True)]})
            return self._send(200, {"kind": "drive#fileList", "incompleteSearch": False,
                                    "files": [f_file(3, "application/pdf")]})
        if path.endswith("/comments"):
            return self._send(200, {"kind": "drive#commentList",
                                    "comments": [{"kind": "drive#comment", "id": "c1",
                                                  "content": "nice", "createdTime": "2024-01-01T00:00:00Z"}]})
        if path.endswith("/revisions"):
            return self._send(200, {"kind": "drive#revisionList",
                                    "revisions": [{"kind": "drive#revision", "id": "r1",
                                                   "modifiedTime": "2024-01-01T00:00:00Z"}]})
        if path.endswith("/export"):
            return self._send(200, None, raw_text="exported document text\n")
        if path.endswith("/changes/startPageToken"):
            return self._send(200, {"kind": "drive#startPageToken", "startPageToken": "100"})
        if path.endswith("/drive/v3/changes"):
            tok = q.get("pageToken")
            if tok == "100":
                return self._send(200, {"kind": "drive#changeList", "nextPageToken": "200",
                                        "changes": [{"kind": "drive#change", "fileId": "file-0001",
                                                     "removed": False, "time": "2024-01-02T00:00:00Z",
                                                     "changeType": "file"}]})
            return self._send(200, {"kind": "drive#changeList", "newStartPageToken": "300",
                                    "changes": [{"kind": "drive#change", "fileId": "file-0002",
                                                 "removed": True, "time": "2024-01-03T00:00:00Z",
                                                 "changeType": "file"}]})
        return self._send(404, {"error": "not found"})

    def log_message(self, *_):
        pass


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), FakeDrive)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    b = f"http://127.0.0.1:{port}"
    os.environ.update({
        "GOOGLE_SERVICE_ACCOUNT_EMAIL": f"sa@{DOMAIN}.iam.gserviceaccount.com",
        "GOOGLE_CUSTOMER_ID": "C0selfcheck", "GOOGLE_DOMAIN": DOMAIN,
        "GMAIL_DIRECTORY_BASE_URL": f"{b}/admin/directory/v1", "GMAIL_TOKEN_URL": f"{b}/token",
        "GOOGLE_DRIVE_API_BASE_URL": f"{b}/drive/v3", "GOOGLE_DRIVE_TOKEN_URL": f"{b}/token",
    })
    for k in ("GOOGLE_PRIVATE_KEY", "GOOGLE_PRIVATE_KEY_PATH"):
        os.environ.pop(k, None)

    from ingest.google import run as google_run
    print("== drive ==")
    report = google_run.run_drive()
    oc = report.object_counts
    print(f"  counts={dict(oc)}")
    print(f"  pages={report.pages}")
    print(f"  protocol={[(p['check'], p['ok']) for p in report.protocol_checks]}")
    print(f"  schema={ {k:(v.passed,v.failed) for k,v in report.schema_checks.items()} }")
    print(f"  divergences={[d.line() for d in report.divergences]}")

    failures = []
    for obj in ("shared_drive", "file", "comment", "revision", "file_exported", "change"):
        if oc.get(obj, 0) <= 0:
            failures.append(f"no {obj} ingested/exercised")
    if report.pages.get("drive.files.list", 0) < 2:
        failures.append("files.list pagination not exercised")
    checks = {p["check"]: p["ok"] for p in report.protocol_checks}
    if not any("newStartPageToken" in k and v for k, v in checks.items()):
        failures.append("changes feed did not terminate with newStartPageToken")
    if report.divergences:
        failures.append(f"unexpected divergences: {[d.line() for d in report.divergences]}")

    print()
    if failures:
        print("SELF-CHECK FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SELF-CHECK PASSED ✅  (SA token, directory enumerate, shared+My drive files "
          "pagination, comments/revisions, export bytes, changes.getStartPageToken + "
          "terminating changes feed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
